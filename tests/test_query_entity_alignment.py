"""检索侧实体词表对齐（P4）的离线守卫。

分两层：
1. 行为测试：直接驱动 NodePrepareQuery，把有网络的 LLM 抽取缝（_call_entity_llm）打桩成空，
   只用正则 + 确定性对齐验证"命中过滤 / 唯一候选直采 / 多候选反问 / 型号代码缺失才拒答 /
   品牌词等退回 best-effort"这几条分支，并强断言 disabled 库整段跳过（零回归）。
2. 静态守卫：跨文件一致性——接口两入口都注入 entity_recognition_mode、状态声明了新增通道，
   用 AST/文本读源码，避免为了断言去 import 会拉起 DB/引擎的 query.py。
"""

from __future__ import annotations

import pytest
from graph_introspection import PROJECT_ROOT, typed_dict_keys

from processor.query_processor.chitchat_gate import normalize_for_match
from processor.query_processor.nodes.node_prepare_query import (
    BRAND_GROUP_FILTER_DISABLE_ENV,
    NodePrepareQuery,
)

QUERY_STATE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"
API_QUERY = PROJECT_ROOT / "backend" / "app" / "api" / "query.py"
API_MYSQL = PROJECT_ROOT / "backend" / "app" / "api" / "mysql.py"
API_LIBRARY_FACTS = PROJECT_ROOT / "backend" / "app" / "api" / "library_facts.py"
API_HISTORY = PROJECT_ROOT / "utils" / "mongo_history_utils.py"


def _required_state(query: str, vocabulary: list[dict]) -> dict:
    return {
        "original_query": query,
        "library_id": 7,
        "entity_recognition_mode": "required",
        "entity_vocabulary": vocabulary,
    }


@pytest.fixture()
def node(monkeypatch):
    """返回一个把 LLM 抽取缝打桩为空的节点，测试只走正则 + 确定性对齐，绝不联网。"""
    prepared = NodePrepareQuery()
    monkeypatch.setattr(prepared, "_call_entity_llm", lambda text, history_text: [])
    return prepared


# --------------------------------------------------------------------------
# 一、归一化与词表索引
# --------------------------------------------------------------------------

def test_normalize_strips_whitespace_punctuation_and_casefolds() -> None:
    """R4：检索侧归一化统一走闲聊闸的 `normalize_for_match` 口径（去空白 + 去标点 + casefold）。

    旧口径保留连字符，用户照真实型号写法问 `HAK-180`（归一化 `hak-180`）而词表键是从
    文件名派生的 `hak180产品安全手册`（天然没有连字符），两侧互不包含 → 判“查无”→
    又恰好是收紧档型号代码 → 库里明明有这份文档却被误拒答。
    """
    assert NodePrepareQuery._normalize("HUAWEI MateBook D 14") == "huaweimatebookd14"
    assert NodePrepareQuery._normalize("  烫金机\nPro ") == "烫金机pro"
    assert NodePrepareQuery._normalize("HAK-180") == "hak180"  # 连字符不再构成差异
    assert NodePrepareQuery._normalize("HAK_180") == "hak180"  # 下划线同上
    assert NodePrepareQuery._normalize(None) == ""
    # 与闲聊闸同一口径：同一句话不能在本库词表探测里算“命中实体”、在对齐里算“查无此型”
    for value in ("HAK-180", "一加 Ace 6T", "hak180产品安全手册", "XT-1"):
        assert NodePrepareQuery._normalize(value) == normalize_for_match(value)


def test_build_vocab_index_merges_duplicate_canonical_and_normalizes_aliases() -> None:
    vocabulary = [
        {"name": "HAK-180", "aliases": ["烫金机", "  "]},
        {"name": "HAK-180", "aliases": ["HAK180"]},  # 同名来自多篇文档 → 合并到同一规范名
        {"name": "   ", "aliases": []},  # 空名丢弃
    ]
    index = NodePrepareQuery._build_vocab_index(vocabulary)
    assert [canonical for canonical, _ in index] == ["HAK-180"]
    keys = dict(index)["HAK-180"]
    # R4 统一口径后“HAK-180”与别名“HAK180”归一化成同一个键（去连字符）；
    # 但回填给 Milvus 的规范名仍是原样串（item_name 存的就是它），只有匹配键被归一化。
    assert keys == {"hak180", "烫金机"}


def test_build_vocab_index_tolerates_bad_shapes() -> None:
    assert NodePrepareQuery._build_vocab_index(None) == []
    assert NodePrepareQuery._build_vocab_index("not-a-list") == []
    assert NodePrepareQuery._build_vocab_index([{"no_name": 1}, "junk"]) == []


# --------------------------------------------------------------------------
# 二、实体对齐决策树（命中过滤 / 唯一候选直采 / 多候选反问 / 型号码缺失拒答 / best-effort）
# --------------------------------------------------------------------------

def test_required_hit_by_canonical_sets_item_names(node) -> None:
    state = node.process(_required_state("HAK-180 报错 E12 怎么处理", [{"name": "HAK-180", "aliases": []}]))
    assert state["item_names"] == ["HAK-180"]
    assert not state.get("answer")  # 命中不反问、不拒答，带过滤继续检索


def test_required_hit_by_alias_resolves_to_canonical_name(node) -> None:
    # "烫金机"不会被正则抽到，用打桩的 LLM 抽取缝提供该提及；别名命中也要回填成规范名，
    # 因为 Milvus 的 item_name 存的是规范名
    node._call_entity_llm = lambda text, history_text: ["烫金机"]
    state = node.process(_required_state("烫金机怎么换膜", [{"name": "HAK-180", "aliases": ["烫金机"]}]))
    assert state["item_names"] == ["HAK-180"]
    assert not state.get("answer")


def test_required_single_fuzzy_candidate_filters_directly_without_clarify(node) -> None:
    # 症状1 修复：唯一模糊候选（"ace6t" → "一加 Ace 6T"）直接带过滤检索，不再反问
    node._call_entity_llm = lambda text, history_text: ["ace6t"]
    state = node.process(_required_state("ace6t", [{"name": "一加 Ace 6T", "aliases": []}]))
    assert state["item_names"] == ["一加 Ace 6T"]  # 回填规范名，供 Milvus 字面命中
    assert not state.get("answer")


def test_required_multiple_fuzzy_candidates_ask_clarification(node) -> None:
    # 只有多个模糊候选才是真歧义，此时才反问（"iPhone 16" 不会被正则抽到，用打桩提供）
    node._call_entity_llm = lambda text, history_text: ["iPhone 16"]
    vocabulary = [{"name": "iPhone 16 Pro", "aliases": []}, {"name": "iPhone 16 Plus", "aliases": []}]
    state = node.process(_required_state("iPhone 16 怎么样", vocabulary))
    assert state["item_names"] == []  # 多解不能拿模糊命中去硬过滤
    answer = state.get("answer") or ""
    assert "您是想问" in answer and "iPhone 16 Pro" in answer and "iPhone 16 Plus" in answer


def test_required_brand_mention_falls_back_to_best_effort_not_refuse(node) -> None:
    # 症状2 修复：品牌词 "一加"（被长度闸挡在候选外）不是型号代码 → 不拒答，退回不带过滤检索
    node._call_entity_llm = lambda text, history_text: ["一加"]
    vocabulary = [{"name": "一加 Ace 6T", "aliases": []}, {"name": "一加 13", "aliases": []}]
    state = node.process(_required_state("一加", vocabulary))
    assert state["item_names"] == []
    assert not state.get("answer")  # 绝不能对品牌浏览问题硬拒答


def test_query_names_model_code_requires_presence_in_current_query() -> None:
    # 只认"当前问题里出现的大写连字符型号代码"：中文品牌/错别字/小写碎片不算
    def _st(q: str) -> dict:
        return {"original_query": q, "rewritten_query": q}

    assert NodePrepareQuery._query_names_model_code(_st("APL-005 无法开机"), ["APL-005"]) is True
    assert NodePrepareQuery._query_names_model_code(_st("HAK-999 报错"), ["HAK-999", "一加"]) is True
    assert NodePrepareQuery._query_names_model_code(_st("一加"), ["一加"]) is False
    # 关键：代码只在历史里出现、不在当前问题里 → 不算用户点名，绝不触发拒答
    assert NodePrepareQuery._query_names_model_code(_st("看看一加手机"), ["APL-005", "一加"]) is False


def test_extract_mentions_ignores_entities_in_assistant_history(node) -> None:
    # 回归污染源：上一轮助手罗列过一堆型号代码，本轮正则不得再从历史里把它们抽成用户提及
    state = _required_state("看看一加手机", [{"name": "一加 Ace 6T", "aliases": []}])
    state["history"] = [
        {"role": "user", "text": "都有哪些手机"},
        {"role": "assistant", "text": "型号包括 iPhone 16 Pro Max (APL-005)、APL-007、APL-003 等"},
    ]
    # node fixture 已把 _call_entity_llm 打桩为空，只验证"正则只扫当前问题"
    mentions = node._extract_mentions(state)
    assert not any("APL" in m.upper() for m in mentions)


def test_brand_followup_after_assistant_model_list_is_not_refused(node) -> None:
    # 端到端复现实测场景：助手先列出全部型号，用户再问"看看一加手机" → best-effort，不拒答也不误过滤
    node._call_entity_llm = lambda text, history_text: ["一加"]
    state = _required_state("看看一加手机", [{"name": "一加 Ace 6T", "aliases": []}, {"name": "一加 13", "aliases": []}])
    state["history"] = [
        {"role": "user", "text": "都有哪些手机"},
        {"role": "assistant", "text": "型号包括 一加 Ace 6T、iPhone 16 Pro Max (APL-005)、APL-007 等"},
    ]
    result = node.process(state)
    assert result["item_names"] == []
    assert not result.get("answer")


def test_refusal_guard_uses_current_turn_text_not_the_rewritten_query(node) -> None:
    """R2：拒答判定只看【本轮用户原文】，改写串里回填的型号不能反过来当成"用户点名了查无的型号"。

    上一轮用户自己问过 HAK-999（本库确实没收录），本轮指代追问"它怎么修"：改写把 HAK-999
    补进检索词是对的（否则这一轮无从过滤），但旧逻辑的拒答护栏判定的正是这个改写串，
    于是护栏恒为真 → 回一句"未收录该型号"，用户本轮根本没提任何型号。
    """
    node._call_entity_llm = lambda text, history_text: []
    state = _required_state("它怎么修", [{"name": "HAK-180", "aliases": []}])
    state["history"] = [{"role": "user", "text": "HAK-999 开不了机"}]
    result = node.process(state)
    assert not result.get("answer")  # 退回不带过滤检索，而不是硬拒答
    assert result["item_names"] == []
    assert "HAK-999" in result["rewritten_query"]  # 检索词仍带着历史实体（改写本身没被削弱）
    assert result["query_entity_text"] == "它怎么修"  # 两条通道在状态里分开


def test_rewrite_only_borrows_entities_from_user_history() -> None:
    """R2：指代改写只从 role == "user" 的历史取实体，助手答案里的型号清单一律不借。

    助手回答动辄罗列十几个型号代码，全库唯一的"未收录"代码都可能出现在里面；
    把它们补进本轮检索词，等于让助手上一句说了什么决定本轮能不能查无此型号。
    """
    history = [
        {"role": "user", "text": "都有哪些报错"},
        {"role": "assistant", "text": "常见报错代码有 E-12、F-03、P-21 等"},
    ]
    assert NodePrepareQuery._rewrite_query_with_history("这个报错怎么处理", history) == "这个报错怎么处理"
    # 用户本轮是泛问题、上一轮用户也没点型号：抽取通道与拒答通道都拿不到任何具体代码
    result_state = {"original_query": "这个报错怎么处理", "query_entity_text": "这个报错怎么处理"}
    assert NodePrepareQuery._query_names_model_code(result_state, ["E-12", "F-03"]) is False


def test_rewrite_still_borrows_model_user_asked_last_turn() -> None:
    """R2 的反向约束：收紧 role 之后，真正的指代消解照常工作（用户上一轮自己提的型号仍会回填）。"""
    history = [
        {"role": "assistant", "text": "长按电源键三秒"},
        {"role": "user", "text": "HAK-180 怎么开机"},
    ]
    rewritten = NodePrepareQuery._rewrite_query_with_history("它的固件版本是多少", history)
    assert rewritten.startswith("HAK-180")


def test_coreference_followup_still_filters_by_borrowed_model(node) -> None:
    # 端到端钉住上一条静态约束在图里的效果：追问句本身没有型号，靠历史回填命中词表并带过滤检索
    node._call_entity_llm = lambda text, history_text: []
    state = _required_state("它的固件版本是多少", [{"name": "HAK-180", "aliases": []}])
    state["history"] = [
        {"role": "user", "text": "HAK-180 怎么开机"},
        {"role": "assistant", "text": "长按电源键三秒，E-12、F-03 都可能是报错"},
    ]
    result = node.process(state)
    assert result["item_names"] == ["HAK-180"]
    assert not result.get("answer")


def test_required_unknown_model_is_refused(node) -> None:
    state = node.process(_required_state("HAK-999 无法启动", [{"name": "HAK-180", "aliases": []}]))
    assert state["item_names"] == []
    assert "未收录" in (state.get("answer") or "")


def test_hyphenated_model_code_matches_hyphen_free_vocabulary_name(node) -> None:
    """R4：用户照真实写法问 `HAK-180`，词表规范名是从文件名派生的 `hak180产品安全手册`（天然没有连字符）。

    两侧归一化口径不一致时互不包含 → candidates 空 → 又恰好是收紧档型号代码 → 库里明明
    有这份文档却回一句"未收录与该型号相关的产品文档"。这是比 R2 更容易被真实用户踩到的误拒答。
    """
    state = node.process(_required_state("HAK-180 报错怎么处理", [{"name": "hak180产品安全手册", "aliases": []}]))
    assert not state.get("answer")
    assert state["item_names"] == ["hak180产品安全手册"]  # 回填规范名原样串，供 Milvus 字面命中


def test_required_without_any_mention_narrows_single_product_library(node) -> None:
    """严格档第五档（已拍板）：本库只收录一款时，属性追问不再退裸搜，直接收窄到那款。

    改造前这里是 `item_names == []`（抽不到实体就退回不带过滤检索）。单款库里本来就只有一个
    对象，继续搜等于把“能不能收窄”交给生成阶段猜，而库里只有一篇时它根本猜不错。
    """
    state = node.process(_required_state("怎么连接电源适配器", [{"name": "HAK-180", "aliases": []}]))
    assert state["item_names"] == ["HAK-180"]
    assert not state.get("answer")  # 收窄不是反问也不是拒答


def test_required_without_any_mention_asks_when_library_has_several(node) -> None:
    """多款库同一句泛问题：拿不准是哪个对象就问一句（候选清单会落到会话历史供下一轮认）。"""
    state = node.process(_required_state("怎么连接电源适配器", PHONE_VOCABULARY))
    assert state["item_names"] == []
    assert "您想问哪个产品" in (state.get("answer") or "")
    assert state["entity_clarify_candidates"] == ["一加 Ace 6T", "iPhone 18 Pro", "Redmi K80"]


def test_required_with_empty_vocabulary_falls_back_to_unfiltered(node) -> None:
    # 本库暂无可对齐实体（如全部导入失败）→ 不能把正常问题误判为拒答
    state = node.process(_required_state("HAK-180 报错", []))
    assert state["item_names"] == []
    assert not state.get("answer")


# --------------------------------------------------------------------------
# 二·五、R7：本轮只说到品牌时，拿品牌组把检索收窄到本库那一款
# --------------------------------------------------------------------------

# 照库 33“手机商品介绍”的真实词表搭的假数据（规范名与 Milvus 的 item_name 逐字一致）
PHONE_VOCABULARY = [
    {"name": "一加 Ace 6T", "aliases": []},
    {"name": "iPhone 18 Pro", "aliases": ["iPhone 18"]},
    {"name": "Redmi K80", "aliases": []},
]


def test_brand_only_mention_narrows_to_the_single_product_of_that_brand(node) -> None:
    """`苹果手机` 与规范名 `iPhone 18 Pro` 字面永不相交，但品牌组能对上：唯一命中就上过滤。"""
    state = node.process(_required_state("有苹果手机吗", PHONE_VOCABULARY))
    assert state["item_names"] == ["iPhone 18 Pro"]
    assert not state.get("answer")  # 收窄不是拒答也不是反问


def test_brand_followup_after_backfill_narrows_too(node) -> None:
    """漂移现场那一例：第二句本轮没主语，靠 M4 回填的 `苹果手机` 也要能收窄。"""
    state = _required_state("有什么存储类型", PHONE_VOCABULARY)
    state["history"] = [{"role": "user", "text": "有苹果手机吗"}]
    result = node.process(state)
    assert result["item_names"] == ["iPhone 18 Pro"], "第二句不能再拿别家型号的存储参数来答"
    assert "iPhone 18 Pro" in result["rewritten_query"]  # M5 跟着把规范名拼进检索词
    assert "苹果手机" in result["rewritten_query"]  # M4 回填还在（同一口径，不是又拼一份）
    assert result["query_entity_text"] == "有什么存储类型"  # R2：原文通道不吃回填


def test_two_products_of_the_same_brand_is_not_guessed(node) -> None:
    """同品牌下了两款就不猜：拿猜测收窄会把答案钉到用户没问的那一款上。"""
    vocabulary = [{"name": "iPhone 18 Pro", "aliases": []}, {"name": "iPad Air", "aliases": []}]
    state = node.process(_required_state("有苹果手机吗", vocabulary))
    assert state["item_names"] == []
    assert not state.get("answer")


def test_a_numbered_mention_never_gets_brand_substituted(node) -> None:
    """点名了 iPhone 17（本库没有）也不能被品牌组换成 18：宁可退回不带过滤检索。"""
    node._call_entity_llm = lambda text, history_text: ["iPhone 17"]
    state = node.process(_required_state("iPhone 17 怎么样", PHONE_VOCABULARY))
    assert state["item_names"] == []
    assert not state.get("answer")


def test_brand_group_reads_canonical_names_not_aliases(node) -> None:
    """只比规范名：实测别名里混着 `LPDDR5X`/`NFC` 这类规格噪声，比别名会圈进不相干实体。"""
    node._call_entity_llm = lambda text, history_text: ["小米"]
    vocabulary = [{"name": "Redmi K80", "aliases": []}, {"name": "配件手册", "aliases": ["小米手机"]}]
    state = node.process(_required_state("小米有什么颜色", vocabulary))
    assert state["item_names"] == ["Redmi K80"], "别名里写不写品牌都不该影响收窄"


def test_brand_narrowing_can_be_switched_back_off(node, monkeypatch) -> None:
    """与 DRIFT_GUARD_DISABLE/IMAGE_ALT_VETO_DISABLE 同族：线上怀疑收窄错了可一键退回旧行为。"""
    monkeypatch.setenv(BRAND_GROUP_FILTER_DISABLE_ENV, "1")
    state = node.process(_required_state("有苹果手机吗", PHONE_VOCABULARY))
    assert state["item_names"] == []
    assert not state.get("answer")


# --------------------------------------------------------------------------
# 三、零回归：disabled 库整段跳过，前置校验答案不被覆盖
# --------------------------------------------------------------------------

def test_disabled_library_never_filters_or_refuses(node) -> None:
    state = {
        "original_query": "HAK-999 无法启动",  # 换成缺型号也不该在 disabled 库拒答
        "library_id": 7,
        "entity_recognition_mode": "disabled",
        "entity_vocabulary": [{"name": "HAK-180", "aliases": []}],
    }
    result = node.process(state)
    assert result["item_names"] == []
    assert not result.get("answer")  # disabled 库不会因为实体逻辑产生反问/拒答


def test_alignment_skipped_when_validation_already_answered(node) -> None:
    # 空问题：前置校验给出提示后不得再走实体逻辑（否则会覆盖成拒答/反问）
    state = node.process(_required_state("", [{"name": "HAK-180", "aliases": []}]))
    assert state["answer"] == "请输入要查询的问题。"
    assert state["item_names"] == []


# --------------------------------------------------------------------------
# 四、LLM 抽取缝本身：容错解析 + 异常吞掉
# --------------------------------------------------------------------------

def test_parse_entities_tolerates_shapes() -> None:
    assert NodePrepareQuery._parse_entities('{"entities": ["A", " ", "B"]}') == ["A", "B"]
    assert NodePrepareQuery._parse_entities('["X", "Y"]') == ["X", "Y"]
    assert NodePrepareQuery._parse_entities([{"text": "no"}]) == []
    assert NodePrepareQuery._parse_entities("not json") == []
    assert NodePrepareQuery._parse_entities(None) == []


def test_call_entity_llm_swallows_failures_and_returns_empty(monkeypatch) -> None:
    prepared = NodePrepareQuery()

    def boom(*args, **kwargs):
        raise TimeoutError("LLM 超时")

    monkeypatch.setattr("utils.llm_utils.get_llm_client", boom)
    # 抽取失败必须退回空列表，让正则结果继续兜底，而不是把异常抛给整条问答链路
    assert prepared._call_entity_llm("HAK-180 报错", "") == []


# --------------------------------------------------------------------------
# 五、静态守卫：接口注入与状态声明
# --------------------------------------------------------------------------

def test_query_api_injects_entity_mode_at_both_entrypoints() -> None:
    """同步与流式两条链路都要注入识别模式、确认闸档位、词表与元数据画像，只改一条会两个入口行为不一致。"""
    text = API_QUERY.read_text(encoding="utf-8")
    # 两个运行入参都注入模式（同一段字面量），此外 _load_entity_vocabulary 内部还有一处模式判断
    assert text.count('"entity_recognition_mode": str(library.entity_recognition_mode or "disabled")') == 2
    assert text.count('"entity_confirm_mode": str(library.entity_confirm_mode or "strict")') == 2
    assert text.count('"entity_vocabulary": entity_vocabulary') == 2
    assert "async def _load_entity_vocabulary" in text
    # 元数据画像同样必须两入口成对注入：漏一个不是报错，而是“同一个问题在同步入口答得出、
    # 在流式入口答不出”——前端默认走流式，那种不一致比都不支持更难查。
    assert text.count('"library_facts": library_facts') == 2
    assert text.count("await _load_library_facts_if_needed(") == 2


def test_readiness_scope_is_defined_in_one_place_only() -> None:
    """“ready + 未删”这套口径只允许住在一处。

    它原本在 query.py、mysql.py 以及两份离线脚本里各抄了一遍，改一处忘三处的表现不是报错，
    而是“清单里混进了待确认和已删除的文档”——用户会拿那个数去核对。接口层现在三处都引
    `READY_DOCUMENT_CLAUSES`（词表装载、来源回填、元数据画像），字面量只能在画像层出现一次
    （eval/ 那两份是离线脚本、自带连库代码，不在本守卫范围内）。

    只钉 `status == "ready"` 而不钉 `deleted_at.is_(None)`：后者在“文档列表要不要含已删”
    这类与就绪口径无关的查询里本来就该出现，把它钉死会让守卫误报并诱使人绕开它。
    """
    facts_text = API_LIBRARY_FACTS.read_text(encoding="utf-8")
    assert 'Document.status == "ready"' in facts_text
    assert "Document.deleted_at.is_(None)" in facts_text
    for path in (API_QUERY, API_MYSQL):
        text = path.read_text(encoding="utf-8")
        assert "*READY_DOCUMENT_CLAUSES" in text, f"{path.name} 还在自带就绪口径"
        assert 'Document.status == "ready"' not in text, \
            f"{path.name} 里又写了一遍就绪条件，它应当只住在 library_facts.py"


def test_clarify_candidates_reach_the_session_history() -> None:
    """反问候选必须真的写进历史：下一轮“这句是不是在回答反问”只认 `clarify_candidates` 字段。

    只钉“接口把候选递出去了”这一件事（不连 MongoDB）：三个落点（写入函数参数、两个端点的
    调用）任一被改回“只存正文”，闭环就只能在单轮内生效，而反问本质上是个跳轮的行为。
    """
    api_text = API_QUERY.read_text(encoding="utf-8")
    history_text = API_HISTORY.read_text(encoding="utf-8")
    assert "clarify_candidates: List[str] = None" in history_text
    assert '"clarify_candidates": [str(c) for c in (clarify_candidates or [])' in history_text
    assert "clarify_candidates=list(clarify_candidates or [])" in api_text
    assert api_text.count("clarify_candidates=") >= 2  # 同步端点与流式端点各一处


def test_stream_completion_carries_clarify_candidates() -> None:
    """流式端点在 `event_generator` 里落库，候选得先装进 workflow_complete 事件才能递到那一步。"""
    text = API_QUERY.read_text(encoding="utf-8")
    assert '"clarify_candidates": [' in text


def test_entity_alignment_state_channels_are_declared() -> None:
    """LangGraph 会静默丢弃未声明的状态键——模式与词表通道必须显式声明。

    R2 的 `query_entity_text`（拒答判定的原文通道）与 R3 的 `entity_filter_relaxed`
    （过滤放宽标记）丢了键都不会报错，只会让护栏退化成旧行为、让放宽在日志里消失。
    """
    declared = typed_dict_keys(QUERY_STATE, "QueryGraphState")
    assert {
        "entity_recognition_mode", "entity_vocabulary", "item_names", "answer",
        "query_entity_text", "entity_filter_relaxed",
        # 阶段一：库级确认闸档位与本轮反问候选（TypedDict 没声明会被 LangGraph 静默丢弃）
        "entity_confirm_mode", "entity_clarify_candidates",
    } <= declared
