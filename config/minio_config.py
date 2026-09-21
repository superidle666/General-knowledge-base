"""
MinIO 对象存储配置。

@FilePath: config/minio_config.py
@Date: 2026-09-07
@Description: 加载对象存储连接信息、知识库桶名称和文档图片目录。
"""

from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass
class MinIOConfig:
    endpoint: str
    access_key: str
    secret_key: str
    bucket_name: str
    img_dir: str

minio_config = MinIOConfig(
    endpoint=os.getenv("MINIO_ENDPOINT"),
    access_key=os.getenv("MINIO_ACCESS_KEY"),
    secret_key=os.getenv("MINIO_SECRET_KEY"),
    bucket_name=os.getenv("MINIO_BUCKET_NAME"),
    img_dir=os.getenv("MINIO_IMG_DIR", "upload-images").strip() or "upload-images",
)
