"""
外部搜索补充节点。

processor/query_processor/nodes/node_web_search_mcp.py

通过 MCP 搜索服务补充联网结果，并将结果转换为统一检索文档结构。
"""
import asyncio
import json

from agents.mcp import MCPServerStreamableHttp

from config.bailian_mcp_config import mcp_config
from processor.query_processor.base import NodeBase
from processor.query_processor.state import QueryGraphState
from tool.logger import configure_logging, logger
from utils.json_format_utils import serialize_json

# 单次联网搜索的总预算：SDK 自带 timeout 只管单个请求，不带总超时会导致“重试3次 x 10s”拖住整个回答
MCP_TOTAL_TIMEOUT_SECONDS = 20
# 搜索条数上限：百炼侧单次可用量有限，拿太多只会拉高延迟和成本
MCP_MAX_RESULT_COUNT = 10


class NodeWebSearchMcp(NodeBase):
    """
    节点功能，调用外部搜索引擎补充信息
    """

    # 覆盖基类的 name 属性，标识节点名称
    name: str = "node_web_search_mcp"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        """联网检索，失败一律降级为“无外部结果”，不能拖垮整个问答请求。

        返回前统一成 {title, url, snippet, site_name} 结构，由 rerank 节点归入多源文档；
        web_search_failed 会随结果一并置位，便于前端和日志区分“没搜到”和“搜失败”。

        跟其他节点一样返回完整 state（写回而不是新建一个只含两个键的字典）。
        """
        query = str(state.get("rewritten_query") or state.get("original_query") or "").strip()
        if not query:
            logger.info("联网检索跳过：无有效问题")
            state["web_search_docs"] = []
            state["web_search_failed"] = False
            return state

        try:
            result = asyncio.run(asyncio.wait_for(
                self._mcp_call(query, count=self._resolve_count(state)),
                timeout=MCP_TOTAL_TIMEOUT_SECONDS,
            ))
            docs = self._parse_pages(result)
        except Exception as e:
            logger.error(f"联网检索失败，降级为仅用本地知识库结果继续回答: {e}")
            state["web_search_docs"] = []
            state["web_search_failed"] = True
            return state

        logger.info("联网检索补充 %s 条结果", len(docs))
        state["web_search_docs"] = docs
        state["web_search_failed"] = False
        return state

    @staticmethod
    def _resolve_count(state: QueryGraphState) -> int:
        """搜索条数跟着 top_k 走并留出重排余量，避免与用户可配的来源数量两套口径。"""
        top_k = int(state.get("top_k") or 5)
        return max(min(top_k * 2, MCP_MAX_RESULT_COUNT), 1)

    @staticmethod
    def _parse_pages(result) -> list[dict]:
        """把 MCP 返回的文本段解析成统一结构；字段缺失只跳过该条，不让整次搜索失败。"""
        content = getattr(result, "content", None) or []
        if not content:
            return []
        text = getattr(content[0], "text", None)
        if not text:
            return []
        payload = json.loads(text)
        pages = payload.get("pages") if isinstance(payload, dict) else None
        docs: list[dict] = []
        for item in pages or []:
            if not isinstance(item, dict):
                continue
            snippet = str(item.get("snippet") or "").strip()
            if not snippet:
                continue
            docs.append({
                "title": str(item.get("title") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "snippet": snippet,
                # 站点名比页面标题更适合做来源展示（页面标题常带一堆 SEO 后缀）
                "site_name": str(item.get("hostname") or "").strip(),
            })
        return docs

    async def _mcp_call(self, query: str, count: int = 5):
        """每次请求新建并回收一条 MCP 连接：避开跨事件循环复用连接的复杂度，代价是多一次握手。"""

        search_mcp = MCPServerStreamableHttp(
            name="search_mcp",
            params={
                "url": mcp_config.mcp_base_url,
                "headers": {"Authorization": f"Bearer {mcp_config.api_key}"},
                "timeout": 10,
            },
            cache_tools_list=True,
            max_retry_attempts=3,
        )

        try:
            await search_mcp.connect()
            return await search_mcp.call_tool(
                tool_name="bailian_web_search",
                arguments={"query": query, "count": count},
            )
        finally:
            try:
                await search_mcp.cleanup()
            except Exception as e:
                # 断连失败不能掩盖真正的搜索异常（否则排障时看到的是无关错误）
                logger.warning(f"关闭 MCP 连接失败: {e}")


if __name__ == "__main__":

    # 日志只由进程入口配置，手动跑本脚本时显式挂一次控制台 handler。
    configure_logging()
    init_state = {
        "rewritten_query": "今天北京天气怎么样"
    }

    # 执行节点的业务调用
    node_web_search_mcp = NodeWebSearchMcp()
    result = node_web_search_mcp(init_state)
    logger.info(serialize_json(result, indent=4))
