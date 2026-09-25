"""P0-2：Milvus 重导入必须"先插后删"。

旧实现是 delete 再 insert：插入失败时旧向量已被删掉、新向量没进来，而 MySQL 里文档
状态仍是 ready，该文档从检索中彻底隐身，用户只能看到"知识库无相关内容"。
改成先插入本批切片、确认成功后再清掉该文档在这之前的全部切片，这里锁住：
1 正常路径的调用顺序是 insert 在前、delete 在后；
2 插入失败时一次 delete 都不能发生（旧数据必须原封不动继续服务）；
3 清理条件必须是"本批主键下界之前"，绝不能把刚插入的这批删掉；
4 清理条件不能依赖 document_version——重试导入沿用同一版本号，按版本排除会漏删；
5 清理失败只告警不上抛——新数据已经安全落库，残留旧切片比回滚新数据轻得多。
"""

from __future__ import annotations

from typing import Any

import pytest

from processor.import_processor.nodes.node_import_milvus import NodeImportMilvus

COLLECTION = "kb_document_chunks_v2"


class RecordingMilvusClient:
    """记录调用顺序的 Milvus 客户端替身，可按需注入插入/删除故障。"""

    def __init__(self, *, fail_insert: bool = False, fail_delete: bool = False) -> None:
        self.calls: list[str] = []
        self.deleted_filters: list[str] = []
        self.collection_exists = True
        self._next_id = 100
        self._fail_insert = fail_insert
        self._fail_delete = fail_delete

    def has_collection(self, *, collection_name: str) -> bool:
        return self.collection_exists

    def delete(self, *, collection_name: str, filter: str) -> None:  # noqa: A002 - 对齐 pymilvus 签名
        self.calls.append("delete")
        self.deleted_filters.append(filter)
        if self._fail_delete:
            raise RuntimeError("Milvus 删除超时")

    def insert(self, *, collection_name: str, data: list[dict[str, Any]]) -> dict[str, list[int]]:
        self.calls.append("insert")
        if self._fail_insert:
            raise RuntimeError("Milvus 插入失败")
        identifiers = list(range(self._next_id, self._next_id + len(data)))
        self._next_id += len(data)
        return {"ids": identifiers}


def make_state(
    knowledge_base_id: int = 1,
    document_id: int = 10,
    document_version: int = 2,
    chunk_count: int = 2,
) -> dict[str, Any]:
    """构造一份已通过校验的入库状态。"""
    return {
        "task_id": f"task-{knowledge_base_id}-{document_id}",
        "knowledge_base_id": knowledge_base_id,
        "document_id": document_id,
        "document_version": document_version,
        "chunks": [
            {
                "knowledge_base_id": knowledge_base_id,
                "document_id": document_id,
                "document_version": document_version,
                "chunk_index": index,
                "content": f"第{index}段设备内容",
                "dense_vector": [0.1, 0.2],
                "sparse_vector": {0: 0.5},
            }
            for index in range(chunk_count)
        ],
    }


def test_new_version_is_inserted_before_old_version_is_deleted() -> None:
    """调用顺序是先插后删：插入落库后才有资格动旧数据。"""
    client = RecordingMilvusClient()

    NodeImportMilvus(milvus_client=client, collection_name=COLLECTION).process(make_state())

    assert client.calls == ["insert", "delete"], f"实际调用顺序 {client.calls}，先删后插又回来了"


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("knowledge_base_id == 1", "知识库维度"),
        ("document_id == 10", "文档维度"),
        ("chunk_id < 100", "本批主键下界"),
    ],
)
def test_cleanup_filter_targets_only_preceding_chunks(field: str, message: str) -> None:
    """清理条件必须精确到"这批之前的切片"，多删一行就又把数据弄丢了。"""
    client = RecordingMilvusClient()

    NodeImportMilvus(milvus_client=client, collection_name=COLLECTION).process(make_state())

    assert len(client.deleted_filters) == 1
    filter_expression = client.deleted_filters[0]
    assert field in filter_expression, f"过滤条件缺少{message}：{filter_expression}"


def test_retry_import_with_same_document_version_still_cleans_previous_batch() -> None:
    """重试导入沿用同一 document_version，清理必须靠主键下界而不是版本号。"""
    client = RecordingMilvusClient()
    node = NodeImportMilvus(milvus_client=client, collection_name=COLLECTION)

    node.process(make_state(document_version=1))
    node.process(make_state(document_version=1))

    assert client.deleted_filters == [
        "knowledge_base_id == 1 and document_id == 10 and chunk_id < 100",
        "knowledge_base_id == 1 and document_id == 10 and chunk_id < 102",
    ], "同版本重试时旧切片必须被清掉，否则同一文档两份内容会长期重复召回"
    assert all("document_version" not in item for item in client.deleted_filters)


def test_insert_failure_keeps_old_vectors_untouched() -> None:
    """插入失败时必须一步删除都没做，检索继续命中上一批切片。"""
    client = RecordingMilvusClient(fail_insert=True)

    with pytest.raises(Exception, match="Milvus 插入失败"):
        NodeImportMilvus(milvus_client=client, collection_name=COLLECTION).process(make_state())

    assert client.calls == ["insert"], f"插入失败后仍发生了删除：{client.calls}"
    assert client.deleted_filters == []


def test_cleanup_failure_does_not_break_successful_import() -> None:
    """清理旧切片失败只告警：新数据已落库，不能因为删不掉旧数据把任务判失败。"""
    client = RecordingMilvusClient(fail_delete=True)
    state = make_state()

    result = NodeImportMilvus(milvus_client=client, collection_name=COLLECTION).process(state)

    assert client.calls == ["insert", "delete"]
    assert [chunk["vector_id"] for chunk in result["chunks"]] == ["100", "101"]
