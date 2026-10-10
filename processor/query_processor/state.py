
"""
问答工作流状态定义。

processor/query_processor/state.py
统一声明导入问答节点之间传递的查询、检索、答案和图片字段。
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
    hyde_doc: str  # 大模型生成的假设性答案原文
    web_search_docs: list  # 网络搜索回来的文档

    # 排序过程中的数据
    rrf_chunks: list  # RRF 融合排序后的切片
    reranked_docs: list  # 重排序后的最终 Top-K 文档
    rerank_failed: bool  # 重排序服务是否失败并回退到原始召回
    embedding_search_failed: bool  # 本地主路检索是否故障（故障≠零命中：Milvus 异常时该路置空并留痕，不伪装成“没查到”）
    entity_filter_relaxed: bool  # 带实体过滤零召回后是否已放宽为“只带库隔离条件”重查过一次（放宽是兜底，既非故障也非零命中；下游增强检索与日志据此区分）
    hyde_failed: bool  # HyDE 链路是否失败（失败时该路召回为空，不阻断主链路）
    web_search_failed: bool  # 联网检索是否失败（失败时仅用本地结果继续回答）
    sources: list  # 面向用户端的来源列表

    # 生成过程中的数据
    prompt: str  # 组装好的 Prompt
    answer: str  # 最终生成的答案
    image_urls: list  # 答案关联的图片 URL 列表

    # 辅助信息
    item_names: List[str]  # 提取出的商品名称
    intent: str  # 意图标签：chitchat=命中闲聊意图闸；inventory_refusal=库级清单型元问题被前置拒答（两者都跳过检索与生成走固定话术）；conversation_echo=对话回指，同样短路但答案由历史回读而非固定话术；meta_rewrite=改写型元指令（不短路：只把检索词换成上一轮真问题，照常检索与生成）；其余为正常知识问答
    rewritten_query: str  # 改写后的问题（专供检索：会按历史补入实体，解决“它/这个”一类指代）
    query_entity_text: str  # 本轮用户原文（未经改写）：实体对齐的拒答判定只看它，改写串里回填的型号不算“用户点名了型号”
    meta_instruction: str  # 本轮是改写型元指令（“换个说法/用表格列一下”）时的指令原文：不进检索词，只作为呈现要求进生成 prompt
    history: list  # 历史对话记录
    is_stream: bool  # 是否流式输出
    persist_history: bool  # 是否由工作流写入 Mongo 历史（新 API 由接口统一保存）

    # 实体识别（检索侧词表对齐）
    # 模式由接口按知识库注入；required 库才对齐，disabled 库整段跳过保证零回归。
    entity_recognition_mode: str  # 该库实体识别模式：disabled / required
    entity_vocabulary: list  # required 库的实体词表 [{"name": str, "aliases": [str]}]，由接口异步载入并注入

    # 诊断埋点（先量后治的第一步）：只记录本轮实体对齐走了哪条出口，供离线统计“线上问题实际漏在哪个出口”。
    # 这两个键**不参与任何判定**：检索、生成、接口与前端都不读它们，写它们不改变任何行为，
    # disabled 库同样落值（not_required）以便当对照组。取值集合见 node_prepare_query.ENTITY_ALIGN_EXITS，
    # 写入位置只有 NodePrepareQuery._mark_align_exit 一处（静态守卫钉住，防分支散落漏写）。
    entity_align_exit: str  # 出口标记；区分“没跑到对齐”（not_required/pre_gated）与“跑了没命中”（no_mention/empty_vocab/…）
    entity_align_signals: dict  # 与出口同时记录的中间量，全部取自判定过程本来就算过的值（mentions/品牌组/是否省略追问/词表规模…）
