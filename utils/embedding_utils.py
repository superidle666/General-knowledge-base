"""
文本向量化工具。
utils/embedding_utils.py
管理 BGE-M3 Embedding 单例并生成稠密、稀疏向量。
"""

import threading
import time

from pymilvus.model.hybrid import BGEM3EmbeddingFunction

from config.embedding_config import embedding_config
from tool.logger import logger

# 此处曾在模块导入期调用 setup_logging()：任何 import 本文件的进程（包括问答 API）
# 都会被顺手重置根日志器，还让 utils 反向依赖了 processor.*。日志配置只由进程入口做。

# 模型单例对象，避免重复初始化
_bge_m3_ef = None
# 双检锁。只判空不加锁时，并发首查会各自加载一份 BGE-M3（约 2GB）：查询侧（FastAPI
# 请求线程）与导入侧（Worker 线程池）共用这一个入口，谁先到都看得到 None，于是
# 峰值内存按并发数翻倍，而最后只有一份被留下，其余变垃圾。
_bge_m3_ef_lock = threading.Lock()


def get_bge_m3_ef():
    """
    获取BGE-M3模型单例对象，自动加载环境变量配置

    首次调用会加载模型（可能要几十秒），期间其他线程在锁上排队，加载只发生一次。
    :return: 初始化完成的BGEM3EmbeddingFunction实例
    """
    global _bge_m3_ef
    if _bge_m3_ef is not None:
        # 快路径：模型已就绪时不去抢锁，否则高并发下所有请求都要在这把锁上串行
        return _bge_m3_ef

    with _bge_m3_ef_lock:
        # 双检：排队等锁期间可能已经有人加载完了，直接复用它的结果
        if _bge_m3_ef is not None:
            return _bge_m3_ef

        # 从环境变量加载配置
        model_name = embedding_config.bge_m3_path
        device = embedding_config.bge_device
        use_fp16 = embedding_config.bge_fp16

        logger.info(
            "首次加载 BGE-M3 模型（其他线程会等待）：model=%s device=%s fp16=%s",
            model_name, device, use_fp16,
        )
        started_at = time.perf_counter()
        # 如果模型没有被提前下载，会自动下载
        model = BGEM3EmbeddingFunction(
            model_name=model_name,
            device=device,
            use_fp16=use_fp16
        )
        # 构造成功才发布单例：中途抛错时保持未加载状态，下次调用还能重试，
        # 不会把一个不可用的半成品永久缓存下来
        _bge_m3_ef = model
        logger.info("BGE-M3 模型加载完成，耗时 %.1f 秒", time.perf_counter() - started_at)
        return _bge_m3_ef

def generate_embeddings(texts ):
    """
    为文本生成向量嵌入
    :param texts: 要生成嵌入的文本列表
    :return: 包含dense和sparse向量的字典
    """
    model = get_bge_m3_ef()
    embeddings = model.encode_documents(texts)
    processed_sparse = []
    for i in range(len(texts)):
        sparse_indices = embeddings["sparse"].indices[embeddings["sparse"].indptr[i]:embeddings["sparse"].indptr[i+1]].tolist()
        sparse_data = embeddings["sparse"].data[embeddings["sparse"].indptr[i]:embeddings["sparse"].indptr[i+1]].tolist()
        sparse_dict = {k: v for k, v in zip(sparse_indices,sparse_data)}
        processed_sparse.append(sparse_dict)

    return {
        "dense": [emb.tolist() for emb in embeddings["dense"]],
        "sparse": processed_sparse
    }
