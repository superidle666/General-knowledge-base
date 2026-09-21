"""知识库查询工作流。"""

from dotenv import load_dotenv
from langgraph.constants import END
from langgraph.graph import StateGraph

from processor.query_processor.nodes.node_answer_output import NodeAnswerOutput
from processor.query_processor.nodes.node_prepare_query import NodePrepareQuery
from processor.query_processor.nodes.node_rerank import NodeRerank
from processor.query_processor.nodes.node_rrf import NodeRrf
from processor.query_processor.nodes.node_search_embedding import NodeSearchEmbedding
from processor.query_processor.nodes.node_search_embedding_hyde import NodeSearchEmbeddingHyde
from processor.query_processor.nodes.node_source_collection import NodeSourceCollection
from processor.query_processor.nodes.node_web_search_mcp import NodeWebSearchMcp
from processor.query_processor.state import QueryGraphState

load_dotenv()


class KBQueryWorkflow:
    """默认执行本地检索，可按状态开关启用 HyDE 和联网搜索。"""

    def __init__(self):
        self.workflow = StateGraph(QueryGraphState)
        self.node_prepare_query = NodePrepareQuery()
        self.node_search_embedding = NodeSearchEmbedding()
        self.node_search_embedding_hyde = NodeSearchEmbeddingHyde()
        self.node_web_search_mcp = NodeWebSearchMcp()
        self.node_rrf = NodeRrf()
        self.node_rerank = NodeRerank()
        self.node_source_collection = NodeSourceCollection()
        self.node_answer_output = NodeAnswerOutput()
        self._register_nodes()
        self._setup_routes()
        self._compiled_app = None

    def _register_nodes(self) -> None:
        self.workflow.add_node("node_prepare_query", self.node_prepare_query)
        self.workflow.add_node("node_search_embedding", self.node_search_embedding)
        self.workflow.add_node("node_search_embedding_hyde", self.node_search_embedding_hyde)
        self.workflow.add_node("node_web_search_mcp", self.node_web_search_mcp)
        self.workflow.add_node("node_rrf", self.node_rrf)
        self.workflow.add_node("node_rerank", self.node_rerank)
        self.workflow.add_node("node_source_collection", self.node_source_collection)
        self.workflow.add_node("node_answer_output", self.node_answer_output)

    def _setup_routes(self) -> None:
        self.workflow.set_entry_point("node_prepare_query")
        self.workflow.add_conditional_edges(
            "node_prepare_query",
            self._route_after_prepare,
            {"search": "node_search_embedding", "finish": "node_answer_output"},
        )
        self.workflow.add_conditional_edges(
            "node_search_embedding",
            self._route_after_local_search,
            {"hyde": "node_search_embedding_hyde", "web": "node_web_search_mcp", "rank": "node_rrf"},
        )
        self.workflow.add_conditional_edges(
            "node_search_embedding_hyde",
            self._route_after_hyde,
            {"web": "node_web_search_mcp", "rank": "node_rrf"},
        )
        self.workflow.add_edge("node_web_search_mcp", "node_rrf")
        self.workflow.add_edge("node_rrf", "node_rerank")
        self.workflow.add_edge("node_rerank", "node_source_collection")
        self.workflow.add_conditional_edges(
            "node_source_collection",
            self._route_after_sources,
            {"answer": "node_answer_output", "finish": "node_answer_output"},
        )
        self.workflow.add_edge("node_answer_output", END)

    @staticmethod
    def _route_after_prepare(state: QueryGraphState) -> str:
        return "finish" if state.get("answer") else "search"

    @staticmethod
    def _route_after_local_search(state: QueryGraphState) -> str:
        if state.get("use_hyde"):
            return "hyde"
        if state.get("use_web_search"):
            return "web"
        return "rank"

    @staticmethod
    def _route_after_hyde(state: QueryGraphState) -> str:
        return "web" if state.get("use_web_search") else "rank"

    @staticmethod
    def _route_after_sources(state: QueryGraphState) -> str:
        return "answer"

    def compile(self):
        if self._compiled_app is None:
            self._compiled_app = self.workflow.compile()
        return self._compiled_app

    def run(self, initial_state: QueryGraphState, stream: bool = False):
        app = self.compile()
        return app.stream(initial_state) if stream else app.invoke(initial_state)
