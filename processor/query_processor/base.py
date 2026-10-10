# processor/query_processor/base.py

"""
查询流程节点基类

定义统一的节点接口规范，提供通用功能
"""
import os
from abc import abstractmethod, ABC
from typing import TypeVar

from tool.logger import logger

T = TypeVar("T")  # 泛型状态类型
class NodeBase(ABC):

    name: str = "base_node"  # 节点名称，子类应覆盖

    @staticmethod
    def _env_disabled(name: str) -> bool:
        """环境变量开关：设成 1/true/yes 视为关闭该步（默认开启）。

        查询链路上多个可选守卫（重排碎片合并、话题一致性校验、图片语义否决…）共用这一套
        语义，线上要拿同一批请求做基线对比时不必改代码回滚。
        """
        return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}

    def __call__(self, state: T) -> T:
        """
        节点执行入口
        """
        try:
            # 1. 开始准备执行节点
            logger.info(f"--- {self.name} 开始啦 ---")

            # 2. 执行节点
            result = self.process(state)

            # 3. 执行节点成功
            logger.info(f"--- {self.name} 完成啦 ---")

            return result

        except Exception as e:
            logger.error(f"{self.name} 执行失败: {e}")
            raise

    @abstractmethod
    def process(self, state: T) -> T:
        """
        节点核心处理逻辑
        子类必须实现此方法
        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        pass
