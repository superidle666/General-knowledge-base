"""关键字段硬校验：集合 schema 漂移必须显性化（批次二 P1-7）。

`resolve_output_fields` 原本只做一件事——按集合结构裁剪输出字段（多要一个不存在的列会让
整次检索报错）。但"裁剪"被顺手用成了"缺了也行"：`chunk_id` 缺失时 `normalize_hits` 会把每
条命中都丢掉（整路静默清空），`content` 缺失时召回照常却给大模型一段空白正文，两者都表现
成"知识库没查到"。这类 schema 漂移（旧集合、迁移跑一半、手工建表）最难查，因为链路不报错。

现在三个关键字段缺失一律抛 `CollectionSchemaError`，并被两路检索既有的"检索故障"降级接住：
置 failed 标志 + 空召回 + 留现场，而不是让整条问答 500。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import processor.query_processor.nodes.node_search_embedding as local_module
import processor.query_processor.nodes.node_search_embedding_hyde as hyde_module
from graph_introspection import PROJECT_ROOT, imported_names
from processor.query_processor.nodes.node_search_embedding import NodeSearchEmbedding
from processor.query_processor.nodes.node_search_embedding_hyde import NodeSearchEmbeddingHyde
from processor.query_processor.retrieval_common import (
    LIBRARY_FILTER_FIELD,
    REQUIRED_OUTPUT_FIELDS,
    RETRIEVAL_OUTPUT_FIELDS,
    CollectionSchemaError,
    resolve_output_fields,
)

LOCAL_NODE = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_search_embedding.py"
HYDE_NODE = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_search_embedding_hyde.py"

# 一份"够用的旧集合"：关键字段齐、可选字段大面积缺失
MINIMAL_OK_FIELDS = {"chunk_id", "content", LIBRARY_FILTER_FIELD}
# 缺 content：召回能返回，但送进大模型的正文是空的
FIELDS_WITHOUT_CONTENT = {"chunk_id", LIBRARY_FILTER_FIELD, "item_name"}
FIELDS_WITHOUT_CHUNK_ID = {"content", LIBRARY_FILTER_FIELD, "document_id"}
FIELDS_WITHOUT_LIBRARY = {"chunk_id", "content", "item_name"}


# --------------------------------------------------------------------------
# 一、纯函数层：关键字段缺失必须抛，可选字段仍按结构自适应
# --------------------------------------------------------------------------

def test_required_fields_are_exactly_the_three_unsubstitutable_columns() -> None:
    assert set(REQUIRED_OUTPUT_FIELDS) == {"chunk_id", "content", "knowledge_base_id"}
    # 必需字段必须也在输出白名单里：否则校验过了却根本不请求它，校验就成了空转
    assert set(REQUIRED_OUTPUT_FIELDS) <= set(RETRIEVAL_OUTPUT_FIELDS)
    assert LIBRARY_FILTER_FIELD in RETRIEVAL_OUTPUT_FIELDS


@pytest.mark.parametrize(
    "fields,missing",
    [
        (FIELDS_WITHOUT_CONTENT, ["content"]),
        (FIELDS_WITHOUT_CHUNK_ID, ["chunk_id"]),
        (FIELDS_WITHOUT_LIBRARY, [LIBRARY_FILTER_FIELD]),
    ],
)
def test_missing_key_field_raises_with_the_field_named(fields: set[str], missing: list[str]) -> None:
    """缺哪个就点名哪个：运维对着日志不用猜是迁移没跑完还是连错集合。"""
    with pytest.raises(CollectionSchemaError) as error:
        resolve_output_fields(fields)
    for field in missing:
        assert field in str(error.value)


def test_error_lists_every_missing_field_and_what_the_collection_has() -> None:
    """现场一次给全：分几次报错会让人以为修一个就够。"""
    with pytest.raises(CollectionSchemaError) as error:
        resolve_output_fields({"item_name", "title"})
    message = str(error.value)
    assert all(field in message for field in REQUIRED_OUTPUT_FIELDS)
    # 集合现有字段一并打印，才能一眼看出是旧集合还是空集合
    assert "item_name" in message and "title" in message


def test_minimal_compatible_collection_still_resolves() -> None:
    """按结构自适应不能被校验过头：旧集合缺绝大多数可选字段仍要能检索。"""
    resolved = resolve_output_fields(MINIMAL_OK_FIELDS)
    assert set(resolved) == MINIMAL_OK_FIELDS


def test_optional_fields_keep_being_trimmed_in_declared_order() -> None:
    """输出顺序沿用白名单顺序：Milvus 回来的字段顺序稳定，排查时好比对。"""
    fields = MINIMAL_OK_FIELDS | {"file_name", "chunk_id"}
    resolved = resolve_output_fields(fields)
    assert resolved == [field for field in RETRIEVAL_OUTPUT_FIELDS if field in fields]
    assert resolved[0] == "chunk_id"


def test_error_type_is_a_runtime_error_so_existing_handlers_catch_it() -> None:
    """降级路径靠的是 except Exception，异常类型换了也要接得住（而且不能被误当取消/中断）。"""
    assert issubclass(CollectionSchemaError, RuntimeError)


# --------------------------------------------------------------------------
# 二、节点层：schema 不兼容走"检索故障"降级，不炸链路、也不白发检索请求
# --------------------------------------------------------------------------

class _ClientStub:
    def has_collection(self, collection_name: str) -> bool:
        return True


class _SimpleLlm:
    def invoke(self, prompt: str) -> object:
        return type("Response", (), {"content": "假设性答案"})()


def _misleading_success_hybrid_search(**kwargs: Any) -> list:
    """不抛错的检索桩：集合缺列时 Milvus 本来就照样返回行，只是少了那一列。

    节点层的用例必须用这个桩而不是“一调就报错”的桩：后者会被节点的 except
    接住也算“降级成功”，于是硬校验被拆掉也不会红。
    """
    return [{"chunk_id": 1, LIBRARY_FILTER_FIELD: 3, "distance": 0.9}]


def _prepare_local_node(monkeypatch, fields: set[str], calls: dict[str, int]):
    monkeypatch.setattr(local_module, "generate_embeddings", lambda texts: {
        "dense": [[0.1, 0.2, 0.3]],
        "sparse": [{7: 0.9}],
    })
    monkeypatch.setattr(local_module, "get_milvus_client", _ClientStub)
    monkeypatch.setattr(local_module, "collection_field_names", lambda client, name: set(fields))
    monkeypatch.setattr(local_module, "create_hybrid_search_requests", lambda **kwargs: [])
    monkeypatch.setattr(local_module, "hybrid_search", _misleading_success_hybrid_search)
    calls["hybrid_search"] = 0
    return NodeSearchEmbedding()


def _prepare_hyde_node(monkeypatch, fields: set[str]):
    monkeypatch.setattr(hyde_module, "get_llm_client", lambda: _SimpleLlm())
    monkeypatch.setattr(hyde_module, "generate_embeddings", lambda texts: {
        "dense": [[0.1, 0.2, 0.3]],
        "sparse": [{7: 0.9}],
    })
    monkeypatch.setattr(hyde_module, "get_milvus_client", _ClientStub)
    monkeypatch.setattr(hyde_module, "collection_field_names", lambda client, name: set(fields))
    monkeypatch.setattr(hyde_module, "create_hybrid_search_requests", lambda **kwargs: [])
    monkeypatch.setattr(hyde_module, "hybrid_search", _misleading_success_hybrid_search)
    return NodeSearchEmbeddingHyde()


def test_local_node_degrades_instead_of_raising(monkeypatch) -> None:
    """旧写法在节点里判 knowledge_base_id 并直接 raise，整条问答会 500；现在按故障降级。

    检索桩故意“成功”：没有硬校验时本会拿到一份看着正常、实则没有正文的召回，
    那正是 P1-7 要堵的伪装。
    """
    node = _prepare_local_node(monkeypatch, FIELDS_WITHOUT_CONTENT, {})
    state = node.process({"rewritten_query": "HAK180 怎么调节温度", "library_id": 3})

    assert state["embedding_search_failed"] is True, "schema 不兼容是检索故障，不是零命中"
    assert state["embedding_chunks"] == [], "残缺切片不能当成正常召回送给大模型"


def test_local_node_skips_the_search_request(monkeypatch) -> None:
    """明知字段不齐还发请求，等于白跑一次 Milvus（还可能返回不可用的切片）。"""
    calls: dict[str, int] = {}
    node = _prepare_local_node(monkeypatch, FIELDS_WITHOUT_CHUNK_ID, calls)

    def counting_hybrid_search(**kwargs: Any) -> list:
        calls["hybrid_search"] += 1
        return [[]]

    # 必须在 _prepare_local_node 之后覆盖：它会给 hybrid_search 装上一个默认桩，
    # 先装计数版会被顶掉，这个用例就变成“永远计数为 0”的空过断言。
    monkeypatch.setattr(local_module, "hybrid_search", counting_hybrid_search)
    state = node.process({"rewritten_query": "怎么调节温度", "library_id": 3})

    assert state["embedding_search_failed"] is True
    assert calls["hybrid_search"] == 0


def test_local_node_with_compatible_collection_is_not_marked_failed(monkeypatch) -> None:
    """硬校验不能把正常路径一起打死：字段齐时 failed 标志必须落回 False。"""
    calls: dict[str, int] = {}
    node = _prepare_local_node(monkeypatch, MINIMAL_OK_FIELDS | {"item_name"}, calls)

    def working_hybrid_search(**kwargs: Any) -> list:
        calls["hybrid_search"] = 1
        return [{"chunk_id": 5, "content": "正文", "distance": 0.8}]

    monkeypatch.setattr(local_module, "hybrid_search", working_hybrid_search)
    state = node.process({"rewritten_query": "怎么调节温度", "library_id": 3})

    assert calls["hybrid_search"] == 1
    assert state["embedding_search_failed"] is False
    assert [item["chunk_id"] for item in state["embedding_chunks"]] == [5]


def test_hyde_node_marks_hyde_failed_on_incompatible_collection(monkeypatch) -> None:
    """HyDE 增强路失败不该拖垮主链路：RRF 还能用本地那一路。"""
    node = _prepare_hyde_node(monkeypatch, FIELDS_WITHOUT_LIBRARY)
    result = node.process({"rewritten_query": "怎么调节温度", "library_id": 3})

    assert result["hyde_failed"] is True
    assert result["hyde_embedding_chunks"] == []
    assert "hyde_doc" not in result or result.get("hyde_doc") is None, "失败时不回写假设文档"


def test_both_nodes_use_the_same_required_field_source(monkeypatch) -> None:
    """两路检索必须同一把尺子，否则一路放行一路报错，RRF 只拿到半份结果。

    这里用“不报错的检索桩”：否则两路都会因为连接失败而置 failed，用例就变成了
    验证 P0-3 而不是验证字段口径一致。
    """
    for fields in (FIELDS_WITHOUT_CONTENT, FIELDS_WITHOUT_CHUNK_ID, FIELDS_WITHOUT_LIBRARY):
        local_state = _prepare_local_node(monkeypatch, set(fields), {}).process(
            {"rewritten_query": "怎么调节温度", "library_id": 3}
        )
        hyde_result = _prepare_hyde_node(monkeypatch, set(fields)).process(
            {"rewritten_query": "怎么调节温度", "library_id": 3}
        )
        assert local_state["embedding_search_failed"] is True, f"本地路放行了 {fields}"
        assert hyde_result["hyde_failed"] is True, f"HyDE 路放行了 {fields}"


# --------------------------------------------------------------------------
# 三、静态守卫：校验点不许被挪出降级路径，也不许在节点里各判一份
# --------------------------------------------------------------------------

def _call_names_inside_try(path: Path, function_name: str) -> set[str]:
    """收集指定函数里 try 块正文中出现的被调用名（含 self.method 的方法名）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function_name:
            continue
        names: set[str] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Try):
                continue
            for stmt in inner.body:
                for call in ast.walk(stmt):
                    if isinstance(call, ast.Call):
                        if isinstance(call.func, ast.Name):
                            names.add(call.func.id)
                        elif isinstance(call.func, ast.Attribute):
                            names.add(call.func.attr)
        return names
    raise AssertionError(f"{path.name} 中找不到 {function_name}，请同步更新用例")


def test_local_node_validates_schema_inside_the_failure_handler() -> None:
    """挪出 try 就等于把降级改成 500，这里用 AST 锁住调用位置。"""
    assert "resolve_output_fields" in _call_names_inside_try(LOCAL_NODE, "process")


def test_hyde_search_step_runs_inside_the_failure_handler() -> None:
    """硬校验落在 _step_2 里，靠的是整个步骤被 process 的 try 包住。"""
    assert "_step_2_search_embedding_hyde" in _call_names_inside_try(HYDE_NODE, "process")


def test_nodes_no_longer_probe_the_library_field_themselves() -> None:
    """关键字段判定只有 retrieval_common 一处，节点里再判一份就是下次的分歧来源。"""
    for path in (LOCAL_NODE, HYDE_NODE):
        assert "LIBRARY_FILTER_FIELD" not in imported_names(path, "retrieval_common"), (
            f"{path.name} 不该自己判库隔离字段是否存在"
        )
