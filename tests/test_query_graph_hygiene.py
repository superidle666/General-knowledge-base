"""查询侧结构 hygiene 守卫（批次三 P2 组 B）。

这一组改的是"形状"而不是"算法"：取值加固、增强节点返回契约统一、恒单值条件边退化
成普通边、配额回填写成人能读的样子、图编译结果进程内共享。所以测试分两类：

1. 行为类：证明形状改动没碰坏结果（回填数量、来源组成、写回的键、共享实例）。
2. 静态类：把"清理成果"钉住，防止下一个人又把部分字典返回、恒单值路由、每请求建图
   加回来——这些都不会让功能出错，只会在半年后重新变成"看代码猜契约"。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from graph_introspection import (
    PROJECT_ROOT,
    QUERY_MAIN_GRAPH,
    conditional_edge_targets,
    literal_string_returns,
)
from processor.query_processor.main_graph import get_kb_query_workflow
from processor.query_processor.nodes import node_search_embedding_hyde as hyde_module
from processor.query_processor.nodes.node_answer_output import NodeAnswerOutput
from processor.query_processor.nodes.node_search_embedding_hyde import NodeSearchEmbeddingHyde
from processor.query_processor.nodes.node_web_search_mcp import NodeWebSearchMcp

HYDE_NODE = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_search_embedding_hyde.py"
API_QUERY = PROJECT_ROOT / "backend" / "app" / "api" / "query.py"
QUERY_NODES_DIR = PROJECT_ROOT / "processor" / "query_processor" / "nodes"


def _long_doc(source: str, score: float, index: int = 0) -> dict:
    """过内容长度门槛的假切片：本地正文要 >=60 字，联网摘要 >=20 字。"""
    return {
        "content": f"{source} 正文内容片段" * 8,
        "source": source,
        "score": score - index * 0.01,
        "chunk_id": index if source == "local" else None,
        "url": None if source == "local" else "https://a.test",
        "title": "T",
    }


# ---------------------------------------------------------------- P2-4 取值加固


def test_prompt_builds_when_item_names_key_is_absent() -> None:
    """绕开入口直接调答案节点时 item_names 可能根本没这个键，不该用 KeyError 惩罚。"""
    prompt = NodeAnswerOutput()._step_2_construct_prompt({"original_query": "HAK180 怎么调温"})
    assert "无指定商品" in prompt


def test_prompt_treats_none_item_names_as_no_entity() -> None:
    """`or []` 而不是 `state.get(k, [])`：上游写进 None 也要按"没有实体"处理。"""
    prompt = NodeAnswerOutput()._step_2_construct_prompt(
        {"original_query": "问题", "item_names": None}
    )
    assert "无指定商品" in prompt


def test_real_item_names_still_reach_the_prompt() -> None:
    """防止把加固实现成"永远当没有实体"：有实体时必须照样进 prompt。"""
    prompt = NodeAnswerOutput()._step_2_construct_prompt(
        {"original_query": "问题", "item_names": ["HAK180烫金机", "Brother标签机"]}
    )
    assert "HAK180烫金机, Brother标签机" in prompt
    assert "无指定商品" not in prompt


# ---------------------------------------------------------------- P2-5 返回契约


def _hyde_node_with_stub(monkeypatch: pytest.MonkeyPatch) -> NodeSearchEmbeddingHyde:
    node = NodeSearchEmbeddingHyde()
    monkeypatch.setattr(node, "_step_1_create_hyde_doc", lambda query: "假设性答案原文")
    monkeypatch.setattr(
        node, "_step_2_search_embedding_hyde",
        lambda **kwargs: [{"chunk_id": 7, "content": "切片"}],
    )
    return node


def test_hyde_returns_the_same_state_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """与兄弟节点同一契约：写回并返回完整 state，不新建只含几个键的字典。"""
    node = _hyde_node_with_stub(monkeypatch)
    state = {"rewritten_query": "问题", "library_id": 3, "item_names": [], "keep_me": "原样保留"}

    result = node.process(state)

    assert result is state, "HyDE 节点不再返回传入的 state，契约回退成部分字典了"
    assert result["hyde_embedding_chunks"] == [{"chunk_id": 7, "content": "切片"}]
    assert result["hyde_doc"] == "假设性答案原文"
    assert result["hyde_failed"] is False
    assert result["keep_me"] == "原样保留"


def test_hyde_drops_entity_filter_once_local_search_relaxed_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """R3：主路已经证明"带实体过滤召不回东西"，HyDE 不该再拿同一个过滤条件白跑一次。"""
    captured: dict[str, object] = {}

    def run(state: dict) -> None:
        node = NodeSearchEmbeddingHyde()
        monkeypatch.setattr(node, "_step_1_create_hyde_doc", lambda query: "假设性答案原文")
        monkeypatch.setattr(
            node, "_step_2_search_embedding_hyde",
            lambda **kwargs: captured.update(kwargs) or [{"chunk_id": 7, "content": "切片"}],
        )
        node.process(state)

    run({"rewritten_query": "怎么调温", "library_id": 3, "item_names": ["HAK-180"], "entity_filter_relaxed": True})
    assert captured["item_names"] == []
    captured.clear()
    # 反向约束：主路没放宽时 HyDE 必须继续带过滤，否则等于整条链路放弃实体收窄
    run({"rewritten_query": "怎么调温", "library_id": 3, "item_names": ["HAK-180"]})
    assert captured["item_names"] == ["HAK-180"]


def test_hyde_failure_path_still_returns_state_and_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """降级分支同样返回完整 state：失败标记在，其他键不能被换成新字典。"""
    node = NodeSearchEmbeddingHyde()

    def boom(query: str) -> str:
        raise RuntimeError("大模型不可用")

    monkeypatch.setattr(node, "_step_1_create_hyde_doc", boom)
    state = {"rewritten_query": "问题", "library_id": 3, "keep_me": "原样保留"}

    result = node.process(state)

    assert result is state
    assert result["hyde_embedding_chunks"] == []
    assert result["hyde_failed"] is True
    assert result["keep_me"] == "原样保留"


def test_web_node_returns_the_same_state_object() -> None:
    """联网节点与 HyDE 同批统一：否则图里仍是两种契约，静态守卫得按形状分叉写两份。"""
    node = NodeWebSearchMcp()
    state = {"rewritten_query": "   ", "original_query": ""}
    result = node.process(state)
    assert result is state
    assert result["web_search_docs"] == []
    assert result["web_search_failed"] is False


def _function(path: Path, class_name: str, function_name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == function_name:
                    return member
    raise AssertionError(f"{path.name} 的 {class_name} 里找不到 {function_name}，请同步更新用例")


def test_hyde_step1_no_longer_wraps_a_log_and_reraise() -> None:
    """`except → logger → raise e` 只是多打一条日志并重新抛一次，异常本来就会被上层接住。"""
    body = _function(HYDE_NODE, "NodeSearchEmbeddingHyde", "_step_1_create_hyde_doc")
    assert not any(isinstance(node, ast.Try) for node in ast.walk(body)), \
        "_step_1_create_hyde_doc 又包了一层只做日志的 try"


def test_hyde_failure_logging_happens_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """去掉内层 try 的副作用检查：一次失败只留一条降级日志，不再同一异常打两遍。

    桩必须打在 `get_llm_client` 上而不是 `_step_1_create_hyde_doc` 本身：直接替掉被守卫
    的那个方法，内层 try 有没有都不会被执行，这条用例就恒绿。
    """
    node = NodeSearchEmbeddingHyde()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("大模型不可用")

    monkeypatch.setattr(hyde_module, "get_llm_client", boom)
    records: list[str] = []
    monkeypatch.setattr(
        hyde_module.logger, "exception",
        lambda *args, **kwargs: records.append(str(args[0] if args else kwargs)),
    )
    node.process({"rewritten_query": "问题", "library_id": 3})
    assert len(records) == 1, f"同一次失败留下了 {len(records)} 条 exception 日志"


# ---------------------------------------------------------------- P2-6 条件边


def _plain_edges(path: Path) -> set[tuple[str, str]]:
    """取 `add_edge("a", "b")` 这样的无分支边（两个参数都必须是字符串字面量）。"""
    edges: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_edge"
            and len(node.args) == 2
            and all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args)
        ):
            edges.add((node.args[0].value, node.args[1].value))
    return edges


def test_rerank_flows_to_answer_without_a_branch() -> None:
    """精排后一条普通边直连答案节点：不分流；死节点 node_source_collection 已从图摘除（Phase3 决策3A）。

    它产物被 node_answer_output 覆盖、无消费方，故从图移除；这里把它的端点“不得复活”钉住（
    只查边、不查文本，因为注释里会提到这个名字）。合法短引用现在统一由生成同源口径产出。
    """
    edges = _plain_edges(QUERY_MAIN_GRAPH)
    assert ("node_rerank", "node_answer_output") in edges
    assert "node_source_collection" not in {n for edge in edges for n in edge}, "已摘除的死节点又被接回图里"
    assert "node_source_collection" not in conditional_edge_targets(QUERY_MAIN_GRAPH)


def test_every_query_router_can_actually_choose_between_targets() -> None:
    """剩下的每个路由函数都必须至少能返回两个不同值，否则又该退化成 add_edge。"""
    edges = conditional_edge_targets(QUERY_MAIN_GRAPH)
    assert set(edges) == {"node_prepare_query", "node_search_embedding", "node_search_embedding_hyde"}, \
        "查询图的条件边数量变了，请确认新增的分流是真分支还是又一个恒单值"
    for source, router_expression in edges.items():
        router = router_expression.split(".")[-1]
        options = literal_string_returns(QUERY_MAIN_GRAPH, router)
        assert len(options) > 1, f"{source} 的路由 {router} 只有单一出口 {options or '（没有字面量返回值）'}"


# ---------------------------------------------------------------- P2-10 图缓存


def test_shared_workflow_is_built_and_compiled_once() -> None:
    first = get_kb_query_workflow()
    second = get_kb_query_workflow()

    assert first is second, "查询图又被每次取用时重建"
    assert first.compile() is second.compile(), "compile() 没缓存，每请求仍要重新编译图"


def test_query_api_stops_building_a_graph_per_request() -> None:
    text = API_QUERY.read_text(encoding="utf-8")
    assert "KBQueryWorkflow()" not in text, "接口层还在每请求 new 一个工作流（含一次图编译）"
    assert text.count("get_kb_query_workflow()") == 2, "同步与流式两条链路都要改用共享工作流"


def test_query_nodes_hold_no_request_state() -> None:
    """共享图的前提：节点实例不带请求态。写进 self 的那一类改动必须先想清楚并发。"""
    offenders: list[str] = []
    for path in sorted(QUERY_NODES_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign):
                targets: list[ast.expr] = list(node.targets)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    offenders.append(f"{path.name}:{node.lineno} self.{target.attr}")
    assert not offenders, f"查询节点出现了实例状态，共享图不再安全：{offenders}"
