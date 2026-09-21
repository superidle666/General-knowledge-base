"""
百炼 MCP 服务配置。

@FilePath: config/bailian_mcp_config.py
@Date: 2026-09-07
@Description: 从环境变量加载外部 MCP 搜索服务地址和访问凭据。
"""

# 导入核心依赖：数据类、环境变量读取、路径处理
from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()


# 定义mcp的服务配置
@dataclass
class McpConfig:
    mcp_base_url: str
    api_key : str

mcp_config = McpConfig(
    mcp_base_url=os.getenv("MCP_DASHSCOPE_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY")
)
