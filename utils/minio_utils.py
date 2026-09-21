"""MinIO 客户端工具，采用按需连接避免导入模块时访问外部服务。"""

import json
from threading import Lock

from minio import Minio

from config.minio_config import minio_config

_minio_client: Minio | None = None
_minio_initialized = False
_minio_lock = Lock()


def _initialize_minio_client() -> Minio | None:
    """创建 MinIO 客户端并初始化存储桶策略。"""
    global _minio_client, _minio_initialized
    if _minio_initialized:
        return _minio_client

    with _minio_lock:
        if _minio_initialized:
            return _minio_client
        try:
            client = Minio(
                endpoint=minio_config.endpoint,
                access_key=minio_config.access_key,
                secret_key=minio_config.secret_key,
                secure=False,
            )
            if not client.bucket_exists(minio_config.bucket_name):
                client.make_bucket(minio_config.bucket_name)
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{minio_config.bucket_name}/*"],
                    }
                ],
            }
            client.set_bucket_policy(minio_config.bucket_name, json.dumps(policy))
            _minio_client = client
        except Exception as exc:
            print(f"MinIO 初始化失败：{exc}")
            _minio_client = None
        finally:
            _minio_initialized = True
    return _minio_client


def get_minio_client() -> Minio | None:
    """按需返回 MinIO 客户端，首次调用时才连接外部服务。"""
    return _initialize_minio_client()


if __name__ == "__main__":
    if get_minio_client():
        print("MinIO 初始化成功")
