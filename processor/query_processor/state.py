
"""
问答工作流状态定义。

processor/query_processor/state.py
统一声明导入问答节点之间传递的查询、检索、答案和图片字段。
"""

from typing import TypedDict, List

# 来源宽度的唯一默认值。请求不传 top_k、接口层缺省、以及任何节点在 state 里读不到 top_k 时，
# 都落到这一个常量上。改之前这个 5 抄在了四个节点和接口 schema 里各写一遍，抬一档要同时改
# 五处，而漏改任何一处的表现不是报错，是“候选按 10 条预算取、窗口只给 5 条”这种口径漂移 ——
# 比默认值取多少难得多发现。上界另由 node_rerank.RERANK_MAX_TOPK 兜住（防客户端把 top_k
# 放大后精排成本与来源面板长度失控），所以这里给到 10 就等于“默认读满允许的证据宽度”。
DEFAULT_TOP_K: int = 10

class QueryGraphState(TypedDict, total=False):
    """
    查询流程图状态
    包含整个查询流程中传递的所有数据。
    """

    session_id: str  # 会话ID
    message_id: str  # 消息ID

    original_query: str  # 用户原始问题
    library_id: int  # 当前检索知识库 ID
    top_k: int  # 最终返回来源数量（缺省见 DEFAULT_TOP_K，夹在 RERANK_MIN_TOPK 与 RERANK_MAX_TOPK 之间）
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
    # 元数据画像读失败（与“库里没有这些内容”严格区分：把系统故障说成事实否定是用户拿去做决定的错信息）。
    # 登记进 node_answer_output.RETRIEVAL_FAILURE_MARKERS（键名以 _failed 结尾的集合被静态守卫钉住全等）。
    metadata_query_failed: bool
    sources: list  # 面向用户端的来源列表

    # 生成过程中的数据
    prompt: str  # 组装好的 Prompt
    answer: str  # 最终生成的答案
    image_urls: list  # 答案关联的图片 URL 列表

    # 辅助信息
    item_names: List[str]  # 提取出的商品名称
    intent: str  # 意图标签，**取值集合的唯一来源是 processor/query_processor/intents.py 的 ALL_INTENTS**（全等静态守卫钉住，新增取值不登记就红）：chitchat / inventory_refusal（R16 旧拒答，仅回滚开关下出现）/ conversation_echo / meta_rewrite（这两者中 meta_rewrite 不短路）/ metadata_entity / metadata_document / metadata_stats / metadata_config / metadata_unsupported / metadata_unavailable；其余为正常知识问答（**不写 intent**，保持键缺失）
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
    # 确认闸的严格程度（库级字段，跟 entity_recognition_mode 同层）：strict = 确认不了就问，
    # lenient = 只筛选不反问（改造前那条链）。键缺失按 strict 处理，理由见 entity_confirm.resolve_confirm_mode：
    # 接口漏注入一个键就把闸静默关掉，比多问一句难发现得多。
    entity_confirm_mode: str
    # 本轮发反问时列出的候选清单：不参与任何判定，只供接口把它落到本轮助手历史消息上，
    # 下一轮才能认出“用户在回答哪一问”（反问闭环，见 entity_confirm.last_clarify_round）。
    # 必须在这里声明：TypedDict 没列出的键会被 LangGraph 静默丢弃，丢了就是闭环接不上。
    entity_clarify_candidates: list

    # 库元数据画像（接口层按需装载，图内只读不查）：回答“本库有哪些产品/多少份文档/开了 HyDE 没”
    # 这类库级元问题。装载条件与口径见 backend/app/api/library_facts.py；未装载时是一个
    # `{"loaded": False}` 的壳而不是缺键，图里的闸据此把“没装载”与“库里没数据”分开处理。
    # 为什么不在图里直接查库：查询图跑在 asyncio.to_thread 的工作线程，MySQL 只有异步引擎且连接池
    # 绑事件循环，图内读库要么跨 loop 要么死锁（详细推导写在那份模块的 docstring 里）。
    # 必须在这里声明：TypedDict 没列出的键会被 LangGraph 静默丢弃。
    library_facts: dict

    # 诊断埋点（先量后治的第一步）：只记录本轮实体对齐走了哪条出口，供离线统计“线上问题实际漏在哪个出口”。
    # 这两个键**不参与任何判定**：检索、生成、接口与前端都不读它们，写它们不改变任何行为，
    # disabled 库同样落值（not_required）以便当对照组。取值集合见 node_prepare_query.ENTITY_ALIGN_EXITS（18 格），
    # 写入位置只有 NodePrepareQuery._mark_align_exit 一处（静态守卫钉住，防分支散落漏写）。
    entity_align_exit: str  # 出口标记；区分“没跑到对齐”（not_required/pre_gated）与“跑了没命中”（no_mention/empty_vocab/…）
    entity_align_signals: dict  # 与出口同时记录的中间量，全部取自判定过程本来就算过的值（mentions/品牌组/是否省略追问/词表规模/确认闸档位…）
