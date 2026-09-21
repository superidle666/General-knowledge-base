
"""
问答工作流状态定义。

@FilePath: processor/query_processor/state.py
@Date: 2026-09-07
@Description: 统一声明导入问答节点之间传递的查询、检索、答案和图片字段。
"""

from typing import TypedDict, List

class QueryGraphState(TypedDict, total=False):
    """
    查询流程图状态
    包含整个查询流程中传递的所有数据。
    """

    session_id: str  # 会话ID
    message_id: str  # 消息ID

    original_query: str  # 用户原始问题
    library_id: int  # 当前检索知识库 ID
    top_k: int  # 最终返回来源数量
    retrieval_top_k: int  # Milvus 初始召回数量
    use_hyde: bool  # 是否启用 HyDE 检索
    use_web_search: bool  # 是否启用联网检索

    # 检索过程中的中间数据
    embedding_chunks: list  # 普通向量检索回来的切片
    hyde_embedding_chunks: list  # 已向量化的假设性问题切片
    web_search_docs: list  # 网络搜索回来的文档

    # 排序过程中的数据
    rrf_chunks: list  # RRF 融合排序后的切片
    reranked_docs: list  # 重排序后的最终 Top-K 文档
    rerank_failed: bool  # 重排序服务是否失败并回退到原始召回
    sources: list  # 面向用户端的来源列表

    # 生成过程中的数据
    prompt: str  # 组装好的 Prompt
    answer: str  # 最终生成的答案
    image_urls: list  # 答案关联的图片 URL 列表

    # 辅助信息
    item_names: List[str]  # 提取出的商品名称
    rewritten_query: str  # 改写后的问题
    history: list  # 历史对话记录
    is_stream: bool  # 是否流式输出
    persist_history: bool  # 是否由工作流写入 Mongo 历史（新 API 由接口统一保存）
