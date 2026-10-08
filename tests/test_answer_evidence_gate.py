"""「无证据不进生成」（R5）与静态短路的来源口径（J2）的离线守卫。

R5：检索一条可用切片都没产出时，过去仍照常调模型 —— prompt 里 `context` 填成"无参考内容"，
模型就着问题本身编出"建议查阅《hak180产品安全手册》"这类引文；而接口层的兜底条件
`if not sources and not answer` 只要模型吐出文字就不触发，于是 `sources=0` 也能出一份看着
有凭据的答案。本守卫钉住：无证据时走固定话术、**不进生成**；并且"检索没跑成"（任一失败
标记为真）与"跑了但确实没查到"必须是两套话术 —— 把服务故障说成"库里没有"是在输出错误事实
（§6-7"故障 ≠ 零命中"的用户侧延伸）。图的拓扑不动：`node_rerank → node_answer_output`
仍是普通边（那条守卫在 `test_query_graph_hygiene.py`），"不进生成"是节点内部行为。

J2：拒答/反问/闲聊/校验这几条静态短路从不进 `_step_2_construct_prompt`，`sources` 键
**根本不存在**而不是空列表；下游硬下标会 KeyError，用 `.get()` 又分不清"算过但没有来源"
和"压根没算"（前端 R15 要靠这个区分隐藏徽标）。本守卫钉住所有静态短路都产出空列表。
"""

from __future__ import annotations

import pytest

from graph_introspection import PROJECT_ROOT, typed_dict_keys

from processor.query_processor.nodes import node_answer_output as answer_module
from processor.query_processor.nodes import node_prepare_query as prepare_module
from processor.query_processor.nodes.node_answer_output import (
    INSUFFICIENT_EVIDENCE_REPLY,
    RETRIEVAL_INCOMPLETE_REPLY,
    RETRIEVAL_FAILURE_MARKERS,
    NodeAnswerOutput,
)
from processor.query_processor.nodes.node_prepare_query import (
    NodePrepareQuery,
)

QUERY_STATE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"

VOCABULARY = [{"name": "HAK-180", "aliases": ["烫金机"]}]
FAKE_GENERATED_ANSWER = "换墨流程：先关闭电源再松开压杆。"


def _answer_state(**overrides) -> dict:
    state = {
        "session_id": "s-r5",
        "original_query": "HAK 180 的换墨流程是什么",
        "rewritten_query": "HAK 180 的换墨流程是什么",
        "item_names": [],
        "history": [],
        "reranked_docs": [],
        "is_stream": False,
        "persist_history": False,
    }
    state.update(overrides)
    return state


@pytest.fixture()
def answer_node(monkeypatch):
    """答案节点的离线桩：返回 (节点, 调用记录)，绝不联网。"""
    calls = {"llm": 0, "task_result": [], "pushed": []}

    class _FakeLLM:
        def invoke(self, prompt):
            calls["llm"] += 1
            return type("Resp", (), {"content": FAKE_GENERATED_ANSWER})()

        def stream(self, prompt):
            calls["llm"] += 1
            return iter(())

    monkeypatch.setattr(answer_module, "get_llm_client", lambda: _FakeLLM())
    monkeypatch.setattr(
        answer_module, "set_task_result", lambda sid, key, value: calls["task_result"].append(value)
    )
    monkeypatch.setattr(answer_module, "push_to_session", lambda sid, event, data: calls["pushed"].append(data))
    monkeypatch.setattr(answer_module, "add_done_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(answer_module, "save_chat_message", lambda **kwargs: None)
    return NodeAnswerOutput(), calls


# --------------------------------------------------------------------------
# 一、R5：无证据不进生成
# --------------------------------------------------------------------------

def test_zero_recall_short_circuits_without_calling_the_llm(answer_node) -> None:
    """一条切片都没有 → 固定"信息不足"话术，模型一次都不调。"""
    node, calls = answer_node
    state = node.process(_answer_state(reranked_docs=[]))

    assert state["answer"] == INSUFFICIENT_EVIDENCE_REPLY
    assert state["sources"] == []
    assert calls["llm"] == 0, "无证据仍进了生成就等于放任编造引文"
    # 必须复用"已有答案即原样输出"的既有出口（非流式写任务结果），不另写一套推送逻辑
    assert calls["task_result"] == [INSUFFICIENT_EVIDENCE_REPLY]


def test_docs_with_only_empty_bodies_count_as_no_evidence(answer_node) -> None:
    """列表非空但每条正文都是空白：对模型同样没有信息量，不能当"有证据"放行进生成。"""
    node, calls = answer_node
    docs = [{"content": "   ", "source": "local", "chunk_id": 1, "score": 0.9}]
    state = node.process(_answer_state(reranked_docs=docs))

    assert state["answer"] == INSUFFICIENT_EVIDENCE_REPLY
    assert calls["llm"] == 0


@pytest.mark.parametrize(
    "marker",
    ["embedding_search_failed", "hyde_failed", "web_search_failed", "rerank_failed"],
)
def test_any_failure_marker_switches_to_the_incomplete_wording(answer_node, marker: str) -> None:
    """检索链路报过故障就不能说"库里没有"：那是把服务问题伪装成事实结论（§6-7）。"""
    node, calls = answer_node
    state = node.process(_answer_state(reranked_docs=[], **{marker: True}))

    assert state["answer"] == RETRIEVAL_INCOMPLETE_REPLY
    assert "没有找到" not in state["answer"]
    assert calls["llm"] == 0


def test_real_docs_still_reach_generation(answer_node) -> None:
    """反向约束：不能把守卫实现成"永远拒答"。有正文就必须走生成。"""
    node, calls = answer_node
    docs = [{"content": "换墨前先关闭主电源开关，再松开压杆。", "source": "local", "chunk_id": 7, "score": 0.91}]
    state = node.process(_answer_state(reranked_docs=docs))

    assert calls["llm"] == 1
    assert state["answer"] == FAKE_GENERATED_ANSWER
    assert len(state["sources"]) == 1


def test_failure_marker_with_usable_docs_does_not_refuse(answer_node) -> None:
    """精排降级（rerank_failed）时切片仍在：有可用证据就照常答，失败标记不该单独触发拒答。"""
    node, calls = answer_node
    docs = [{"content": "温度建议设置在 110℃ 左右。", "source": "local", "chunk_id": 3, "score": 0.42}]
    state = node.process(_answer_state(reranked_docs=docs, rerank_failed=True))

    assert calls["llm"] == 1
    assert state["answer"] == FAKE_GENERATED_ANSWER


def test_preset_answer_is_never_overwritten_by_the_guard(answer_node) -> None:
    """前置短路（闲聊/拒答/清单元问题）已写好答案时，本守卫不得改写它。"""
    node, calls = answer_node
    preset = "当前我无法获取知识库的全部产品，请提供您要咨询的具体型号。"
    state = node.process(_answer_state(answer=preset, reranked_docs=[]))

    assert state["answer"] == preset
    assert calls["llm"] == 0


def test_failure_marker_list_covers_every_failed_key_in_state() -> None:
    """静态守卫：话术选择靠一串标记名，而这些名字是上游节点写进 state 的——两边不同步，
    新增的故障路就会在 R5 里被当成“查过了但没有”，把服务问题说成“库里没有”。
    用 QueryGraphState 里声明的全部 `*_failed` 字段反查，坚持等于而不是包含。"""
    declared = {
        key for key in typed_dict_keys(QUERY_STATE, "QueryGraphState") if key.endswith("_failed")
    }
    assert set(RETRIEVAL_FAILURE_MARKERS) == declared, (
        f"故障标记与 state 声明不一致：仅本守卫有 {set(RETRIEVAL_FAILURE_MARKERS) - declared}，"
        f"仅 state 有 {declared - set(RETRIEVAL_FAILURE_MARKERS)}"
    )


def test_evidence_check_matches_the_prompt_builders_own_notion(answer_node) -> None:
    """“有没有证据”必须与 `_select_context_docs` 的口径一致：它只过掉空白正文、不按分数筛，
    所以本守卫也不能拿低分当“无证据”（否则精排降级时的可用切片会被误拒）。"""
    node, calls = answer_node
    docs = [{"content": "低温启动前先预热三分钟。", "source": "local", "chunk_id": 9, "score": 0.01}]
    state = node.process(_answer_state(reranked_docs=docs))

    assert calls["llm"] == 1
    assert state["answer"] == FAKE_GENERATED_ANSWER


# --------------------------------------------------------------------------
# 二、J2：静态短路统一产出空来源列表
# --------------------------------------------------------------------------

def _prepare_state(query: str, **overrides) -> dict:
    state = {
        "original_query": query,
        "library_id": 7,
        "history": [],
        "entity_recognition_mode": "required",
        "entity_vocabulary": VOCABULARY,
    }
    state.update(overrides)
    return state


@pytest.fixture()
def prepare_node(monkeypatch):
    prepared = NodePrepareQuery()
    monkeypatch.setattr(prepared, "_call_entity_llm", lambda text, history_text: [])
    monkeypatch.setattr(prepare_module, "classify_chitchat_llm", lambda text: (None, ""))
    return prepared


def _assert_short_circuit_with_empty_sources(state: dict, expected_prefix: str) -> None:
    answer = str(state.get("answer") or "")
    assert answer.startswith(expected_prefix), f"预期短路话术以 {expected_prefix} 开头，实际：{answer!r}"
    assert "sources" in state, "短路后 sources 键必须存在（缺键 ≠ 空列表）"
    assert state["sources"] == []


def test_empty_query_short_circuit_exposes_empty_sources(prepare_node) -> None:
    _assert_short_circuit_with_empty_sources(
        prepare_node.process(_prepare_state("")), "请输入要查询的问题"
    )


def test_invalid_library_short_circuit_exposes_empty_sources(prepare_node) -> None:
    _assert_short_circuit_with_empty_sources(
        prepare_node.process(_prepare_state("HAK-180 怎么调温", library_id=0)), "请选择有效的知识库"
    )


def test_chitchat_short_circuit_exposes_empty_sources(prepare_node) -> None:
    state = prepare_node.process(_prepare_state("你好"))
    assert state.get("intent") == "chitchat"
    assert "sources" in state and state["sources"] == []


def test_refusal_short_circuit_exposes_empty_sources(prepare_node) -> None:
    """库里查无此型号 → 拒答；这条走 _align_entity，过去只写 answer 不写 sources。"""
    _assert_short_circuit_with_empty_sources(
        prepare_node.process(_prepare_state("ZZZ-999 怎么开机")), "当前知识库未收录"
    )


def test_clarify_short_circuit_exposes_empty_sources(prepare_node) -> None:
    """多候选反问：同样必须产出空来源，且不能被 R5 的话术覆盖（它在答案节点之前）。"""
    vocabulary = [
        {"name": "华为平板 C7 用户指南", "aliases": []},
        {"name": "华为平板 M6 用户指南", "aliases": []},
    ]
    _assert_short_circuit_with_empty_sources(
        prepare_node.process(_prepare_state("华为平板有哪些型号", entity_vocabulary=vocabulary)),
        "您是想问以下哪个产品",
    )


def test_normal_path_leaves_sources_to_the_answer_node(prepare_node) -> None:
    """未被短路时不该预置 sources：来源由答案节点按"真正进 prompt 的切片"产出（同源口径）。"""
    state = prepare_node.process(_prepare_state("HAK-180 怎么设置温度"))

    assert not state.get("answer")
    assert "sources" not in state
