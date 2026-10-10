"""检索出口话题一致性校验（M6）与"过滤已放宽"诚实告知（M7）的离线守卫。

M6 治的是打分治不了的那一半：交叉编码器只看"这段文本像不像在回答这个问题"，于是图注与 URL
里的词能把**另一个型号**的切片顶到高分（线上实测：话题是 iPhone，窗口里坐着平板手册的存储
章节，答案写成"该设备 UFS4.1 / 12+256GB"）。"这个名字到底在不在切片里"是一条确定的字符串
事实，所以校验不依赖任何 Milvus 新列，disabled 库（无词表、无 item_name 可过滤）也能用。

M7 治的是"放宽之后冒充答案"：`entity_filter_relaxed` 为真意味着用户点名的型号在本库带过滤
一条都没召回、过滤已被拆掉重查过一次，此时窗口里全是别的型号——不能不说，也不能因此拒答
（内容确实检索到了）。这条只补说明，绝不抢 R5 那两个"结果语义"的话术槽。

本批用例只锁这几件事：换入换出的触发与不触发、等量替换（条数不减）、分数降序不变量、
web 结果不参与、归一化与别名口径、开关可关回旧行为、异常 fail-open、两条出口都过校验；
以及 M7 的触发面/三种不触发/幂等/不污染固定话术/说明能到达历史与 FINAL。
"""

from __future__ import annotations

import ast
from types import SimpleNamespace
from typing import Any

import pytest
from graph_introspection import PROJECT_ROOT, state_key_writes

import processor.query_processor.nodes.node_rerank as rerank_node_module
from processor.query_processor.nodes import node_answer_output as answer_module
from processor.query_processor.nodes.node_answer_output import (
    GENERATION_FAILED_REPLY,
    INSUFFICIENT_EVIDENCE_REPLY,
    RELAXED_FILTER_NOTICE_TEMPLATE,
    NodeAnswerOutput,
)
from processor.query_processor.nodes.node_rerank import NodeRerank
from processor.query_processor.topic_consistency import (
    GUARD_DISABLE_ENV,
    MAX_TOPIC_GUARD_SWAPS,
    align_window_to_topic,
    topic_entities,
)
from utils.reranker_http_utils import RerankError

RERANK_NODE_PATH = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_rerank.py"
ANSWER_NODE_PATH = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_answer_output.py"

TOPIC = "HAK-180"
ON_TOPIC_BODY = "HAK 180 温度建议设置在 110℃ 左右，按面板上的 TEMP 键调整。"
OFF_TOPIC_BODY = "隔壁型号的保养周期为每季度一次，请断开气源后再清洁加热板。"


def _doc(
    chunk_id: str,
    *,
    title: str,
    content: str,
    score: float | None = None,
    rrf_score: float | None = None,
    source: str = "local",
    **extra: Any,
) -> dict:
    """构造一条本地切片（字段与检索输出白名单一致）；`title` 里是否含话题名就是本批的判据。"""
    doc = {
        "chunk_id": chunk_id,
        "source": source,
        "title": title,
        "content": content,
        "file_name": f"{title}.pdf",
        "document_id": 7,
        "chunk_index": 1,
    }
    if score is not None:
        doc["score"] = score
    if rrf_score is not None:
        doc["rrf_score"] = rrf_score
    doc.update(extra)
    return doc


def _state(**overrides) -> dict:
    state = {
        "session_id": "s-m6",
        "original_query": "HAK-180 怎么调温",
        "rewritten_query": "HAK-180 怎么调温",
        "item_names": [TOPIC],
        "entity_recognition_mode": "required",
        "entity_vocabulary": [{"name": TOPIC, "aliases": ["烫金机"]}],
        "history": [],
        "top_k": 3,
    }
    state.update(overrides)
    return state


def _ids(docs: list[dict]) -> list[Any]:
    return [doc.get("chunk_id") for doc in docs]


# --------------------------------------------------------------------------
# 一、话题实体的取法：对齐规范名优先，其次检索词里的形状
# --------------------------------------------------------------------------

def test_confirmed_entity_names_carry_their_vocabulary_aliases() -> None:
    """匹配键要含本库词表别名：切片标题常写"烫金机"而不写规范名，只比规范名会把它判成跑题。"""
    entities = topic_entities(_state())
    assert [name for name, _ in entities] == [TOPIC]
    assert set(entities[0][1]) == {"hak180", "烫金机"}, "归一化要去掉连字符，别名要并进来"


def test_disabled_library_falls_back_to_the_shape_in_the_retrieval_text() -> None:
    """disabled 库没有词表、item_names 恒空：锚只能从检索词现取（M4 回填的话题实体就在这儿）。"""
    entities = topic_entities(_state(
        item_names=[], entity_vocabulary=[],
        original_query="怎么调温", rewritten_query="怎么调温",
    ))
    assert entities == [], "本轮什么都没提话题实体时不该臆造一个"

    entities = topic_entities(_state(
        item_names=[], entity_vocabulary=[], rewritten_query="HAK180 的调温上限是多少",
    ))
    assert [keys for _name, keys in entities] == [["hak180"]]


def test_fragmentary_entity_names_are_dropped_instead_of_matching_everything() -> None:
    """归一化后短于闸门的实体名（"C7"、"ab"）一律丢弃：留着它等于所有切片都"相关"。"""
    assert topic_entities(_state(item_names=["ab"], entity_vocabulary=[])) == []
    assert topic_entities(_state(item_names=["C7"], entity_vocabulary=[])) == []


def test_the_guard_needs_at_least_two_window_docs() -> None:
    """只有一条切片时无从"替换"（换出去就少一条证据），直接原样放行。"""
    window = [_doc("c1", title="竞品保养", content=OFF_TOPIC_BODY, score=0.9)]
    pool = [_doc("c2", title="HAK-180 调温", content=ON_TOPIC_BODY, score=0.5)]
    assert _ids(align_window_to_topic(_state(), window, pool)) == ["c1"]


# --------------------------------------------------------------------------
# 二、换入换出的触发面与四条不触发
# --------------------------------------------------------------------------

def test_off_topic_window_doc_is_swapped_for_a_match_from_outside_the_window() -> None:
    """核心场景：高分但整篇没提话题实体的块被换出，窗口外命中它的块补进来（条数不变）。"""
    window = [
        _doc("c1", title="HAK-180 调温", content=ON_TOPIC_BODY, score=0.91),
        _doc("c2", title="竞品保养规程", content=OFF_TOPIC_BODY, score=0.88),
    ]
    pool = [_doc("c3", title="HAK-180 常见故障", content="HAK180 加热异常时先检查保险丝。", score=0.42)]

    result = align_window_to_topic(_state(), window, pool)

    assert _ids(result) == ["c1", "c3"], "跑题块被换出、窗口外命中块补进来"
    assert len(result) == len(window), "等量替换：条数是 top_k 的承诺"


def test_no_swap_when_nothing_inside_the_window_mentions_the_entity() -> None:
    """窗口内一条都不命中：多半是锚取错了或本库真没有，硬把窗口外的低分块换进来是拿猜测覆盖精排。"""
    window = [
        _doc("c1", title="竞品保养规程", content=OFF_TOPIC_BODY, score=0.9),
        _doc("c2", title="通用安全须知", content="操作时请佩戴隔热手套。", score=0.8),
    ]
    pool = [_doc("c3", title="HAK-180 调温", content=ON_TOPIC_BODY, score=0.3)]

    assert _ids(align_window_to_topic(_state(), window, pool)) == ["c1", "c2"]


def test_no_swap_when_no_outside_doc_mentions_the_entity() -> None:
    """窗口外也没有命中块：换出只会少证据（宁查勿否），原样放行。"""
    window = [
        _doc("c1", title="HAK-180 调温", content=ON_TOPIC_BODY, score=0.9),
        _doc("c2", title="竞品保养规程", content=OFF_TOPIC_BODY, score=0.8),
    ]
    pool = [_doc("c3", title="车间卫生规范", content="每班结束后清扫工作台面。", score=0.3)]

    assert _ids(align_window_to_topic(_state(), window, pool)) == ["c1", "c2"]


def test_no_swap_without_a_topic_entity_at_all() -> None:
    """没有话题实体（泛概念问题、best-effort 检索）时本道校验必须完全不动作。"""
    state = _state(item_names=[], entity_vocabulary=[], rewritten_query="怎么测主板的短路问题")
    window = [
        _doc("c1", title="短路查修", content="通电前先打各主供电电感对地阻值。", score=0.9),
        _doc("c2", title="保险丝规格", content="保险丝额定电流为 10A。", score=0.8),
    ]

    assert _ids(align_window_to_topic(state, window, [])) == ["c1", "c2"]


def test_web_docs_are_never_treated_as_off_topic() -> None:
    """联网结果的标题来自搜索引擎页面，与本库实体名对不上是常态：它既不参与判题也不被换出。"""
    window = [
        _doc("w1", title="隔壁型号保养 - 官网", content=OFF_TOPIC_BODY, score=0.95, source="web",
             url="https://example.com/a"),
        _doc("c2", title="竞品保养规程", content=OFF_TOPIC_BODY, score=0.8),
    ]
    pool = [_doc("c3", title="HAK-180 调温", content=ON_TOPIC_BODY, score=0.3)]

    # 两条都不提话题实体，但只有本地那条能被换出；web 那条留在原位。
    assert _ids(align_window_to_topic(_state(), window, pool)) == ["w1", "c3"]


def test_empty_bodied_candidates_are_never_swapped_in() -> None:
    """空正文的"命中块"换进来等于没换：`_select_context_docs` 会跳过它，窗口反而少一条。"""
    window = [
        _doc("c1", title="HAK-180 调温", content=ON_TOPIC_BODY, score=0.9),
        _doc("c2", title="竞品保养规程", content=OFF_TOPIC_BODY, score=0.8),
    ]
    pool = [_doc("c3", title="HAK-180 空白块", content="   ", score=0.4)]

    assert _ids(align_window_to_topic(_state(), window, pool)) == ["c1", "c2"]

# --------------------------------------------------------------------------
# 三、不变量：分数降序、条数封顶、归一化口径
# --------------------------------------------------------------------------

def test_window_stays_score_descending_after_the_swap() -> None:
    """换入的是低分块：不重新排序就会破坏"断崖之后所有下游都假定降序"这一不变量。"""
    window = [
        _doc("c1", title="竞品保养规程", content=OFF_TOPIC_BODY, score=0.9),
        _doc("c2", title="HAK-180 调温", content=ON_TOPIC_BODY, score=0.86),
        _doc("c3", title="竞品安全须知", content="操作时请佩戴隔热手套。", score=0.7),
    ]
    pool = [_doc("c4", title="HAK-180 清洁", content="HAK180 清洁前断电。", score=0.31)]

    result = align_window_to_topic(_state(), window, pool)

    scores = [doc["score"] for doc in result]
    assert scores == sorted(scores, reverse=True), f"来源面板与断崖都按降序解读：{scores}"
    assert _ids(result) == ["c1", "c2", "c4"], "只换出分数最低的那条跑题块，头部高分块不动"


def test_swaps_are_capped() -> None:
    """一次最多换出 MAX_TOPIC_GUARD_SWAPS 条：本道校验是兜底层，不该整体改写精排的结论。"""
    window = [_doc("c1", title="HAK-180 调温", content=ON_TOPIC_BODY, score=0.9)] + [
        _doc(f"x{index}", title=f"竞品资料{index}", content=OFF_TOPIC_BODY, score=0.8 - index * 0.01)
        for index in range(4)
    ]
    pool = [
        _doc(f"y{index}", title="HAK-180 附录", content=f"HAK180 附录{index}", score=0.2 - index * 0.01)
        for index in range(4)
    ]

    result = align_window_to_topic(_state(), window, pool)

    assert len(result) == 5
    swapped_in = [doc for doc in result if str(doc["chunk_id"]).startswith("y")]
    assert len(swapped_in) == MAX_TOPIC_GUARD_SWAPS


def test_hyphen_and_case_variants_count_as_the_same_entity() -> None:
    """用户写 HAK-180、文件名写 hak180产品安全手册：必须按同一口径（去标点 + casefold）判相关。"""
    window = [
        _doc("c1", title="hak180产品安全手册 第2章", content="温度建议 110℃。", score=0.9),
        _doc("c2", title="竞品保养", content=OFF_TOPIC_BODY, score=0.8),
    ]
    pool = [_doc("c3", title="HAK 180 调温", content="调整 TEMP 键。", score=0.3)]

    assert _ids(align_window_to_topic(_state(), window, pool)) == ["c1", "c3"], "标题命中即算相关"


def test_alias_only_document_is_recognised_as_on_topic() -> None:
    """切片只写了别名"烫金机"而没写规范名：靠词表别名键判相关，否则真答案块会被当成跑题。"""
    state = _state(item_names=[TOPIC])
    window = [
        _doc("c1", title="烫金机作业指导书", content="开机前确认气源压力。", score=0.9),
        _doc("c2", title="竞品保养", content=OFF_TOPIC_BODY, score=0.8),
    ]

    assert _ids(align_window_to_topic(state, window, [])) == ["c1", "c2"]


def test_switch_keeps_the_old_behaviour_and_still_logs(monkeypatch) -> None:
    """`DRIFT_GUARD_DISABLE=1`：判断照做但结果原样，线上才能拿同一批请求做基线对比。"""
    monkeypatch.setenv(GUARD_DISABLE_ENV, "1")
    window = [
        _doc("c1", title="HAK-180 调温", content=ON_TOPIC_BODY, score=0.9),
        _doc("c2", title="竞品保养", content=OFF_TOPIC_BODY, score=0.8),
    ]
    pool = [_doc("c3", title="HAK-180 故障", content="HAK180 加热异常先查保险丝。", score=0.3)]

    assert _ids(align_window_to_topic(_state(), window, pool, apply_changes=False)) == ["c1", "c2"]
    assert _ids(align_window_to_topic(_state(), window, pool, apply_changes=True)) == ["c1", "c3"]


def test_guard_is_fail_open_on_internal_errors(monkeypatch) -> None:
    """校验自己出错时必须原样放行：这是锦上添花的一层，不能把它变成新的故障源。"""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("校验内部炸了")

    monkeypatch.setattr(rerank_node_module, "align_window_to_topic", _boom)
    window = [_doc("c1", title="HAK-180 调温", content=ON_TOPIC_BODY, score=0.9)]
    pool = [_doc("c2", title="HAK-180 故障", content=ON_TOPIC_BODY, score=0.3)]

    assert NodeRerank()._step_4_topic_consistency_guard(_state(), window, pool) is window


# --------------------------------------------------------------------------
# 四、接线：两条出口（正常精排 / 降级召回序）都过校验
# --------------------------------------------------------------------------

@pytest.fixture
def fake_rerank(monkeypatch) -> SimpleNamespace:
    """拦住精排外部调用（绝不联网）：`scores` 为 None 时用默认降序分，`error` 非空则抛错。"""
    box = SimpleNamespace(scores=None, error=None)

    def _rerank(_query: str, contents: list[str]) -> list[float]:
        if box.error is not None:
            raise box.error
        return box.scores or [0.9 - index * 0.05 for index in range(len(contents))]

    monkeypatch.setattr(rerank_node_module, "rerank_documents", _rerank)
    return box


def _rrf_state(**overrides) -> dict:
    """构造一个能一路跑到 rerank 出口的查询状态（窗口外还留有一条命中块当换入候选）。"""
    state = _state(
        rrf_chunks=[
            _doc("c1", title="HAK-180 调温", content=ON_TOPIC_BODY, rrf_score=0.03),
            _doc("c2", title="竞品保养规程", content=OFF_TOPIC_BODY, rrf_score=0.028),
            _doc("c3", title="竞品安全须知", content="操作时请佩戴隔热手套。", rrf_score=0.026),
            _doc("c4", title="HAK-180 常见故障", content="HAK180 加热异常时先检查保险丝。", rrf_score=0.01),
        ],
        web_search_docs=[],
    )
    state.update(overrides)
    return state


def test_the_swap_happens_on_the_normal_rerank_exit(fake_rerank) -> None:
    state = NodeRerank().process(_rrf_state())

    assert _ids(state["reranked_docs"]) == ["c1", "c2", "c4"], "跑题块被换出、窗口外命中块补进来"
    assert all("score" in doc for doc in state["reranked_docs"])
    assert len(state["reranked_docs"]) == 3, "出口条数仍是 top_k，校验只换人不换量"


def test_the_swap_also_happens_on_the_fallback_exit(fake_rerank) -> None:
    """精排故障降级时分数只是召回序投影，但"名字在不在切片里"照样成立：两条出口都得过。"""
    fake_rerank.error = RerankError("精排服务不可用")
    state = NodeRerank().process(_rrf_state())

    assert state["rerank_failed"] is True
    assert _ids(state["reranked_docs"]) == ["c1", "c2", "c4"], "降级出口同样过校验（换入的是窗口外命中块 c4）"


def test_rerank_exit_writes_reranked_docs_only_through_the_guard() -> None:
    """静态守卫：`reranked_docs` 只能经校验写回（空召回早退除外）。

    曾经有过一次"校验写在成功分支里"的写法，精排一挂整道守卫就静默失效；这种接线错误
    用行为用例很难稳定复现（要同时构造降级 + 命中块），所以钉在源码结构上。
    """
    tree = ast.parse(RERANK_NODE_PATH.read_text(encoding="utf-8"))
    process = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "process"
    )
    assigns: list[ast.AST] = []
    for node in ast.walk(process):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "state"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "reranked_docs"
                for target in node.targets
            )
        ):
            assigns.append(node.value)

    assert len(assigns) == 2, f"预期两处写入（空召回早退 + 校验出口），实际：{[ast.unparse(a) for a in assigns]}"
    guarded = [a for a in assigns if isinstance(a, ast.Call) and "_step_4_topic_consistency_guard" in ast.unparse(a.func)]
    assert len(guarded) == 1, "非空出口必须走校验，不能把断崖结果直接写回"
    assert [ast.unparse(a) for a in assigns if a not in guarded] == ["[]"]


def test_guard_does_not_read_the_refusal_channel() -> None:
    """M6 读的是 item_names / rewritten_query，绝不碰本轮原文通道（R2）：校验不产生拒答。"""
    assert state_key_writes(RERANK_NODE_PATH, "NodeRerank", "_step_4_topic_consistency_guard") == set()


# --------------------------------------------------------------------------
# 五、M4 → M6 的接力：省略型追问补出来的锚能被出口校验用上
# --------------------------------------------------------------------------

def test_backfilled_anchor_feeds_the_window_guard() -> None:
    """disabled 库（无词表、item_names 恒空）里，M4 回填进检索词的话题锚就是 M6 唯一的锚。

    这条钉的是两个改动的接线：省略型追问回填后，校验才拿得到实体名去比切片标题。
    """
    state = _state(
        item_names=[],
        entity_recognition_mode="disabled",
        entity_vocabulary=[],
        rewritten_query="苹果手机 有什么存储类型",
    )
    window = [
        _doc("c1", title="苹果手机 存储与内存", content="提供 256GB 与 512GB 两种存储容量。", score=0.9),
        _doc("c2", title="华为平板 C7 用户指南 存储", content="支持 UFS 3.1，12+256GB。", score=0.8),
    ]
    pool = [_doc("c3", title="苹果手机 型号对照", content="各型号的容量与颜色见下表。", score=0.3)]

    assert _ids(align_window_to_topic(state, window, pool)) == ["c1", "c3"]


def test_the_guard_stays_silent_when_the_library_has_no_matching_slice() -> None:
    """诚实边界（线上实测的例2）：本库压根没收录该型号时，窗口里一条都不提它，校验不动作。

    这种情形只能靠 M4（把话题锚喂进检索词）与拒答/清单那几条前置闸，M6 的天花板就是
    “本库至少得有一条切片真的在讲这个对象”——把它钉成用例，是为了下一个人不把 M6 当作
    disabled 库的万能药（根治仍需要 P3：切 required + 重跑导入）。
    """
    state = _state(
        item_names=[],
        entity_recognition_mode="disabled",
        entity_vocabulary=[],
        rewritten_query="苹果手机 有什么存储类型",
    )
    window = [
        _doc("c1", title="华为平板 C7 用户指南 存储", content="支持 UFS 3.1，12+256GB。", score=0.9),
        _doc("c2", title="华为平板 C7 外观", content="机身厚度 6.85mm。", score=0.8),
    ]
    pool = [_doc("c3", title="华为平板 C7 参数", content="峰值亮度 500nit。", score=0.3)]

    assert _ids(align_window_to_topic(state, window, pool)) == ["c1", "c2"]


def test_a_brand_phrase_never_judges_by_body_text() -> None:
    """线上实测（库 33“手机商品介绍”）那一例：锚只到“苹果手机”时，正文不参与判跑题。

    钉的是反向危害：iPhone 文档里真正该留下的参数切片短到只剩“## 256GB”（一个身份字都
    没有），而另一型号的对比文案会写“对标苹果手机”。拿品牌短语去比正文，结果就是把对的
    换出去、把错的留下——比不改更糟。
    """
    state = _state(
        item_names=[],
        entity_recognition_mode="disabled",
        entity_vocabulary=[],
        rewritten_query="苹果手机 有什么存储类型",
    )
    window = [
        _doc("c1", title="存储版本", file_name="iphone18_fb9a213c.pdf", content="## 256GB", score=0.9),
        _doc(
            "c2", title="核心性能", file_name="一加手机ace6t.md", score=0.8,
            content="对标苹果手机的性能释放，可选版本：12+256GB / 16+1TB。",
        ),
    ]
    pool = [_doc("c3", title="苹果手机 型号对照", content="各型号的容量与颜色见下表。", score=0.3)]

    assert _ids(align_window_to_topic(state, window, pool)) == ["c1", "c2"], (
        "两条切片的身份字段都不含“苹果手机”→ 窗口内无一命中→ 整体不动；绝不能把 c1 换出"
    )


def test_a_brand_phrase_still_narrows_by_title_and_file_name() -> None:
    """但品牌+品类短语在“文件名/标题”这种身份位上是有用的，不能连这条收益一起砍掉。

    “华为平板”对得上文件名里就写了“华为平板 C7 用户指南”的那篇，对不上“华为擎云B530”那篇，
    于是窗口里混进来的隔壁文档可以被换出去——这是旧行为里唯一站得住的那部分。
    """
    state = _state(
        item_names=[],
        entity_recognition_mode="disabled",
        entity_vocabulary=[],
        rewritten_query="华为平板 都有哪些型号",
    )
    window = [
        _doc("c1", title="华为平板 C7 用户指南 存储", content="支持 NM 存储卡扩展。", score=0.9),
        _doc("c2", title="华为擎云B530 用户指南 前言", content="感谢购买本产品的用户。", score=0.8),
    ]
    pool = [_doc("c3", title="华为平板 C7 接口说明", content="顶部为 USB-C 接口。", score=0.3)]

    assert _ids(align_window_to_topic(state, window, pool)) == ["c1", "c3"]


# --------------------------------------------------------------------------
# 六、M7：过滤放宽后的诚实告知
# --------------------------------------------------------------------------

FAKE_ANSWER = "换墨流程：先关闭电源再松开压杆。"
USABLE_DOC = {"content": "换墨前先关闭主电源开关，再松开压杆。", "source": "local", "chunk_id": 7, "score": 0.4}


@pytest.fixture
def llm_answer_node(monkeypatch):
    """答案节点的离线桩：可控生成结果与推送/落库记录，绝不联网。"""
    calls = {"llm": 0, "task_result": [], "pushed": [], "saved": []}
    box = SimpleNamespace(answer=FAKE_ANSWER, error=None)

    class _FakeLLM:
        def invoke(self, prompt):
            calls["llm"] += 1
            if box.error is not None:
                raise box.error
            return SimpleNamespace(content=box.answer)

        def stream(self, prompt):
            calls["llm"] += 1
            if box.error is not None:
                raise box.error
            return [SimpleNamespace(content=box.answer)]

    monkeypatch.setattr(answer_module, "get_llm_client", lambda: _FakeLLM())
    monkeypatch.setattr(answer_module, "set_task_result", lambda sid, key, value: calls["task_result"].append(value))
    monkeypatch.setattr(answer_module, "push_to_session", lambda sid, event, data: calls["pushed"].append(data))
    monkeypatch.setattr(answer_module, "add_done_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        answer_module, "save_chat_message", lambda **kwargs: calls["saved"].append(kwargs) or True
    )
    node = NodeAnswerOutput()
    return node, calls, box


def _answer_state(**overrides) -> dict:
    state = {
        "session_id": "s-m7",
        "original_query": "HAK-180 的换墨流程是什么",
        "rewritten_query": "HAK-180 的换墨流程是什么",
        "item_names": [TOPIC],
        "entity_filter_relaxed": True,
        "history": [],
        "reranked_docs": [dict(USABLE_DOC)],
        "is_stream": False,
        "persist_history": True,
    }
    state.update(overrides)
    return state


def test_relaxed_filter_prepends_a_notice_to_the_generated_answer(llm_answer_node) -> None:
    node, _calls, _box = llm_answer_node
    state = node.process(_answer_state())

    assert state["answer"].startswith("（说明：本库暂未收录「HAK-180」")
    assert state["answer"].endswith(FAKE_ANSWER), "只补说明，不动模型结论"
    assert "HAK-180" in state["answer"] and "专属资料" in state["answer"]


def test_no_notice_without_the_relaxed_marker(llm_answer_node) -> None:
    """没放宽过就没有"来源不对题"这件事，硬加一句会让正常答案看着像在推卸。"""
    node, _calls, _box = llm_answer_node
    state = node.process(_answer_state(entity_filter_relaxed=False))

    assert state["answer"] == FAKE_ANSWER, "没放宽就不该多出一句推卸"


def test_no_notice_when_the_topic_entity_is_unclear(llm_answer_node) -> None:
    """item_names 为空时本来就是不带过滤检索，谈不上"拆掉过滤"，说明会说出一件没发生的事。"""
    node, _calls, _box = llm_answer_node
    state = node.process(_answer_state(item_names=[]))

    assert state["answer"] == FAKE_ANSWER


def test_notice_names_at_most_two_entities(llm_answer_node) -> None:
    node, _calls, _box = llm_answer_node
    state = node.process(_answer_state(item_names=["HAK-180", "G740", "DBY-W09"]))

    assert "G740" in state["answer"] and "DBY-W09" not in state["answer"]
    assert state["answer"].count("（说明：") == 1


def test_notice_is_not_prefixed_twice(llm_answer_node) -> None:
    """同一请求重入（重处理、重复消费 state）时不叠第二遍说明。"""
    node, _calls, _box = llm_answer_node
    state = node.process(_answer_state())
    once = state["answer"]
    node._step_3b_disclose_relaxed_filter(state)

    assert state["answer"] == once
    assert state["answer"].count("（说明：") == 1


def test_generation_failure_reply_is_not_decorated(llm_answer_node) -> None:
    """生成自己挂了：输出的是兜底错误句，没有结论需要修正"来源不对题"。"""
    node, _calls, box = llm_answer_node
    box.error = RuntimeError("模型超时")
    box.answer = None
    state = node.process(_answer_state())

    assert state["answer"] == GENERATION_FAILED_REPLY
    assert "（说明：" not in state["answer"]


def test_no_evidence_wording_is_never_decorated(llm_answer_node) -> None:
    """R5 的固定话术已经把"没有内容"说清楚了；叠一条"已拆过滤"是自相矛盾的两种结论。"""
    node, calls, _box = llm_answer_node
    state = node.process(_answer_state(reranked_docs=[]))

    assert state["answer"] == INSUFFICIENT_EVIDENCE_REPLY
    assert "（说明：" not in state["answer"]
    assert calls["llm"] == 0, "M7 不得把无证据守卫带进生成"


def test_preset_short_circuit_answer_is_not_decorated(llm_answer_node) -> None:
    """前置短路（闲聊/拒答/反问/清单）的固定话术不走生成路径，M7 根本不该碰它。"""
    node, _calls, _box = llm_answer_node
    preset = "当前知识库未收录与该型号相关的产品文档，请确认型号或改用其它知识库。"
    state = node.process(_answer_state(answer=preset))

    assert state["answer"] == preset


def test_notice_reaches_history_sources_and_the_final_push(llm_answer_node) -> None:
    """说明必须跟着正文一起落库与推 FINAL：流式下 delta 已推完，靠的是接口层 FINAL 全量覆盖。"""
    node, calls, _box = llm_answer_node
    state = node.process(_answer_state(is_stream=True, persist_history=True))

    assert "（说明：" in state["answer"]
    assert calls["saved"], "答案必须写进历史"
    assert "（说明：" in calls["saved"][0]["text"], "历史里只有正文、下次追问就丢了这条告知"
    final_pushes = [data for data in calls["pushed"] if "answer" in data]
    assert final_pushes and "（说明：" in str(final_pushes[0]["answer"]), "FINAL 必须带说明：前端按它覆盖式渲染"
    assert len(state["sources"]) == 1, "M7 不改来源集合（依据仍是真正进 prompt 的那批切片）"


def test_relaxed_notice_does_not_reuse_the_result_wordings() -> None:
    """三条出口必须各说各事：`INSUFFICIENT_EVIDENCE_REPLY`/`RETRIEVAL_INCOMPLETE_REPLY` 说结果，
    M7 说依据来源。措辞重叠会让用户把"已拆过滤"读成"库里没有"。"""
    assert "没有找到" not in RELAXED_FILTER_NOTICE_TEMPLATE
    assert "检索没有完成" not in RELAXED_FILTER_NOTICE_TEMPLATE
    assert RELAXED_FILTER_NOTICE_TEMPLATE.format(entity="X").startswith("（说明：")


def test_relaxed_notice_never_touches_the_failure_markers() -> None:
    """静态守卫：M7 只写 answer；写进 sources 或失败标记会把它变成第二个 R5。"""
    assert state_key_writes(ANSWER_NODE_PATH, "NodeAnswerOutput", "_step_3b_disclose_relaxed_filter") == {"answer"}
