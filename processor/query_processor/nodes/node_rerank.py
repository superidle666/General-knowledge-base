"""
检索结果重排序节点。

@FilePath: processor/query_processor/nodes/node_rerank.py
@Date: 2026-09-07
@Description: 对多路召回结果进行精排和断崖截断，输出最终知识片段集合。
"""
from typing import Dict, Any, List

from processor.query_processor.base import NodeBase
from processor.query_processor.state import QueryGraphState
from tool.logger import configure_logging, logger
from utils.json_format_utils import serialize_json
from utils.reranker_http_utils import rerank_documents

# -----------------------------
# Rerank / TopK 全局常量
# -----------------------------
# 动态 TopK 硬上限：最多取前 N 条（<=10）
RERANK_MAX_TOPK: int = 10
# 最小 TopK：至少保留前 N 条（>=1，且 <= RERANK_MAX_TOPK）
RERANK_MIN_TOPK: int = 3 #总数最少条数

# 断崖阈值（绝对，判断高分文档）
RERANK_GAP_ABS: float = 0.5
# 断崖阈值（相对，判断低分文档）
RERANK_GAP_RATIO: float = 0.25

# 送进交叉编码器的候选上限：精排成本随条数线性增长、切片又长，全量送会顶高在线问答的
# 首字延迟。本地按最终 top_k 的倍数留余量（断崖截断要能在尾部看到下降段才判得准），
# web 只取 top_k；两个绝对上限则防客户端把 top_k 放大后成本失控。
RERANK_LOCAL_CANDIDATE_MULTIPLIER: int = 3
RERANK_MAX_LOCAL_CANDIDATES: int = 30
RERANK_MAX_WEB_CANDIDATES: int = 10
# 单条切片送精排前的截断长度：只影响送给模型的文本，不影响最终送给大模型生成
# 的完整 content（那里另有 MAX_CONTEXT_CHARS 预算）。
RERANK_MAX_CONTENT_CHARS: int = 1000
# 精排失败降级时给 web 结果的固定分：本地切片按组内 RRF 分数归一到 0~1，1.0 是最强
# 本地命中、长尾接近 0；给 web 一个中位值意味着“比本地长尾可靠、但不压过最强本地资料”，
# 而不再像以前那样带着 None 被排到末位。
RERANK_FALLBACK_WEB_SCORE: float = 0.5

class NodeRerank(NodeBase):
    """
    节点功能：使用 Cross-Encoder 模型对 RRF 后的结果进行精确打分重排。
    """

    # 覆盖基类的 name 属性，标识节点名称
    name: str = "node_rerank"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        """
        执行重排序
        流程: 合并多源文档 → Reranker 计算相关性 → 断崖检测动态截断
        :param state: 需包含 rrf_chunks、web_search_docs、rewritten_query
        :return: 更新后的 state，包含 reranked_docs
        """

        # 1. 合并多源文档
        merged_multi_docs: List[Dict[str, Any]] = self._step_1_merge_multi_source_docs(state)

        if not merged_multi_docs:
            state['reranked_docs'] = []
            return state

        # 2. Rerank 精排(精排打分)
        reranked_docs: List[Dict[str, Any]] = self._step_2_rerank_merged_docs(state, merged_multi_docs)

        # 3. 动态 Top_K 截取(断崖检测)
        if state.get("rerank_failed"):
            # 降级分数只是召回序的投影，而断崖阈值是给交叉编码器的分数刻度定的，
            # 套在它上面等于用一把没校准的尺子做截断；直接取头部，窗口宽度与正常路径一致。
            cutoff_docs = reranked_docs[:RERANK_MAX_TOPK]
        else:
            cutoff_docs = self._step_3_cliff_cutoff(reranked_docs)

        # 4. 更新state
        state['reranked_docs'] = cutoff_docs

        # 5. 返回state
        return state

    def _step_1_merge_multi_source_docs(self, state: QueryGraphState) -> List[Dict[str, Any]]:
        """合并本地 RRF 结果和网络搜索结果为统一格式（并按精排预算截断候选数）。"""

        final_docs = []
        local_limit, web_limit = self._candidate_limits(state)

        # 1. 获取本地 RRF 的文档（已按 RRF 得分降序，取头部就是最相关的一批）
        for rrf_doc in (state.get('rrf_chunks') or [])[:local_limit]:
            format_rrf_doc = {**rrf_doc, "url": None, "source": "local"}
            final_docs.append(format_rrf_doc)

        # 2. 获取 web 远程的文档（搜索引擎自身已排序）
        for web_doc in (state.get('web_search_docs') or [])[:web_limit]:
            format_web_doc = {
                "content": web_doc.get('snippet'),
                "title": web_doc.get('title'),
                "chunk_id": None,
                "url": web_doc.get('url'),
                "source": "web",
                # 站点名随链路透传，供来源面板展示“这是哪家网站”而不是重复一遍页面标题
                "site_name": web_doc.get('site_name'),
            }
            final_docs.append(format_web_doc)

        return final_docs

    @staticmethod
    def _candidate_limits(state: QueryGraphState) -> tuple[int, int]:
        """算出本地/web 各送多少条进精排。

        本地下限抬到 RERANK_MAX_TOPK：断崖截断最多也就能输出这么多条，候选少于它就是
        纯削召回（小 top_k 的请求会因此比改前看到更少的文档）。web 只取 top_k 几条，
        但它仍要与本地同场竞价，所以也留个最低条数避免被一两条搜索结果的顺序定死。
        """
        try:
            top_k = int(state.get('top_k') or 5)
        except (TypeError, ValueError):
            top_k = 5
        top_k = max(1, top_k)
        local_limit = max(
            RERANK_MAX_TOPK,
            min(top_k * RERANK_LOCAL_CANDIDATE_MULTIPLIER, RERANK_MAX_LOCAL_CANDIDATES),
        )
        web_limit = min(max(top_k, RERANK_MIN_TOPK), RERANK_MAX_WEB_CANDIDATES)
        return local_limit, web_limit

    @staticmethod
    def _rerank_input_text(doc: Dict[str, Any]) -> str:
        """送精排的单条文本：缺失/None 用空串占位，超长切片截断。

        web 结果没有 snippet 时上一步拿到的就是 None，把它塞进批量请求可能让整次
        外部调用失败——一条脏数据毁掉全部候选的重排，代价远大于这一条少几个字。
        只缩短送给模型的副本，doc["content"] 本体不变（生成阶段还要用全文）。
        """
        content = doc.get("content")
        text = "" if content is None else str(content)
        return text[:RERANK_MAX_CONTENT_CHARS]

    def _step_2_rerank_merged_docs(self, state: QueryGraphState, merged_multi_docs: List[Dict[str, Any]]) -> List[
        Dict[str, Any]]:
        """使用 Reranker 模型对文档进行精排"""

        try:
            user_query = state.get('rewritten_query') or state.get('original_query') or ''
            # 获取文档列表的content字段组成列表（占位 + 截断，不污染 doc 本体）
            contents = [self._rerank_input_text(doc) for doc in merged_multi_docs]
            # 调用Rerank模型：交叉编码器（精排阶段）
            # Query 和 Document 联合编码，精度更高
            rerank_scores = rerank_documents(user_query, contents)

            scored_docs = [{**doc, "score": score} for doc, score in zip(merged_multi_docs, rerank_scores)]
            # 等同如下写法
            # scored_docs = []
            # for doc, score in zip(merged_multi_docs, rerank_scores):
            #     scored_docs.append({
            #         "content": doc.get("content"),
            #         "title": doc.get("title"),
            #         "chunk_id": doc.get("chunk_id"),
            #         "url": doc.get("url"),
            #         "source": doc.get("source"),
            #         "score": float(score),
            #     })

            sorted_score_docs = sorted(
                scored_docs,
                key=lambda x: x["score"],
                reverse=True
            )
            state["rerank_failed"] = False

            return sorted_score_docs

        except Exception as e:
            # 降级口径：按召回分重排（本地用 RRF、web 给中位分），并标记 rerank_failed 让
            # 后续来源面板改用宽阈值，不让一次精排故障变成“知识库无相关内容”。
            logger.warning(f"Rerank 重排序失败，回退召回序：{str(e)}")
            state["rerank_failed"] = True
            return self._fallback_ranked_docs(merged_multi_docs)

    @staticmethod
    def _fallback_ranked_docs(merged_multi_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """精排失败时的降级排序，返回已按分数降序的列表。

        为什么不用原来的 vector_score：它是 Milvus 返回的 distance，COSINE 集合与 IP 集合
        数值口径不同（一个落在 0~1、一个无上界），混在一起比大小是自作主张；而 RRF 分数
        只依赖名次，天然同量纲。除以组内最大值归一是为了保持 score 字段“0~1 相对置信”
        的约定，不让下游拿到 0.03 这种看着像“全部不相关”的原始融合值。
        旧实现还有个隐性毛病：它返回的是召回序却没排序，而断崖检测假定输入降序排列，
        分数非单调时“差距”会变成负数，截断位置就成了不可预期的结果。
        """
        local_scores = [
            float(doc["rrf_score"])
            for doc in merged_multi_docs
            if doc.get("source") == "local" and doc.get("rrf_score") is not None
        ]
        max_rrf_score = max(local_scores) if local_scores else 0.0

        ranked: List[Dict[str, Any]] = []
        for doc in merged_multi_docs:
            if doc.get("source") != "local":
                ranked.append({**doc, "score": RERANK_FALLBACK_WEB_SCORE})
                continue
            rrf_score = doc.get("rrf_score")
            if rrf_score is None:
                # 拿不到 RRF 分（例如非 RRF 节点产出的切片）就退回向量分，至少不比改前差
                score = doc.get("vector_score")
            elif max_rrf_score > 0:
                score = float(rrf_score) / max_rrf_score
            else:
                score = 0.0
            ranked.append({**doc, "score": score})

        def sort_key(doc: Dict[str, Any]) -> float:
            value = doc.get("score")
            # 无分数的文档排到末尾，不能让 None 参与比较（会直接抛 TypeError）
            return float(value) if isinstance(value, (int, float)) else float("-inf")

        # sort 稳定：分数相同的文档保持各自召回顺序，web 因此仍是搜索引擎给的那个序
        ranked.sort(key=sort_key, reverse=True)
        return ranked

    def _step_3_cliff_cutoff(self, ranked_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """断崖检测截断：相邻得分差距超过阈值时截断。"""
        if not ranked_docs:
            return []

        upper_bound = min(RERANK_MAX_TOPK, len(ranked_docs))
        lower_bound = min(RERANK_MIN_TOPK, upper_bound)

        # 默认值：取满硬上限（最多10条）
        cutoff_pos = upper_bound

        # 遍历范围：从min_topk-1到max_topk-2（索引从0开始），检测相邻两个文档的分数差
        # 例：min_topk=3，max_topk=10 → 遍历i=2,3,4,5,6,7,8（对应第3~9条文档，检测与下一条的差距）
        for idx in range(lower_bound - 1, upper_bound - 1):
            current_score = ranked_docs[idx].get("score")
            next_score = ranked_docs[idx + 1].get("score")

            if current_score is None or next_score is None:
                continue

            # 计算相邻文档的分数绝对差距（因已降序，gap≥0）
            abs_gap = current_score - next_score
            # 计算相对差距：绝对差距 / 当前文档分数（+1e-6避免除数为0/极小值，防止程序报错）
            # 1e-6 是 Python 中科学计数法的写法，等价于 0.000001（10 的负 6 次方，也就是百万分之一）。
            rel_gap = abs_gap / (abs(current_score) + 1e-6)

            # 触发断崖截断条件：绝对差距≥绝对阈值 OR 相对差距≥相对阈值
            # 满足任一条件，说明下一条文档相关性骤降，截断在当前位置
            if abs_gap >= RERANK_GAP_ABS or rel_gap >= RERANK_GAP_RATIO:
                # 最终取前i+1条（索引转实际数量，如i=2 → 取前3条）
                cutoff_pos = idx + 1
                logger.debug(f"断崖检测: 位置 {idx + 1}, abs_gap={abs_gap:.4f}, rel_gap={rel_gap:.4f}")
                break

        return ranked_docs[:cutoff_pos]
if __name__ == "__main__":

    # 日志只由进程入口配置，手动跑本脚本时显式挂一次控制台 handler。
    configure_logging()
    mock_state = {
        "rewritten_query": "怎么测这块主板的短路问题？",
        "rrf_chunks": [
            {
                "chunk_id": "local_1",
                "title": "主板维修手册",
                "content": "主板短路通常表现为通电后风扇转一下就停，可以使用万用表的蜂鸣档测量。"
            },
            {
                "chunk_id": "local_2",
                "title": "闲聊",
                "content": "今天中午去吃猪脚饭吧，这块主板外观很漂亮。"
            },
        ],
        "web_search_docs": [
            {
                "url": "https://example.com/repair",
                "title": "短路查修指南",
                "snippet": "主板通电前先打各主供电电感对地阻值，阻值偏低就是短路。"
            },
            {
                "url": "https://example.com/news",
                "title": "科技新闻",
                "snippet": "苹果发布新款手机，A系列芯片性能提升20%。"
            },
        ],
    }

    node_rerank = NodeRerank()
    result = node_rerank(mock_state)
    logger.info(serialize_json(result, indent=4))
