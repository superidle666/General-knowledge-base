"""
文档导入入口节点。

@FilePath: processor/import_processor/nodes/node_entry.py
@Date: 2026-09-07
@Description: 校验导入文件并初始化 PDF、Markdown 处理流程所需的状态字段。
"""
import json
import logging
import sys
from pathlib import Path

from processor.import_processor.base import BaseNode, setup_logging
from processor.import_processor.exceptions import StateFieldError, FileProcessingError, ValidationError
from processor.import_processor.state import ImportGraphState


class NodeEntry(BaseNode):
    """
    入口节点：任务分发
    """

    name = "node_entry"

    def process(self, state: ImportGraphState):
        """Validate the uploaded file and initialize PDF or Markdown state fields."""
        #从state中获取文件绝对路径
        import_file_path = state.get("import_file_path")
        #判断路径是否为空
        if not import_file_path:
            raise StateFieldError(
                node_name=self.name,
                field_name="import_file_path",
                message="文件内容不能为空",
                expected_type=str,
                                  )
        # 2. 转换Path标准化对象
        import_file_path_obj = Path(import_file_path)
        # 判断文件是否存在
        if not import_file_path_obj.exists():
            raise FileProcessingError(message=f"文件{import_file_path_obj.name}不存在")
        #判断文件类型
        suffix = import_file_path_obj.suffix.lower()
        if suffix == ".pdf":
            state["is_pdf_read_enabled"] = True
            state["pdf_path"] = import_file_path
        elif suffix == ".md":
            state["is_md_read_enabled"] = True
            state["md_path"] = import_file_path
            try:
                state["md_content"] = import_file_path_obj.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise FileProcessingError(
                    message=f"Markdown 文件{import_file_path_obj.name}不是 UTF-8 编码"
                ) from error
        else:
            raise ValidationError(message=f"该文件的后缀格式{import_file_path_obj.suffix}不支持")
        # 4. 获取上传文件的标题，更新到state中
        state["file_title"] = import_file_path_obj.stem


        return state


#单元测试
if __name__ == "__main__":
    #激活日志的全局配置
    setup_logging()
    #初始化图状态
    init_state = {
        "import_file_path":r"E:\doc\H3C LA2608室内无线网关 用户手册-6W100-整本手册.pdf"
    }
    #创建节点
    node_entry = NodeEntry()
    #执行节点的单元测试
    result = node_entry(init_state)
    json_state =  json.dumps(result, ensure_ascii=False, indent=4)
    logging.getLogger().info(json_state)
