"""
MinerU 文档解析服务配置。

@FilePath: config/mineru_config.py
@Date: 2026-09-07
@Description: 加载 PDF 转 Markdown 服务的地址和访问令牌。
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# 定义minerU服务配置
@dataclass
class MineruConfig:
    base_url: str
    api_token : str

    # ==================== 外部请求超时与轮询参数 ====================
    # requests 的 timeout 是“连接/单次读取”秒数，不是全程预算；不设超时则 MinerU
    # 卡住会把导入线程永久阻塞，且 worker 心跳仍在续租，过期恢复永远不会触发。
    connect_timeout_seconds: int = 5       # 所有外部请求共用的连接超时
    read_timeout_seconds: int = 60         # 普通 JSON 请求（取上传链接/上传文件/查任务）读取超时
    poll_read_timeout_seconds: int = 10    # 轮询单次短超时，超时后由外层循环继续重试
    zip_read_timeout_seconds: int = 120    # 流式下载解析结果 ZIP 包的读取超时
    poll_total_timeout_seconds: int = 600  # 轮询等待解析完成的最长总时长
    poll_interval_seconds: int = 3         # 轮询间隔
    model_version: str = "vlm"             # MinerU 解析使用的模型版本

mineru_config = MineruConfig(
    base_url=os.getenv("MINERU_BASE_URL"),
    api_token=os.getenv("MINERU_API_TOKEN")
)
# 配置对象由环境变量初始化，避免在源码中保存服务凭据。
