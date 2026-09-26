# knowledge/processor/import_process/import_config.py

"""
导入流程配置管理模块

只放导入流程特有的阈值。连接类参数（MinerU / OpenAI / Milvus / MinIO / 向量维度）
一律由 config/* 提供，这里不再重复一份——同一参数两处定义时，改配置不生效的问题
只能靠人记住哪一处才是真的生效（P1-8 修的图片限流口径就是这一类）。
"""

from dataclasses import dataclass, field
from typing import Set, Optional
import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv(override=True)


@dataclass
class ImportConfig:
    """导入流程配置"""

    # ==================== 文档处理配置 ====================
    max_content_length: int = 2000      # 切片最大长度
    min_content_length: int = 500       # 合并短内容的最小长度
    item_name_chunk_k: int = 5          # 主实体枚举时的采样切片数量（首切片 + 均匀取）
    item_name_chunk_size: int = 800     # 单个采样切片注入模型的内容截断上限
    entity_enum_char_budget: int = 4000  # 主实体枚举拼接上下文的总字符预算，防止超长上下文

    # 支持的图片扩展名
    image_extensions: Set[str] = field(
        default_factory=lambda: {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
    )

    # ==================== LLM 配置 ====================
    # 仅实体识别节点用这一个（它按模型名选实体模型）；其余模型参数走 config/lm_config.py。
    # 给空串兜底而不是 None：类型标的是 str，取值处已用 `or None` 处理空值。
    item_model: str = field(
        default_factory=lambda: os.getenv("ITEM_MODEL", "")
    )

    # ==================== 向量配置 ====================
    embedding_batch_size: int = 8

    # ==================== 速率限制 ====================
    # 图片总结 API 速率限制。作用域是“每个任务一个滑动窗口”，不是账号级全局窗口。
    requests_per_minute: int = 15

    @classmethod
    def from_env(cls) -> "ImportConfig":
        """从环境变量加载配置"""
        return cls()


# ==================== 全局单例 ====================
_config: Optional[ImportConfig] = None


def get_config() -> ImportConfig:
    """获取配置单例"""
    global _config
    if _config is None:
        _config = ImportConfig.from_env()
    return _config
