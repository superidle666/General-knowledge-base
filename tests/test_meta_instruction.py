"""元指令与对话回指的专门通道（R9-B）的离线守卫。

要解决的两件事（清单 §3 R9 的 B 面）：

① 对话回指（`刚刚我问了你什么`）：问的是**这段对话本身**，不是知识库内容。A 面（不被闲聊闸
吃）已随 R1 落地，但它仍然会拿字面串去跑一次向量检索（必然零召回），再靠 prompt 里的
【历史对话】让模型答对 —— 能答对，却白烧一次检索 + 一次生成，还把噪声切片挤进预算。
正确出口是历史里那条用户原话，本守卫钉住：**零检索零生成**。

② 改写型指令（`换个说法`/`再次回答`/`继续`/`精简点`/`用表格列一下`）：检索词是字面指令 →
整条链路白跑，且刚落地 R5 之后这类请求还可能被"无证据"话术挡掉（用户看到的是"没找到"，
可他其实是在要上一轮答案的另一种说法）。出口是**把检索词换成上一轮那个真问题**，同时把
本轮的呈现要求单独带进 prompt——指令不能进检索词（那正是本条要治的污染），丢了又答不出
表格/精简。

两条都取"宁查勿否"：判定只认**整句就是指令/回指**（fullmatch 语义），句子里带任何实义内容
一律不认，退回现状继续检索。
"""

from __future__ import annotations

import pytest
from graph_introspection import PROJECT_ROOT, typed_dict_keys

from processor.query_processor.meta_instruction import (
    is_conversation_echo,
    is_regeneration_instruction,
)
from processor.query_processor.nodes import node_answer_output as answer_module
from processor.query_processor.nodes import node_prepare_query as node_module
from processor.query_processor.nodes.node_answer_output import (
    INSUFFICIENT_EVIDENCE_REPLY,
    NodeAnswerOutput,
)
from processor.query_processor.nodes.node_prepare_query import NodePrepareQuery

QUERY_STATE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"

PREV_QUESTION = "HAK-180 怎么设置转印温度"
PREV_ANSWER = "转印温度建议设置在 110℃ 左右，先预热三分钟。"
HISTORY = [
    {"role": "user", "text": PREV_QUESTION},
    {"role": "assistant", "text": PREV_ANSWER},
]
VOCABULARY = [{"name": "HAK-180", "aliases": ["烫金机"]}]


def _state(query: str, history=None, mode: str = "disabled", vocabulary=None) -> dict:
    return {
        "original_query": query,
        "library_id": 7,
        "history": list(history or []),
        "entity_recognition_mode": mode,
        "entity_vocabulary": list(vocabulary or []),
    }


@pytest.fixture()
def node(monkeypatch):
    prepared = NodePrepareQuery()
    monkeypatch.setattr(prepared, "_call_entity_llm", lambda text, history_text: [])
    monkeypatch.setattr(node_module, "classify_chitchat_llm", lambda text: (None, ""))
    return prepared


# --------------------------------------------------------------------------
# 一、判定层：只认整句就是回指/指令
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query",
    [
        "刚刚我问了你什么",
        "我之前问的啥",
        "刚才我问的是什么",
        "上一个问题是什么",
        "刚才我问的问题是啥",
        "你刚才的回答是什么",
        "之前我问你来着",
    ],
)
def test_conversation_echo_shapes_are_recognized(query: str) -> None:
    assert is_conversation_echo(query) is True


@pytest.mark.parametrize(
    "query",
    [
        # 带实义内容：问的仍是文档，必须交给检索
        "之前问的保修期是几年",
        "刚刚说的那个报错怎么解决",
        "上次提到的温度上限是多少",
        # 骨架里允许一个“是”，但后面必须紧跟疑问代词；带真内容的“我问的是X”不能吃
        "我问的是温度",
        "我问的是什么东西先关电源",
        # 清单型元问题（R16）与回指是两回事
        "知识库里有哪些产品",
        "HAK-180 怎么设置温度",
    ],
)
def test_questions_with_real_content_are_not_echo(query: str) -> None:
    assert is_conversation_echo(query) is False


@pytest.mark.parametrize(
    "query",
    [
        "换个说法",
        "再次回答",
        "再说一遍",
        "继续",
        "精简点",
        "详细说说",
        "用表格列一下",
        "总结一下",
        "换种说法呢",
        "重新回答上面那个问题",
    ],
)
def test_regeneration_instructions_are_recognized(query: str) -> None:
    assert is_regeneration_instruction(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "换个说法讲讲 HAK-180 的温度设置",
        "用表格列一下华为平板的参数",
        "继续说保修期多久",
        "精简一点讲温度怎么设置",
    ],
)
def test_instructions_carrying_content_are_not_meta(query: str) -> None:
    """带真问题的句子检索词里本来就有内容，不该被本闸改写（漏判只是退回现状）。"""
    assert is_regeneration_instruction(query) is False


# --------------------------------------------------------------------------
# 二、回指型：零检索零生成
# --------------------------------------------------------------------------

def test_echo_is_answered_from_history_without_any_downstream_call(node) -> None:
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)

    state = node.process(_state("刚刚我问了你什么", history=HISTORY))

    answer = str(state.get("answer") or "")
    assert PREV_QUESTION in answer, f"回指句应当把上一轮问题原样回读，实际：{answer!r}"
    assert state["intent"] == "conversation_echo"
    assert state["sources"] == []
    assert called["align"] is False, "回指型命中就不该再花实体抽取调用"


def test_echo_repeats_the_most_recent_user_turn_even_if_it_was_a_refusal(node) -> None:
    """回读的是"你刚才问了什么"这件事本身，哪怕那句是清单型元问题（上一轮被 R16 拒答过），
    回读它仍是正确答案——所以回指通道不能套用改写通道那套"跳过不可答的问题"。"""
    history = [
        {"role": "user", "text": PREV_QUESTION},
        {"role": "assistant", "text": PREV_ANSWER},
        {"role": "user", "text": "知识库里有哪些产品"},
    ]
    state = node.process(_state("刚才我问的是什么", history=history))

    assert "知识库里有哪些产品" in str(state.get("answer") or "")
    assert PREV_QUESTION not in str(state.get("answer") or "")


def test_echo_without_history_is_not_short_circuited(node) -> None:
    """没有可回读的内容就不短路：宁查勿否，退回现状让它走检索。"""
    state = node.process(_state("刚刚我问了你什么"))

    assert not state.get("answer")
    assert state.get("intent") != "conversation_echo"


def test_echo_works_on_disabled_library_too(node) -> None:
    """回指通道与实体识别模式无关（disabled 库同样白跑过一次检索）。"""
    state = node.process(_state("上一个问题是什么", history=HISTORY, mode="disabled"))

    assert state.get("intent") == "conversation_echo"
    assert PREV_QUESTION in str(state.get("answer") or "")


# --------------------------------------------------------------------------
# 三、改写型：换检索词 + 把呈现要求单独带走
# --------------------------------------------------------------------------

def test_rewrite_swaps_retrieval_text_and_keeps_the_instruction(node) -> None:
    state = node.process(_state("用表格列一下", history=HISTORY))

    assert not state.get("answer"), "改写型不该短路，它要重新生成一版"
    assert state["rewritten_query"] == PREV_QUESTION, "字面指令绝不能当检索词"
    assert state["meta_instruction"] == "用表格列一下"
    assert state["intent"] == "meta_rewrite"
    assert state["query_entity_text"] == "用表格列一下", "拒答判定仍吃本轮原文（与 R2 同口径）"


def test_rewrite_skips_previous_turns_that_cannot_be_answered(node) -> None:
    """上一轮若是清单句/指令句，拿它当检索词等于把同一个问题再问一遍；要往前找到能答的那条。"""
    history = [
        {"role": "user", "text": PREV_QUESTION},
        {"role": "assistant", "text": PREV_ANSWER},
        {"role": "user", "text": "知识库里有哪些产品"},
        {"role": "assistant", "text": "当前我无法获取知识库的全部产品。"},
    ]
    state = node.process(_state("换个说法", history=history))

    assert state["rewritten_query"] == PREV_QUESTION


def test_rewrite_without_history_leaves_the_query_untouched(node) -> None:
    state = node.process(_state("继续"))

    assert state["rewritten_query"] == "继续"
    assert not state.get("meta_instruction")
    assert state.get("intent") != "meta_rewrite"


def test_rewrite_feeds_the_aligned_entity_from_the_previous_question(node) -> None:
    """检索词换成上一轮真问题后，实体抽取必须跟着用新词——否则带实体过滤的那一路还是空的。"""
    seen: list[str] = []
    node._call_entity_llm = lambda text, history_text: seen.append(text) or []

    state = node.process(_state("再说一遍", history=HISTORY, mode="required", vocabulary=VOCABULARY))

    assert state["item_names"] == ["HAK-180"], "正则通道也应从上一轮问题里命中本库实体"
    assert seen == [PREV_QUESTION], f"抽取的输入应当是替换后的检索词，实际：{seen}"


def test_meta_gate_runs_before_the_chitchat_llm_gate(node, monkeypatch) -> None:
    """"继续""这种短句无实体信号，正好落在闲聊 L2 门禁里；若先过闲聊闸，元指令会被小模型
    判成闲聊并预置固定回复，用户要的"再来一版"就永远出不来。故元指令闸必须排在它之前。"""
    l2_calls: list[str] = []
    monkeypatch.setattr(node_module, "classify_chitchat_llm", lambda text: l2_calls.append(text) or (None, ""))

    state = node.process(_state("继续", history=HISTORY))

    assert l2_calls == [], "元指令命中后不该再花一次意图分类调用"
    assert state["intent"] == "meta_rewrite"
    assert not state.get("answer")


def test_plain_question_is_not_touched(node) -> None:
    state = node.process(_state("HAK-180 怎么设置温度", history=HISTORY))

    assert state["rewritten_query"] == "HAK-180 怎么设置温度"
    assert not state.get("meta_instruction")
    assert state.get("intent") not in ("conversation_echo", "meta_rewrite")


# --------------------------------------------------------------------------
# 四、生成侧：呈现要求进 prompt，但不新增事实来源
# --------------------------------------------------------------------------

@pytest.fixture()
def answer_node(monkeypatch):
    calls = {"llm": 0}

    class _FakeLLM:
        def invoke(self, prompt):
            calls["llm"] += 1
            return type("Resp", (), {"content": "已按要求重排。"})()

        def stream(self, prompt):  # pragma: no cover
            calls["llm"] += 1
            return iter(())

    monkeypatch.setattr(answer_module, "get_llm_client", lambda: _FakeLLM())
    monkeypatch.setattr(answer_module, "set_task_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(answer_module, "add_done_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(answer_module, "save_chat_message", lambda **kwargs: None)
    return NodeAnswerOutput(), calls


def _answer_state(**overrides) -> dict:
    state = {
        "session_id": "s-r9",
        "original_query": "用表格列一下",
        "rewritten_query": PREV_QUESTION,
        "item_names": [],
        "history": HISTORY,
        "reranked_docs": [{"content": "转印温度建议 110℃，预热三分钟。", "source": "local", "chunk_id": 3, "score": 0.8}],
        "is_stream": False,
        "persist_history": False,
    }
    state.update(overrides)
    return state


def test_presentation_requirement_reaches_the_prompt(answer_node) -> None:
    node, _ = answer_node
    prompt = node._step_2_construct_prompt(_answer_state(meta_instruction="用表格列一下"))

    assert PREV_QUESTION in prompt
    assert "用表格列一下" in prompt


def test_prompt_is_byte_identical_without_an_instruction(answer_node) -> None:
    """没有元指令时 prompt 必须逐字不变：这条通道对既有问答零影响。"""
    node, _ = answer_node
    baseline = node._step_2_construct_prompt(_answer_state())

    assert node._step_2_construct_prompt(_answer_state(meta_instruction="")) == baseline
    assert node._step_2_construct_prompt(_answer_state(meta_instruction=None)) == baseline


def test_rewrite_without_evidence_still_hits_the_r5_guard(answer_node) -> None:
    """R5 与 R9-B 的耦合点：改写型换了检索词但仍是"检索出来的东西说话"，
    重跑没有证据时照常走无证据话术，不能因为"用户只是要换个说法"就绕过守卫。"""
    node, calls = answer_node
    state = node.process(_answer_state(meta_instruction="换个说法", reranked_docs=[]))

    assert state["answer"] == INSUFFICIENT_EVIDENCE_REPLY
    assert calls["llm"] == 0


# --------------------------------------------------------------------------
# 五、静态守卫：新键与新 intent 值必须先声明
# --------------------------------------------------------------------------

def test_meta_instruction_keys_and_intents_are_declared() -> None:
    declared = typed_dict_keys(QUERY_STATE, "QueryGraphState")
    assert "meta_instruction" in declared, "未声明的键会被 LangGraph 静默丢弃"

    comment = QUERY_STATE.read_text(encoding="utf-8")
    for intent in ("conversation_echo", "meta_rewrite"):
        assert intent in comment, f"intent 新值 {intent} 必须注释进 state.py 才能被下游消费"
