"""任务进度追踪与节点名映射的一致性守卫。

两张 LangGraph 主图用字符串注册节点，前端步骤名又来自 utils/task_utils 的静态映射表，
两者没有任何编译期约束：节点改名、删除或漏注册时，映射表会静默失真（本项目就出现过
知识图谱等 5 个早已不存在的节点留在表里的情况）。这里用 AST 直接读源码做交叉校验，
不实例化工作流，因此不触碰 Milvus / Redis / LLM。
"""

from __future__ import annotations

from graph_introspection import IMPORT_MAIN_GRAPH, QUERY_MAIN_GRAPH, registered_node_names
from utils import task_utils

# LangGraph 的终止节点不是业务节点，允许出现在映射表里但不来自 add_node。
BUILTIN_END_NAMES = {"__end__", "END"}


def test_both_main_graphs_register_expected_nodes() -> None:
    """两张图都还能被解析到节点注册表，防止守卫因文件重构而空转。"""
    import_nodes = registered_node_names(IMPORT_MAIN_GRAPH)
    query_nodes = registered_node_names(QUERY_MAIN_GRAPH)
    assert {"node_entry", "node_persist_chunks", "node_bge_embedding"} <= import_nodes
    assert {"node_prepare_query", "node_rerank", "node_answer_output"} <= query_nodes
    # 导入 10 个、检索 8 个：数量变化意味着链路结构调整，需要同步复核进度与映射
    assert len(import_nodes) == 10, f"导入图节点数变为 {len(import_nodes)}，请同步更新本用例与节点名映射"
    assert len(query_nodes) == 8, f"检索图节点数变为 {len(query_nodes)}，请同步更新本用例与节点名映射"


def test_node_name_mapping_matches_registered_nodes() -> None:
    """中文映射表的 key 必须与真实注册节点一一对应：既不能有幽灵节点，也不能漏掉线上节点。"""
    registered = registered_node_names(IMPORT_MAIN_GRAPH) | registered_node_names(QUERY_MAIN_GRAPH)
    mapped = set(task_utils._NODE_NAME_TO_CN) - BUILTIN_END_NAMES
    ghosts = mapped - registered
    missing = registered - mapped
    assert not ghosts, f"映射表存在未注册节点，前端会展示不会发生的步骤：{sorted(ghosts)}"
    assert not missing, f"以下节点缺少中文展示名，前端只能看到英文原名：{sorted(missing)}"


# CJK 基本汉字区的起止码位；用 chr() 构造，避开本仓库对 unicode 转义写中文的编码守卫。
CJK_START = chr(0x4E00)
CJK_END = chr(0x9FFF)


def _contains_chinese(value: str) -> bool:
    """判断字符串是否落在基本汉字区内。"""
    return any(CJK_START <= char <= CJK_END for char in value)


def test_every_mapped_node_has_chinese_display_name() -> None:
    """展示名必须是中文，不能把英文原名当成已处理项塞回映射表。"""
    offenders = [
        f"{name} -> {cn!r}"
        for name, cn in task_utils._NODE_NAME_TO_CN.items()
        if name.startswith("node_") and not _contains_chinese(cn)
    ]
    assert not offenders, "以下节点展示名不含中文：\n" + "\n".join(offenders)


def test_running_and_done_lists_track_node_progress() -> None:
    """节点完成时应从 running 移到 done，并以中文名对外展示。"""
    task_id = "unit-case-running-done"
    task_utils.clear_task(task_id)
    try:
        task_utils.add_running_task(task_id, "node_document_split")
        assert task_utils.get_running_task_list(task_id) == ["文档切分"]
        assert task_utils.get_done_task_list(task_id) == []

        task_utils.add_done_task(task_id, "node_document_split")
        assert task_utils.get_running_task_list(task_id) == []
        assert task_utils.get_done_task_list(task_id) == ["文档切分"]

        # 同名节点重复上报不应产生重复条目
        task_utils.add_done_task(task_id, "node_bge_embedding")
        task_utils.add_done_task(task_id, "node_bge_embedding")
        assert task_utils.get_done_task_list(task_id) == ["文档切分", "向量生成"]
    finally:
        task_utils.clear_task(task_id)


def test_unmapped_node_falls_back_to_raw_name() -> None:
    """未知节点名要原样透出，便于排查而不是抛异常打断 SSE 进度。"""
    task_id = "unit-case-unknown-node"
    task_utils.clear_task(task_id)
    try:
        task_utils.add_running_task(task_id, "node_not_in_registry")
        assert task_utils.get_running_task_list(task_id) == ["node_not_in_registry"]
    finally:
        task_utils.clear_task(task_id)


def test_clear_task_removes_all_traces() -> None:
    """清理后不得残留内存态，否则长跑的 API 进程会持续泄漏任务字典。"""
    task_id = "unit-case-clear"
    task_utils.add_running_task(task_id, "node_entry")
    task_utils.set_task_result(task_id, "answer", "答案")
    task_utils.update_task_status(task_id, task_utils.TASK_STATUS_COMPLETED)
    task_utils.clear_task(task_id)
    assert task_utils.get_running_task_list(task_id) == []
    assert task_utils.get_done_task_list(task_id) == []
    assert task_utils.get_task_result(task_id, "answer") == ""
    assert task_utils.get_task_status(task_id) == ""
