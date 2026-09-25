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

from processor.query_processor.nodes.node_prepare_query import NodePrepareQuery

QUERY_STATE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"
API_QUERY = PROJECT_ROOT / "backend" / "app" / "api" / "query.py"


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

def test_normalize_strips_all_whitespace_and_casefolds() -> None:
    # 必须与导入侧 node_persist_chunks._normalize_entity_name 同规则，否则两侧对不齐
    assert NodePrepareQuery._normalize("HUAWEI MateBook D 14") == "huaweimatebookd14"
    assert NodePrepareQuery._normalize("  烫金机\nPro ") == "烫金机pro"
    assert NodePrepareQuery._normalize(None) == ""


def test_build_vocab_index_merges_duplicate_canonical_and_normalizes_aliases() -> None:
    vocabulary = [
        {"name": "HAK-180", "aliases": ["烫金机", "  "]},
        {"name": "HAK-180", "aliases": ["HAK180"]},  # 同名来自多篇文档 → 合并到同一规范名
        {"name": "   ", "aliases": []},  # 空名丢弃
    ]
    index = NodePrepareQuery._build_vocab_index(vocabulary)
    assert [canonical for canonical, _ in index] == ["HAK-180"]
    keys = dict(index)["HAK-180"]
    assert keys == {"hak-180", "烫金机", "hak180"}


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


def test_required_unknown_model_is_refused(node) -> None:
    state = node.process(_required_state("HAK-999 无法启动", [{"name": "HAK-180", "aliases": []}]))
    assert state["item_names"] == []
    assert "未收录" in (state.get("answer") or "")


def test_required_without_any_mention_falls_back_to_unfiltered(node) -> None:
    # 抽不到实体时不臆断也不拒答：不带过滤继续检索，避免误伤泛问题
    state = node.process(_required_state("怎么连接电源适配器", [{"name": "HAK-180", "aliases": []}]))
    assert state["item_names"] == []
    assert not state.get("answer")


def test_required_with_empty_vocabulary_falls_back_to_unfiltered(node) -> None:
    # 本库暂无可对齐实体（如全部导入失败）→ 不能把正常问题误判为拒答
    state = node.process(_required_state("HAK-180 报错", []))
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
    """同步与流式两条链路都要注入识别模式并载入词表，只改一条会让两个入口行为不一致。"""
    text = API_QUERY.read_text(encoding="utf-8")
    # 两个运行入参都注入模式（同一段字面量），此外 _load_entity_vocabulary 内部还有一处模式判断
    assert text.count('"entity_recognition_mode": str(library.entity_recognition_mode or "disabled")') == 2
    assert text.count('"entity_vocabulary": entity_vocabulary') == 2
    assert "async def _load_entity_vocabulary" in text
    # 词表只取就绪且未删除文档，避免命中已下线实体却召不回切片
    assert 'Document.status == "ready"' in text and "Document.deleted_at.is_(None)" in text


def test_entity_alignment_state_channels_are_declared() -> None:
    """LangGraph 会静默丢弃未声明的状态键——模式与词表通道必须显式声明。"""
    declared = typed_dict_keys(QUERY_STATE, "QueryGraphState")
    assert {"entity_recognition_mode", "entity_vocabulary", "item_names", "answer"} <= declared
