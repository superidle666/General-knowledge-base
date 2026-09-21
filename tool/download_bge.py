"""
BGE-M3 模型下载脚本。

@FilePath: tool/download_bge.py
@Date: 2026-09-07
@Description: 从 ModelScope 下载 BGE-M3 模型到本地缓存目录。
"""

from modelscope.hub.snapshot_download import snapshot_download

model_dir = snapshot_download('BAAI/bge-m3', cache_dir=r'E:/ai_models/modelscope_cache/models')
print(f"模型已下载到: {model_dir}")
