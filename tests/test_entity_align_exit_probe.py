"""实体对齐出口埋点的守卫（第 1 件事"先量后治"的守护栏）。

这批改动只加"走了哪条出口"的记录，不加任何判定。所以守卫分三类：

1. 枚举全等：出口取值集合必须是 11 格且不多不少——分布表靠字面量对齐，有人加一格或
   改了名，统计会静默错位（与 RETRIEVAL_FAILURE_MARKERS 同一手法）。
2. 写入唯一：两个诊断键只能在 `_mark_align_exit` 一处写。11 个分支各写一份必然漏写。
3. 零行为影响：诊断键不得被接口/前端读到；同时用行为用例证明每格确实被对应分支落对
   （静态守卫只能证明"有人写"，证明不了"写在正确的分支上"）。

`_extract_mentions` 会走 LLM、三个前置闸也可能走小模型，行为用例一律 monkeypatch 掉，
只验编排与打标，不发任何外部调用。
"""
from __future__ import annotations

import ast
import re

import pytest

from graph_introspection import (
    PROJECT_ROOT,
    class_method_names,
    collection_literal,
    state_key_writes,
    typed_dict_keys,
)
from processor.query_processor.nodes.node_prepare_query import (
    CLARIFY_TEMPLATE,
    REFUSE_TEMPLATE,
    NodePrepareQuery,
)

PREPARE_NODE = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_prepare_query.py"
STATE_FILE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"
EXIT_KEY = "entity_align_exit"
SIGNALS_KEY = "entity_align_signals"

# 期望的 11 格出口。增删必须同时改这里、改统计口径文档、改跑批脚本的列定义。
EXPECTED_EXITS = {
    "hit_exact", "single_candidate", "clarify", "refuse",
    "brand_narrowed", "brand_giveup", "no_brand_group",
    "no_mention", "empty_vocab", "not_required", "pre_gated",
}

IPHONE_LIBRARY = [
    {"name": "iPhone 15", "aliases": []},
    {"name": "iPhone 15 Pro", "aliases": []},
    {"name": "iPhone 18 Pro", "aliases": []},
]


def _exit_constants() -> dict[str, str]:
    """取模块级 `EXIT_*` 字符串常量：常量名 -> 取值。"""
    tree = ast.parse(PREPARE_NODE.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id.startswith("EXIT_")
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found[target.id] = node.value.value
    assert found, "node_prepare_query 里找不到任何 EXIT_* 常量，守卫会空过"
    return found


# ---------------------------------------------------------------- 一、静态守卫


def test_exit_enum_is_exactly_eleven_values() -> None:
    """出口取值必须与预期全等：少一格说明有分支没记，多一格说明有人改了口径却没改分析。"""
    assert collection_literal(PREPARE_NODE, "ENTITY_ALIGN_EXITS") == EXPECTED_EXITS


def test_every_enum_value_has_a_write_point() -> None:
    """枚举里不能留"哑值"：定义了却没人写的格子，会让分布表那一列永远空白并被误读成"没有这类问题"。"""
    constants = _exit_constants()
    text = PREPARE_NODE.read_text(encoding="utf-8")
    used = {
        constants.get(name, name)
        for name in re.findall(r"_mark_align_exit\(\s*state,\s*([A-Za-z_][A-Za-z0-9_]*)", text)
    }
    assert set(constants.values()) == EXPECTED_EXITS
    assert used == EXPECTED_EXITS, f"以下出口没有写入点或传的不是 EXIT_* 常量：{sorted(EXPECTED_EXITS - used)}"


def test_state_declares_both_probe_keys() -> None:
    """TypedDict 没声明的键会被 LangGraph 静默丢弃：埋点整格消失且不报错，只能静态钉住。"""
    assert {EXIT_KEY, SIGNALS_KEY} <= typed_dict_keys(STATE_FILE, "QueryGraphState")


def test_probe_keys_written_only_in_the_mark_helper() -> None:
    """两个诊断键只允许 `_mark_align_exit` 一处写：分支各写一份必然漏写，漏一格就把统计带偏。"""
    methods = class_method_names(PREPARE_NODE, "NodePrepareQuery")
    assert "_mark_align_exit" in methods
    assert state_key_writes(PREPARE_NODE, "NodePrepareQuery", "_mark_align_exit") == {
        EXIT_KEY, SIGNALS_KEY,
    }
    offenders = [
        f"{method}: {sorted(written)}"
        for method in sorted(methods - {"_mark_align_exit"})
        if (written := state_key_writes(PREPARE_NODE, "NodePrepareQuery", method))
        & {EXIT_KEY, SIGNALS_KEY}
    ]
    assert not offenders, f"绕开统一写入口的方法：{offenders}"


@pytest.mark.parametrize("scan_dir", ["backend", "frontend/src", "utils"])
def test_probe_keys_do_not_leak_outside_the_query_node(scan_dir: str) -> None:
    """诊断字段不得进接口与前端：一旦有人拿它做分支，"只记录、不改行为"这个前提就没了。"""
    root = PROJECT_ROOT / scan_dir
    if not root.exists():
        pytest.skip(f"{scan_dir} 不存在")
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".ts", ".vue", ".js"}
        and "entity_align" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, f"读取了诊断字段的文件：{offenders}"


# ---------------------------------------------------------------- 二、行为：每格确实落对


def _align_state(vocabulary: list[dict], query: str) -> dict:
    """构造一个只够跑 `_align_entity` 的 state。"""
    return {
        "entity_recognition_mode": "required",
        "entity_vocabulary": vocabulary,
        "query_entity_text": query,
        "rewritten_query": query,
        "original_query": query,
        "item_names": [],
    }


def _align(mentions: list[str], vocabulary: list[dict], query: str, monkeypatch) -> dict:
    monkeypatch.setattr(NodePrepareQuery, "_extract_mentions", lambda self, state: list(mentions))
    state = _align_state(vocabulary, query)
    NodePrepareQuery()._align_entity(state)  # type: ignore[arg-type]
    return state


def test_hit_exact_marks_exit_and_keeps_filter(monkeypatch) -> None:
    state = _align(["一加 Ace 6T"], [{"name": "一加 Ace 6T", "aliases": ["OnePlus Ace 6T"]}],
                   "一加 Ace 6T 电池多大", monkeypatch)
    assert state[EXIT_KEY] == "hit_exact"
    assert state["item_names"] == ["一加 Ace 6T"]
    assert not state.get("answer")
    assert state[SIGNALS_KEY]["confirmed_count"] == 1
    assert state[SIGNALS_KEY]["vocab_size"] == 1


def test_single_candidate_does_not_clarify(monkeypatch) -> None:
    state = _align(["ace6t"], [{"name": "一加 Ace 6T", "aliases": []}], "ace6t 多少钱", monkeypatch)
    assert state[EXIT_KEY] == "single_candidate"
    assert state["item_names"] == ["一加 Ace 6T"]
    assert not state.get("answer")


def test_multiple_candidates_clarify(monkeypatch) -> None:
    state = _align(["iPhone"], IPHONE_LIBRARY, "iPhone 多少钱", monkeypatch)
    assert state[EXIT_KEY] == "clarify"
    assert state["answer"] == CLARIFY_TEMPLATE.format(
        candidates="iPhone 15、iPhone 15 Pro、iPhone 18 Pro")
    assert state[SIGNALS_KEY]["candidate_count"] == 3
    assert state[SIGNALS_KEY]["shown_count"] == 3
    assert not state["item_names"]


def test_named_unknown_model_code_refuses(monkeypatch) -> None:
    state = _align(["APL-999"], IPHONE_LIBRARY, "APL-999 多少钱", monkeypatch)
    assert state[EXIT_KEY] == "refuse"
    assert state["answer"] == REFUSE_TEMPLATE
    assert state[SIGNALS_KEY]["model_code_named"] is True


def test_brand_group_with_two_models_gives_up(monkeypatch) -> None:
    """同品牌 ≥2 款不猜。这一格是"要不要给实体加品类维度"的数据源，必须与"根本不是品牌词"分开。"""
    monkeypatch.delenv("BRAND_GROUP_FILTER_DISABLE", raising=False)
    state = _align(["苹果手机"], IPHONE_LIBRARY, "有苹果手机吗", monkeypatch)
    assert state[EXIT_KEY] == "brand_giveup"
    assert state[SIGNALS_KEY]["brand_matched"] == 3
    assert state[SIGNALS_KEY]["brand_group"] == ["苹果"]
    assert not state["item_names"]


def test_brand_group_with_single_model_narrows(monkeypatch) -> None:
    monkeypatch.delenv("BRAND_GROUP_FILTER_DISABLE", raising=False)
    state = _align(["苹果手机"], [{"name": "iPhone 18 Pro", "aliases": []}], "有苹果手机吗", monkeypatch)
    assert state[EXIT_KEY] == "brand_narrowed"
    assert state["item_names"] == ["iPhone 18 Pro"]
    assert state[SIGNALS_KEY]["brand_matched"] == 1


def test_env_disabled_brand_filter_is_flagged_as_ops_choice(monkeypatch) -> None:
    """开关关掉时算"放弃收窄"，但必须另置 brand_disabled：否则运维选择被统计成数据问题。"""
    monkeypatch.setenv("BRAND_GROUP_FILTER_DISABLE", "1")
    state = _align(["苹果手机"], IPHONE_LIBRARY, "有苹果手机吗", monkeypatch)
    assert state[EXIT_KEY] == "brand_giveup"
    assert state[SIGNALS_KEY]["brand_disabled"] is True
    assert "brand_matched" not in state[SIGNALS_KEY]


def test_typo_mention_is_separated_from_brand_giveup(monkeypatch) -> None:
    """错字/别称（`一家 ace6t`）落 no_brand_group，不混进 brand_giveup——否则
    "先做别名扩充还是先做向量软对齐"这条决策线就没有输入。"""
    state = _align(["一家 ace6t"], [{"name": "一加 Ace 6T", "aliases": []}], "一家 ace6t 多少钱", monkeypatch)
    assert state[EXIT_KEY] == "no_brand_group"
    assert state[SIGNALS_KEY]["brand_group"] == []
    assert not state["item_names"]


def test_no_mention_and_empty_vocab_are_separate_exits(monkeypatch) -> None:
    """⑥的两个半区必须分开：一个是行为问题（靠会话锚治），一个是数据问题（靠重导入治）。"""
    state = _align([], IPHONE_LIBRARY, "有什么存储类型", monkeypatch)
    assert state[EXIT_KEY] == "no_mention"
    assert state[SIGNALS_KEY]["mention_count"] == 0

    state = _align(["iPhone 18 Pro"], [], "iPhone 18 Pro 多少钱", monkeypatch)
    assert state[EXIT_KEY] == "empty_vocab"
    assert state[SIGNALS_KEY]["vocab_size"] == 0


def test_signals_record_ellipsis_and_rewrite(monkeypatch) -> None:
    """省略主语的追问、以及"实体是靠改写补进来的"这两列，是判断硬闸该不该开的主输入。"""
    state = _align([], IPHONE_LIBRARY, "有什么存储类型", monkeypatch)
    assert state[SIGNALS_KEY]["ellipsis_followup"] is True
    assert state[SIGNALS_KEY]["query_rewritten"] is False

    monkeypatch.setattr(NodePrepareQuery, "_extract_mentions", lambda self, s: [])
    aligned = _align_state(IPHONE_LIBRARY, "有什么存储类型")
    aligned["rewritten_query"] = "iPhone 18 Pro 有什么存储类型"
    NodePrepareQuery()._align_entity(aligned)  # type: ignore[arg-type]
    assert aligned[SIGNALS_KEY]["query_rewritten"] is True


def test_clarify_still_seals_empty_sources(monkeypatch) -> None:
    """反问短路仍要遵守 J2：预置了 answer 就必须把 sources 兜成空列表，埋点不能破坏这条。"""
    state = _align(["iPhone"], IPHONE_LIBRARY, "iPhone 多少钱", monkeypatch)
    assert NodePrepareQuery()._seal_short_circuit(state)["sources"] == []  # type: ignore[arg-type]


# ---------------------------------------------------------------- 三、两个早退格


@pytest.fixture
def offline_prepare(monkeypatch: pytest.MonkeyPatch):
    """把 process() 里所有可能发外部调用或参与判定的步骤短路掉，只留编排与打标。"""
    monkeypatch.setattr(NodePrepareQuery, "_rewrite_query_with_history", staticmethod(lambda q, h: q))
    monkeypatch.setattr(NodePrepareQuery, "_backfill_ellipsis_anchor", staticmethod(lambda t, s: t))
    for gate in ("_meta_instruction_gate", "_chitchat_gate", "_inventory_gate"):
        monkeypatch.setattr(NodePrepareQuery, gate, lambda self, state: False)


def test_disabled_library_marks_not_required(offline_prepare) -> None:
    """disabled 库整段没跑对齐：记 not_required，且**不记 mentions**（压根没抽过，填 0 会被读成"抽不到"）。"""
    state: dict = {
        "original_query": "一加 Ace 6T 电池多大",
        "library_id": 1,
        "entity_recognition_mode": "disabled",
    }
    NodePrepareQuery().process(state)
    assert state[EXIT_KEY] == "not_required"
    assert "mention_count" not in state[SIGNALS_KEY]
    assert state["item_names"] == []
    assert not state.get("answer")
    assert "sources" not in state  # 未短路的请求必须不带 sources（同源口径）


@pytest.mark.parametrize(
    "gate,expected_label",
    [
        ("_meta_instruction_gate", "meta_instruction"),
        ("_chitchat_gate", "chitchat"),
        ("_inventory_gate", "inventory"),
    ],
)
def test_pre_gates_mark_pre_gated_not_bare_search(
        monkeypatch, offline_prepare, gate: str, expected_label: str) -> None:
    """前置闸短路必须落 pre_gated：落成 no_mention/裸搜会把"没走到对齐"统计成"对齐后没命中"。"""
    monkeypatch.setattr(NodePrepareQuery, gate, lambda self, state: True)
    state: dict = {
        "original_query": "iPhone 多少钱",
        "library_id": 1,
        "entity_recognition_mode": "required",
    }
    NodePrepareQuery().process(state)
    assert state[EXIT_KEY] == "pre_gated"
    assert state[SIGNALS_KEY]["gate"] == expected_label
    assert state[SIGNALS_KEY]["mode"] == "required"
    assert "sources" in state  # 短路的既有行为不受影响：预置了 answer 就兜空来源


def test_validation_answer_marks_pre_gated(offline_prepare) -> None:
    """空问题/无效库这两个校验出口也走 pre_gated（闸名记 validation），别留成"没有出口标记"。"""
    state: dict = {"original_query": "", "library_id": 1, "entity_recognition_mode": "required"}
    NodePrepareQuery().process(state)
    assert state[EXIT_KEY] == "pre_gated"
    assert state[SIGNALS_KEY]["gate"] == "validation"
    assert state["answer"] == "请输入要查询的问题。"

    invalid = {"original_query": "HAK-180 怎么调温", "library_id": None,
               "entity_recognition_mode": "required"}
    NodePrepareQuery().process(invalid)
    assert invalid[EXIT_KEY] == "pre_gated"
    assert invalid["answer"] == "请选择有效的知识库后再进行查询。"
