"""新版知识库导入 LangGraph 工作流。"""

from typing import Any

from langgraph.constants import END
from langgraph.graph import StateGraph

from processor.import_processor.base import setup_logging
from processor.import_processor.exceptions import ValidationError
from processor.import_processor.import_config import ImportConfig, get_config
from processor.import_processor.nodes.node_bge_embedding import NodeBGEEmbedding
from processor.import_processor.nodes.node_document_split import NodeDocumentSplit
from processor.import_processor.nodes.node_entity_recognition_disabled import NodeEntityRecognitionDisabled
from processor.import_processor.nodes.node_entity_recognition_optional import NodeEntityRecognitionOptional
from processor.import_processor.nodes.node_entity_recognition_required import NodeEntityRecognitionRequired
from processor.import_processor.nodes.node_entry import NodeEntry
from processor.import_processor.nodes.node_import_milvus import NodeImportMilvus
from processor.import_processor.nodes.node_md_img import NodeMDImg
from processor.import_processor.nodes.node_pdf_to_md import NodePDFToMD
from processor.import_processor.nodes.node_persist_chunks import NodePersistChunks
from processor.import_processor.state import EntityRecognitionMode, ImportGraphState, get_default_state


ENTITY_RECOGNITION_NODE_NAMES: dict[str, str] = {
    "disabled": "node_entity_recognition_disabled",
    "optional": "node_entity_recognition_optional",
    "required": "node_entity_recognition_required",
}


class KBImportWorkflow:
    """设备技术文档导入工作流。"""

    def __init__(self, config: ImportConfig | None = None):
        """初始化工作流并保存本次执行使用的配置对象。"""
        self.config = config or get_config()
        self._compiled_graph = None

    @property
    def graph(self):
        """懒加载并返回编译后的 LangGraph。"""
        if self._compiled_graph is None:
            self._compiled_graph = self.build_graph()
        return self._compiled_graph

    @staticmethod
    def route_after_entry(state: ImportGraphState) -> str:
        """根据文件类型选择 PDF 解析、Markdown 处理或结束流程。"""
        if state.get("is_pdf_read_enabled"):
            return "node_pdf_to_md"
        if state.get("is_md_read_enabled"):
            return "node_md_img"
        return END

    @staticmethod
    def route_after_document_split(state: ImportGraphState) -> str:
        """根据知识库实体识别模式选择对应节点。"""
        mode = state.get("entity_recognition_mode") or "disabled"
        if mode not in ENTITY_RECOGNITION_NODE_NAMES:
            raise ValidationError(
                message=f"不支持的实体识别模式：{mode}",
                node_name="route_after_document_split",
            )
        return ENTITY_RECOGNITION_NODE_NAMES[mode]

    def build_graph(self):
        """创建并编译新版三模式导入图。"""
        graph = StateGraph(ImportGraphState)

        graph.add_node("node_entry", NodeEntry(config=self.config))
        graph.add_node("node_pdf_to_md", NodePDFToMD(config=self.config))
        graph.add_node("node_md_img", NodeMDImg(config=self.config))
        graph.add_node("node_document_split", NodeDocumentSplit(config=self.config))
        graph.add_node("node_entity_recognition_disabled", NodeEntityRecognitionDisabled(config=self.config))
        graph.add_node("node_entity_recognition_optional", NodeEntityRecognitionOptional(config=self.config))
        graph.add_node("node_entity_recognition_required", NodeEntityRecognitionRequired(config=self.config))
        graph.add_node("node_bge_embedding", NodeBGEEmbedding(config=self.config))
        graph.add_node("node_import_milvus", NodeImportMilvus(config=self.config))
        graph.add_node("node_persist_chunks", NodePersistChunks(config=self.config))

        graph.set_entry_point("node_entry")
        graph.add_conditional_edges(
            "node_entry",
            self.route_after_entry,
            {
                "node_md_img": "node_md_img",
                "node_pdf_to_md": "node_pdf_to_md",
                END: END,
            },
        )

        graph.add_edge("node_pdf_to_md", "node_md_img")
        graph.add_edge("node_md_img", "node_document_split")
        graph.add_conditional_edges(
            "node_document_split",
            self.route_after_document_split,
            {
                "node_entity_recognition_disabled": "node_entity_recognition_disabled",
                "node_entity_recognition_optional": "node_entity_recognition_optional",
                "node_entity_recognition_required": "node_entity_recognition_required",
            },
        )
        graph.add_edge("node_entity_recognition_disabled", "node_bge_embedding")
        graph.add_edge("node_entity_recognition_optional", "node_bge_embedding")
        graph.add_edge("node_entity_recognition_required", "node_bge_embedding")
        graph.add_edge("node_bge_embedding", "node_import_milvus")
        graph.add_edge("node_import_milvus", "node_persist_chunks")
        graph.add_edge("node_persist_chunks", END)
        return graph.compile()

    def run(self, state: ImportGraphState, stream: bool = False):
        """合并默认状态后执行导入图，可选择流式返回节点事件。"""
        initial_state = get_default_state()
        initial_state.update(state)
        if stream:
            return self.graph.stream(initial_state)
        return self.graph.invoke(initial_state)


if __name__ == "__main__":
    setup_logging()
    init_state: ImportGraphState = {
        "task_id": "graph-test",
        "entity_recognition_mode": "disabled",
        "import_file_path": r"E:\doc\hak180使用说明书.pdf",
        "file_dir": r"E:\output",
    }
    workflow = KBImportWorkflow()
    for event in workflow.run(init_state, stream=True):
        print(event)
