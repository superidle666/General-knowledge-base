"""本地检索与 HyDE 检索共用的 Milvus 检索工具。

@FilePath: processor/query_processor/retrieval_common.py
@Date: 2026-09-21
@Description: 收敛多路向量检索共用的输出字段白名单、集合结构探测、过滤表达式构造与结果归一化。
@BusinessRule: 同一知识库的多路检索必须使用同一套字段和同一套库隔离条件，否则某一路召回的结果
               会因为缺少定位字段而无法回填成用户可见的来源。
"""

from __future__ import annotations

from typing import Any

from utils.milvus_utils import escape_milvus_string

# 来源回填、图片关联和引用溯源都依赖这些字段；新增字段要先确认导入侧确实写入
#
# image_sources / image_metadata 不在此列：查询/生成期的图片回链完全走 content 里的
# Markdown ![](url)（见 node_answer_output._extract_images_from_docs），没有任何读取方依赖
# 这两个 Milvus 列；把它们列入输出只会让每条命中多拽回最多 65535 字节的 JSON。列本身仍在
# schema 里作溯源预留，要用时放回此白名单即可，无需重建集合。管理端展示的图片来自
# node_persist_chunks 写入的归档副本，与本输出字段无关。
RETRIEVAL_OUTPUT_FIELDS: tuple[str, ...] = (
    "chunk_id", "knowledge_base_id", "document_id", "document_version",
    "chunk_index", "content", "title", "parent_title", "file_name",
    "file_title", "source_page", "item_name", "entity_recognition_mode",
    "chunk_type", "source_kind",
)

# 库隔离字段：v2 集合按知识库 ID 分区过滤，缺少它等于跨库检索
LIBRARY_FILTER_FIELD = "knowledge_base_id"

# 实体过滤最多带入的值数量：过滤表达式是纯文本拼接，过长的列表只会放大解析失败面
MAX_FILTER_VALUES = 10

# 混合检索的稠密/稀疏打分权重；多路检索共用一份，避免调权时只改了一路
HYBRID_RANKER_WEIGHTS: tuple[float, float] = (0.8, 0.2)

# 实体名所在的列；旧版集合没有这一列，对它的过滤表达式会让整次检索报错
ITEM_NAME_FILTER_FIELD = "item_name"

# 检索链路不可缺少的字段：缺任何一个都不能“自适应”掉
# - chunk_id：normalize_hits 会丢弃没有它的命中（等于整路静默清空），RRF 也靠它去重
# - content：送给大模型的正文，缺了就是“召回到却答不出”，只能从日志反推
# - knowledge_base_id：库隔离过滤字段，缺了要么表达式报错，要么跨库
REQUIRED_OUTPUT_FIELDS: tuple[str, ...] = ("chunk_id", "content", LIBRARY_FILTER_FIELD)


class CollectionSchemaError(RuntimeError):
    """集合结构不符合检索契约：调用方应按“检索故障”降级，而不是当成零命中继续回答。"""


def collection_field_names(client: Any, collection_name: str) -> set[str]:
    """读取集合的实际字段名，用于按结构自适应，避免对旧集合写出无效表达式。"""
    description = client.describe_collection(collection_name=collection_name)
    if isinstance(description, dict):
        schema = description.get("schema")
        fields = schema.get("fields", []) if isinstance(schema, dict) else description.get("fields", [])
    else:
        schema = getattr(description, "schema", None)
        fields = getattr(schema, "fields", None) or getattr(description, "fields", [])
    names: set[str] = set()
    for field in fields or []:
        name = field.get("name") if isinstance(field, dict) else getattr(field, "name", None)
        if name:
            names.add(str(name))
    return names


def resolve_output_fields(field_names: set[str]) -> list[str]:
    """按集合真实结构裁剪输出字段，并对关键字段做硬校验。

    多要一个不存在的字段就会让整次检索抛错，所以可选字段只能按结构自适应；但
    “自适应”不等于“缺了也行”：三个关键字段缺失时检索不会报错，只会给出空结果或没有
    正文的切片，看起来像“知识库没查到”，实际是 schema 漂移（旧集合、迁移跑一半、手工建表）。
    这里显性报错，让调用方走已有的检索故障降级路并留下现场。
    """
    missing = [field for field in REQUIRED_OUTPUT_FIELDS if field not in field_names]
    if missing:
        raise CollectionSchemaError(
            f"检索集合 schema 不兼容，缺少关键字段：{missing}；集合现有字段：{sorted(field_names)}"
        )
    return [field for field in RETRIEVAL_OUTPUT_FIELDS if field in field_names]


def build_library_filter(library_id: int) -> str:
    """构造知识库隔离条件。

    ID 来自 MySQL 自增主键，这里显式转 int 而不是拼接字符串，既保证表达式是数值比较，
    也顺带堵住了把 library_id 当字符串注入的通道。
    """
    return f"{LIBRARY_FILTER_FIELD} == {int(library_id)}"


def build_item_name_filter(item_names: Any, available_fields: set[str] | None = None) -> str | None:
    """把实体名列表转成 Milvus in 表达式，无有效值时返回 None（表示不过滤）。

    实体名可能来自用户输入或大模型抽取，必须走 escape_milvus_string 再套双引号；
    直接把 Python list 的 repr 拼进表达式会产生单引号字面量，且遇到引号就解析失败。

    :param available_fields: 集合真实字段名；传入时若集合不含 item_name 列则放弃过滤，
                             宁可多召回也不能让整次检索失败。
    """
    if not item_names:
        return None
    if available_fields is not None and ITEM_NAME_FILTER_FIELD not in available_fields:
        return None
    values: list[str] = []
    for name in item_names:
        text = str(name or "").strip()
        if text and text not in values:
            values.append(text)
        if len(values) >= MAX_FILTER_VALUES:
            break
    if not values:
        return None
    quoted = ", ".join(f'"{escape_milvus_string(value)}"' for value in values)
    return f"{ITEM_NAME_FILTER_FIELD} in [{quoted}]"


def combine_filters(*expressions: str | None) -> str | None:
    """用 and 串起多个过滤条件，忽略空条件。"""
    parts = [expression for expression in expressions if expression]
    return " and ".join(parts) if parts else None


def normalize_hits(result: Any) -> list[dict[str, Any]]:
    """兼容 MilvusClient 返回的 entity 嵌套和扁平两种结果格式。

    统一补上 source=local 与 vector_score，使多路召回在进入 RRF 前结构一致；
    没有 chunk_id 的结果直接丢弃，因为下游无法凭它回填来源。
    """
    if not result:
        return []
    hits = result[0] if isinstance(result, list) and result and isinstance(result[0], list) else result
    normalized: list[dict[str, Any]] = []
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        entity = hit.get("entity") if isinstance(hit.get("entity"), dict) else hit
        item = dict(entity)
        if "chunk_id" not in item and hit.get("id") is not None:
            item["chunk_id"] = hit["id"]
        if hit.get("distance") is not None:
            item["vector_score"] = float(hit["distance"])
        item.setdefault("source", "local")
        if item.get("chunk_id") is not None:
            normalized.append(item)
    return normalized
