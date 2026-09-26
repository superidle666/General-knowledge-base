"""知识库查询工作流。"""

import threading

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
        # 来源整理后没有分支：不管有没有来源都要出答案（无来源时由答案节点给兜底话术）。
        # 这里原先是一条条件边，但两个分支指向同一个节点、路由函数恒返回同一个值，
        # 留着只会让人误以为此处能分流（P2-6）。
        self.workflow.add_edge("node_source_collection", "node_answer_output")
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

    def compile(self):
        if self._compiled_app is None:
            self._compiled_app = self.workflow.compile()
        return self._compiled_app

    def run(self, initial_state: QueryGraphState, stream: bool = False):
        app = self.compile()
        return app.stream(initial_state) if stream else app.invoke(initial_state)


# 图与节点都不携带请求态：节点实例里没有任何 self.X = 赋值，所有中间结果都住在
# state 字典里，编译好的图可以并发复用。以前接口层每个请求都 new 一个 KBQueryWorkflow
# （每请求 8 个节点对象 + 一次 StateGraph 组装 + 一次 compile），纯属重复劳动。
_shared_workflow: "KBQueryWorkflow | None" = None
_shared_workflow_lock = threading.Lock()


def get_kb_query_workflow() -> KBQueryWorkflow:
    """取进程内共享的查询工作流（图只组装与编译一次，每请求只新建 state 字典）。

    双检锁与 P1-9 的模型单例同一套口径：工作流是在 asyncio.to_thread 的工作线程里
    跑的，并发首请求会各自进入这里；不互斥的话先建的那份会被后建的顶掉。
    """
    global _shared_workflow
    if _shared_workflow is not None:  # 快路径：图已就绪时不去抢锁
        return _shared_workflow
    with _shared_workflow_lock:
        if _shared_workflow is None:  # 锁内复查：排队等锁期间可能已经有人建好了
            workflow = KBQueryWorkflow()
            workflow.compile()  # 先编译再发布，取到的实例一定带着可用的图
            _shared_workflow = workflow
        return _shared_workflow
