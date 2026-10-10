"""实体对齐出口埋点的守卫（第 1 件事"先量后治"的守护栏）。

这批改动只加"走了哪条出口"的记录，不加任何判定。所以守卫分三类：

1. 枚举全等：出口取值集合必须是 18 格且不多不少——分布表靠字面量对齐，有人加一格或
   改了名，统计会静默错位（与 RETRIEVAL_FAILURE_MARKERS 同一手法）。
2. 写入唯一：两个诊断键只能在 `_mark_align_exit` 一处写。十八条分支各写一份必然漏写。
3. 零行为影响：诊断键不得被接口/前端读到；同时用行为用例证明每格确实被对应分支落对
   （静态守卫只能证明"有人写"，证明不了"写在正确的分支上"）。

阶段一把 `_align_entity` 分成了两条链（严格档五道闸 + 反问闭环 / 宽松档即改造前基线），
所以本文件分两段：下面那批原有行为用例全部跑在**宽松档**上（它们是“改造前那批题不能弄坏”
的钉），严格档的新出口另起一节。

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
    state_key_writes,
    typed_dict_keys,
)
from processor.query_processor.nodes.node_prepare_query import (
    CLARIFY_TEMPLATE,
    NO_ENTITY_CLARIFY_TEMPLATE,
    REFUSE_TEMPLATE,
    UNLISTED_BRAND_TEMPLATE,
    NodePrepareQuery,
)

PREPARE_NODE = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_prepare_query.py"
STATE_FILE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"
EXIT_KEY = "entity_align_exit"
SIGNALS_KEY = "entity_align_signals"

# 期望的 18 格出口（11 格旧链 + 7 格严格档）。增删必须同时改这里、改统计口径文档、
# 改跑批脚本 EXIT_LABELS 的列定义，否则报表那一行会写着"未在标签表里"。
EXPECTED_EXITS = {
    "hit_exact", "single_candidate", "clarify", "refuse",
    "brand_narrowed", "brand_giveup", "no_brand_group",
    "no_mention", "empty_vocab", "not_required", "pre_gated",
    # 阶段一严格档
    "clarify_answered", "unlisted_brand", "unlisted_model", "multi_mention_defer",
    "confirm_clarify", "single_product_narrowed", "degraded_answer",
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


def _declared_exit_values() -> set[str]:
    """读 `ENTITY_ALIGN_EXITS` 里列出的取值集合。

    不用 `graph_introspection.collection_literal`：那个助手只认字面量字符串，而这里列的是
    `EXIT_*` 常量名，拿它读会得到空集、于是“全等”断言恒假（上一轮就是这么错红的）。
    自己映射常量表：改名跟常量走，只钉“枚举里不能多也不能少”这一件事。
    """
    constants = _exit_constants()
    tree = ast.parse(PREPARE_NODE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        container: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            targets, container = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, container = [node.target], node.value
        if not any(isinstance(t, ast.Name) and t.id == "ENTITY_ALIGN_EXITS" for t in targets):
            continue
        assert container is not None and isinstance(container, (ast.Tuple, ast.List, ast.Set)), \
            "ENTITY_ALIGN_EXITS 不再是字面量集合，请同步更新本守卫"
        values: set[str] = set()
        for element in container.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                values.add(element.value)
                continue
            assert isinstance(element, ast.Name) and element.id in constants, \
                f"ENTITY_ALIGN_EXITS 里出现了未定义的常量名：{ast.dump(element)}"
            values.add(constants[element.id])
        return values
    raise AssertionError("node_prepare_query 里找不到 ENTITY_ALIGN_EXITS")


# ---------------------------------------------------------------- 一、静态守卫


def test_exit_enum_is_exactly_eighteen_values() -> None:
    """出口取值必须与预期全等：少一格说明有分支没记，多一格说明有人改了口径却没改分析。"""
    assert _declared_exit_values() == EXPECTED_EXITS


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
    """诊断字段不得进接口与前端：一旦有人拿它做分支，"只记录、不改行为"这个前提就没了。

    扫的是两个**完整键名**而不是 `entity_align` 这个前缀：后台统计接口里有一句注释写的
    `alignment_effective`，与查询侧 `_entity_alignment_available` 同名字形，拿前缀扫会把它
    当成“接口读了诊断字段”误报（真漏与假漏混在一个报表里，下次就没人看这个守卫了）。
    """
    root = PROJECT_ROOT / scan_dir
    if not root.exists():
        pytest.skip(f"{scan_dir} 不存在")
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".ts", ".vue", ".js"}
        and any(key in path.read_text(encoding="utf-8", errors="ignore")
                for key in (EXIT_KEY, SIGNALS_KEY))
    ]
    assert not offenders, f"读取了诊断字段的文件：{offenders}"


# ---------------------------------------------------------------- 二、行为：每格确实落对


def _align_state(vocabulary: list[dict], query: str, confirm_mode: str = "lenient") -> dict:
    """构造一个只够跑 `_align_entity` 的 state。

    默认给**宽松档**：下面那批用例是改造前基线的钉（阶段零的一票否决项：本轮逐字命中答对
    的题与靠历史锚答对的追问都不能弄坏），必须按旧判定顺序跑。严格档另起一节。
    """
    return {
        "entity_recognition_mode": "required",
        "entity_confirm_mode": confirm_mode,
        "entity_vocabulary": vocabulary,
        "query_entity_text": query,
        "rewritten_query": query,
        "original_query": query,
        "item_names": [],
    }


def _align(mentions: list[str], vocabulary: list[dict], query: str, monkeypatch,
           confirm_mode: str = "lenient", history: list | None = None) -> dict:
    monkeypatch.setattr(NodePrepareQuery, "_extract_mentions", lambda self, state: list(mentions))
    state = _align_state(vocabulary, query, confirm_mode)
    if history is not None:
        state["history"] = history
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
    # 拒答句从等值改为 startswith：本阶段它在后面追加了本库实有清单的前 3 项（报错型号的用户
    # 看见名单就知道该改报哪个）。追加的是措辞，不是判定——“该不该拒答”仍由收紧档型号代码那条决定。
    assert state["answer"].startswith(REFUSE_TEMPLATE)
    assert "本库收录了：iPhone 15、iPhone 15 Pro、iPhone 18 Pro。" in state["answer"]
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
    for gate in ("_meta_instruction_gate", "_chitchat_gate", "_metadata_gate"):
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
        ("_metadata_gate", "metadata"),
    ],
)
def test_pre_gates_mark_pre_gated_not_bare_search(
        monkeypatch, offline_prepare, gate: str, expected_label: str) -> None:
    """前置闸短路必须落 pre_gated：落成 no_mention/裸搜会把"没走到对齐"统计成"对齐后没命中"。

    假闸也得出预置 answer：真的那三道闸命中时都带一句固定话术，`_seal_short_circuit` 只对有
    answer 的请求兑空来源（J2）；光 return True 不预置的话，下面那条 `sources in state` 验的
    就不是短路行为而是空字典。
    """

    def _answering_gate(self, state):  # noqa: ARG001 - 与真闸同签名
        state["answer"] = "（假闸预置的固定话术）"
        return True

    monkeypatch.setattr(NodePrepareQuery, gate, _answering_gate)
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

    invalid = {"original_query": "HAK-180 怎么调本", "library_id": None,
               "entity_recognition_mode": "required"}
    NodePrepareQuery().process(invalid)
    assert invalid[EXIT_KEY] == "pre_gated"
    assert invalid["answer"] == "请选择有效的知识库后再进行查询。"


# ---------------------------------------------------------------- 四、严格档（阶段一新增七格）


@pytest.fixture(autouse=True)
def _no_confirm_gate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉三个阈值开关：.env 里留着 `ENTITY_CONFIRM_GATE_DISABLE=1` 的开发机上，
    严格档用例会整段落到旧链并集体假绿。出口枚举守卫只验“有人写”，验不了档位。"""
    for name in ("ENTITY_CONFIRM_GATE_DISABLE", "ENTITY_CLARIFY_MAX", "ENTITY_CLARIFY_CANDIDATE_CAP"):
        monkeypatch.delenv(name, raising=False)


# 上一轮系统发了反问、还正等回答：候选只从 `clarify_candidates` 字段读（不从正文回捞）
CLARIFY_ASKED_HISTORY = [
    {"role": "user", "text": "苹果15 电池多大"},
    {
        "role": "assistant",
        "text": "您是想问以下哪个产品：iPhone 15、iPhone 15 Pro？请补充更明确的型号或产品名。",
        "clarify_candidates": ["iPhone 15", "iPhone 15 Pro"],
    },
]


def test_strict_brand_level_question_neither_narrows_nor_asks(monkeypatch) -> None:
    """4.8 第 2 条：`有苹果手机吗` 问的是库里有什么而不是哪一款 → 不收窄也不反问。"""
    state = _align(["苹果手机"], IPHONE_LIBRARY, "有苹果手机吗", monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "brand_giveup"
    assert not state.get("answer")
    assert state["item_names"] == []


def test_strict_ambiguous_writing_asks_with_family_reason(monkeypatch) -> None:
    """写法本身可指同族多款（`苹果15`）：逐字没命中、模糊候选也空，只能拿同族名单去问。

    问完还必须把候选落到 `entity_clarify_candidates`：那是下一轮认“这句在回答反问”的唯一依据。
    """
    state = _align(["苹果15"], IPHONE_LIBRARY, "苹果15 电池多大", monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "clarify"
    assert state[SIGNALS_KEY]["clarify_reason"] == "family_ambiguous"
    assert state["entity_clarify_candidates"] == ["iPhone 15", "iPhone 15 Pro"]
    assert state["answer"] == CLARIFY_TEMPLATE.format(candidates="iPhone 15、iPhone 15 Pro")
    assert state["item_names"] == []


def test_strict_unlisted_brand_wins_before_similarity(monkeypatch) -> None:
    """4.8 第 1 条：未收录品牌必须排在任何形状/相似度采纳之前（实测 `小米 15` 会被认成 `iPhone 15`）。"""
    state = _align(["小米 15"], IPHONE_LIBRARY, "小米 15 电池多大", monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "unlisted_brand"
    assert state["answer"] == UNLISTED_BRAND_TEMPLATE.format(brand="小米")
    assert state["item_names"] == []


def test_strict_unlisted_model_never_substitutes_another_model(monkeypatch) -> None:
    """品牌本库有、那一代没收录（`iPhone 17`）：既不替用户换成 18，也不明知故问，退裸搜。"""
    vocabulary = [{"name": "iPhone 18 Pro", "aliases": []}, {"name": "一加 Ace 6T", "aliases": []}]
    state = _align(["iPhone 17"], vocabulary, "iPhone 17 怎么样", monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "unlisted_model"
    assert not state.get("answer")
    assert state["item_names"] == []


def test_strict_two_local_models_are_not_filtered_to_one(monkeypatch) -> None:
    """一句里点了两个本库型号却只对上其中一个 → 不收窄（本轮错二）。

    两个都逐字对上时不管：多值筛选本来就是通的。
    """
    both = _align(["iPhone 15", "iPhone 18 Pro"], IPHONE_LIBRARY,
                  "iPhone 15 和 iPhone 18 Pro 哪个轻", monkeypatch, confirm_mode="strict")
    assert both[EXIT_KEY] == "hit_exact"
    assert both["item_names"] == ["iPhone 15", "iPhone 18 Pro"]

    half = _align(["iPhone 15", "18 Pro"], IPHONE_LIBRARY,
                  "iPhone 15 和 18 Pro 哪个轻", monkeypatch, confirm_mode="strict")
    assert half[EXIT_KEY] == "multi_mention_defer"
    assert half["item_names"] == []
    assert not half.get("answer")


def test_strict_error_code_is_not_counted_as_second_model(monkeypatch) -> None:
    """一票否决项：`HAK-180 报错 E12` 里的 E12 是报错码，不能当成第二个型号把收窄拆掉。"""
    vocabulary = [{"name": "HAK-180", "aliases": []}]
    state = _align(["HAK-180", "E12"], vocabulary, "HAK-180 报错 E12 怎么处理",
                   monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "hit_exact"
    assert state["item_names"] == ["HAK-180"]


def test_strict_multi_writing_of_one_model_is_not_counted_twice(monkeypatch) -> None:
    """实测抽取形状：一个型号会被拆成 `x1`/`gen12`/全名三条，计数前必须先合并。"""
    vocabulary = [{"name": "ThinkPad X1 Carbon Gen12", "aliases": []}]
    state = _align(["x1", "gen12", "thinkpad x1 carbon gen12"], vocabulary,
                   "thinkpad x1 carbon gen12 多少钱", monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "hit_exact"
    assert state["item_names"] == ["ThinkPad X1 Carbon Gen12"]


def test_strict_protocol_token_is_not_a_model(monkeypatch) -> None:
    """阶段零第 4 条：`wifi7` 与型号同形且分数带重叠，只能黑名单挡；摘完就当这句没写型号。"""
    vocabulary = [{"name": "HAK-180", "aliases": []}]
    state = _align(["wifi7"], vocabulary, "支持 wifi7 吗", monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "single_product_narrowed"
    assert "wifi7" not in state["item_names"]


def test_strict_missing_entity_narrows_single_product_library(monkeypatch) -> None:
    """第五档·单款库（已拍板）：库里就一个对象，不问，直接收窄到那款。"""
    state = _align([], [{"name": "HAK-180", "aliases": []}], "怎么连接电源适配器",
                   monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "single_product_narrowed"
    assert state["item_names"] == ["HAK-180"]
    assert not state.get("answer")


def test_strict_missing_entity_asks_with_library_roster(monkeypatch) -> None:
    """第五档·多款库：这句没写出型号也要问（与老项目的强制确认对齐），并把整库名单列出去。"""
    state = _align([], IPHONE_LIBRARY, "有什么存储类型", monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "confirm_clarify"
    assert state["answer"] == NO_ENTITY_CLARIFY_TEMPLATE.format(
        candidates="iPhone 15、iPhone 15 Pro、iPhone 18 Pro")
    assert state["entity_clarify_candidates"] == ["iPhone 15", "iPhone 15 Pro", "iPhone 18 Pro"]
    # 反问短路仍要遵守 J2
    assert NodePrepareQuery()._seal_short_circuit(state)["sources"] == []  # type: ignore[arg-type]


def test_strict_borrowed_anchor_from_history_still_filters(monkeypatch) -> None:
    """一票否决项：本轮没写型号、型号是改写从上一轮用户话里借来的 → 照旧按它收窄。

    既不能退成裸搜（那会回到“属性追问拿到别家型号参数”的现场），也不能拿本库名单去问
    已经点过名的用户（明知故问）。与 R2 同一口径：借来的串不算“本轮点名”。
    """
    monkeypatch.setattr(NodePrepareQuery, "_extract_mentions", lambda self, state: ["HAK-180"])
    state = _align_state([{"name": "HAK-180", "aliases": []}], "它的固件版本是多少", "strict")
    state["rewritten_query"] = "HAK-180 它的固件版本是多少"
    NodePrepareQuery()._align_entity(state)  # type: ignore[arg-type]
    assert state[EXIT_KEY] == "hit_exact"
    assert state["item_names"] == ["HAK-180"]
    assert not state.get("answer")
    assert state[SIGNALS_KEY]["borrowed_mentions"] == ["HAK-180"]


def test_strict_clarify_answer_is_confirmed_and_predicate_inherited(monkeypatch) -> None:
    """闭环第 2-3 步：只甩一句话（“第二个”）也要认下来，并把上一轮的谓词接回检索词。"""
    state = _align(["15 Pro"], IPHONE_LIBRARY, "第二个", monkeypatch,
                   confirm_mode="strict", history=CLARIFY_ASKED_HISTORY)
    assert state[EXIT_KEY] == "clarify_answered"
    assert state["item_names"] == ["iPhone 15 Pro"]
    assert not state.get("answer")
    assert "iPhone 15 Pro" in state["rewritten_query"]
    assert "电池多大" in state["rewritten_query"]
    assert state[SIGNALS_KEY]["predicate_inherited"] is True


def test_strict_clarify_answer_by_plain_name_and_by_ordinal(monkeypatch) -> None:
    """报名字与报序号都要能接住：“iPhone 15 Pro”与“第一个”都是同一个候选。"""
    by_name = _align([], IPHONE_LIBRARY, "iPhone 15 Pro", monkeypatch,
                     confirm_mode="strict", history=CLARIFY_ASKED_HISTORY)
    assert by_name[EXIT_KEY] == "clarify_answered"
    assert by_name["item_names"] == ["iPhone 15 Pro"]

    by_ordinal = _align([], IPHONE_LIBRARY, "第一个", monkeypatch,
                        confirm_mode="strict", history=CLARIFY_ASKED_HISTORY)
    assert by_ordinal[EXIT_KEY] == "clarify_answered"
    assert by_ordinal["item_names"] == ["iPhone 15"]


def test_strict_answer_that_switches_model_goes_back_to_normal_flow(monkeypatch) -> None:
    """4.3 第 4 步（最隐蔽的坑）：答的内容不在候选清单里（用户换了型号）→ 不能当成回答反问。"""
    state = _align(["iPhone 18 Pro"], IPHONE_LIBRARY, "iPhone 18 Pro 多少钱", monkeypatch,
                   confirm_mode="strict", history=CLARIFY_ASKED_HISTORY)
    assert state[EXIT_KEY] == "hit_exact"
    assert state["item_names"] == ["iPhone 18 Pro"]


def test_strict_second_clarify_degrades_to_bare_search(monkeypatch) -> None:
    """4.4 防死循环：本会话已经反问过一次，同一句再歧义也不问第二遍，退成不带过滤裸搜。"""
    history = CLARIFY_ASKED_HISTORY + [
        {"role": "user", "text": "第二个"},
        {"role": "assistant", "text": "iPhone 15 Pro 电池 3274mAh", "clarify_candidates": []},
    ]
    state = _align([], IPHONE_LIBRARY, "有什么存储类型", monkeypatch, confirm_mode="strict", history=history)
    assert state[EXIT_KEY] == "degraded_answer"
    assert state[SIGNALS_KEY]["clarify_reason"] == "no_entity"
    assert not state.get("answer")
    assert state["item_names"] == []


def test_strict_clarify_cap_zero_never_asks(monkeypatch) -> None:
    """`ENTITY_CLARIFY_MAX=0` = 从不反问（确认闸降级成只筛选），0 不能被当成“没配”吞掉。"""
    monkeypatch.setenv("ENTITY_CLARIFY_MAX", "0")
    state = _align([], IPHONE_LIBRARY, "有什么存储类型", monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "degraded_answer"
    assert not state.get("answer")


def test_strict_candidate_cap_shows_a_short_roster(monkeypatch) -> None:
    """候选念不完时只列前几个并明说“还有其他型号”：念不完不等于念错。"""
    monkeypatch.setenv("ENTITY_CLARIFY_CANDIDATE_CAP", "2")
    state = _align([], IPHONE_LIBRARY, "有什么存储类型", monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "confirm_clarify"
    assert state["entity_clarify_candidates"] == ["iPhone 15", "iPhone 15 Pro"]
    assert "还有其他型号" in state["answer"]


def test_gate_disable_env_falls_back_to_the_old_chain(monkeypatch) -> None:
    """线上基线对比：`ENTITY_CONFIRM_GATE_DISABLE=1` 时库级字段一律按宽松档走。"""
    monkeypatch.setenv("ENTITY_CONFIRM_GATE_DISABLE", "1")
    state = _align([], IPHONE_LIBRARY, "有什么存储类型", monkeypatch, confirm_mode="strict")
    assert state[EXIT_KEY] == "no_mention"
    assert state[SIGNALS_KEY]["confirm_mode"] == "lenient"


def test_missing_confirm_mode_key_defaults_to_strict(monkeypatch) -> None:
    """接口漏注入一个键不能把闸静默关掉：“看起来上了改造却没效果”比多问一句难发现得多。"""
    monkeypatch.setattr(NodePrepareQuery, "_extract_mentions", lambda self, state: [])
    state = {
        "entity_recognition_mode": "required",
        "entity_vocabulary": IPHONE_LIBRARY,
        "query_entity_text": "有什么存储类型",
        "rewritten_query": "有什么存储类型",
        "original_query": "有什么存储类型",
        "item_names": [],
    }
    NodePrepareQuery()._align_entity(state)  # type: ignore[arg-type]
    assert state[SIGNALS_KEY]["confirm_mode"] == "strict"
    assert state[EXIT_KEY] == "confirm_clarify"

