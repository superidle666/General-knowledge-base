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

mineru_config = MineruConfig(
    base_url=os.getenv("MINERU_BASE_URL"),
    api_token=os.getenv("MINERU_API_TOKEN")
)
# 配置对象由环境变量初始化，避免在源码中保存服务凭据。
