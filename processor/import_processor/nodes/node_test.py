"""
文档导入节点测试样例。

@FilePath: processor/import_processor/nodes/node_test.py
@Date: 2026-09-07
@Description: 验证导入节点基类、状态传递和步骤日志机制，不参与生产导入流程。
"""

import logging
from typing import Dict

from processor.import_processor.base import BaseNode, setup_logging
from processor.import_processor.exceptions import ImportProcessError


# --- 定义测试节点 (继承 BaseNode) ---
class TestNode(BaseNode):
    name = "test_node"  # 覆盖基类的 name

    def process(self, state: Dict) -> Dict:
        """
        实现具体的 process 逻辑。
        这里我们简单地修改状态，添加一个键值对，并记录一个步骤日志。
        """
        # 模拟业务逻辑：修改状态
        state["processed_by"] = self.name

        # 使用基类提供的 log_step 方法
        self.log_step("数据清洗", "移除空值完成")
            # self.logger.debug(f"{self.name}debug")
        return state


# --- 4. 测试入口 ---
if __name__ == "__main__":
    # 1. 初始化日志 (显示 INFO 级别){激活}
    setup_logging(logging.INFO)
    # 2. 创建节点实例,只加圆括号,相当于调用对象call方法
    node = TestNode()

    # 3. 准备初始状态
    initial_state = {"data": [1, 2, 3], "status": "raw"}

    print("初始状态:", initial_state)
    print("\n开始执行节点...\n")

    # 4. 调用 __call__ 方法 (LangGraph 或工作流引擎通常就是这样调用的)
    try:
        final_state = node(initial_state)
        print("\n最终状态:", final_state)
    except ImportProcessError as e:
        print("捕获到流程错误:", e)
