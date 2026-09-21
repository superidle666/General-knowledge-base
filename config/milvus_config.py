"""
Milvus 向量数据库配置。

@FilePath: config/milvus_config.py
@Date: 2026-09-07
@Description: 加载向量数据库地址和各类知识集合名称。
"""

from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass
class MilvusConfig:
    milvus_url: str
    chunks_collection: str
    document_chunks_v2_collection: str
    item_name_collection: str

# 实例化Milvus配置对象（和其他配置对象命名风格统一）
milvus_config = MilvusConfig(
    milvus_url=os.getenv("MILVUS_URL"),
    chunks_collection=os.getenv("CHUNKS_COLLECTION"),
    document_chunks_v2_collection=os.getenv("DOCUMENT_CHUNKS_V2_COLLECTION", "kb_document_chunks_v2"),
    item_name_collection=os.getenv("ITEM_NAME_COLLECTION")
)
