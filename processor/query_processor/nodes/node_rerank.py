"""
检索结果重排序节点。

processor/query_processor/nodes/node_rerank.py

对多路召回结果进行精排和断崖截断，输出最终知识片段集合。
"""
import os
import re
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

# -----------------------------
# 方案 A：rerank 出口的两步排序修正（图片 markdown 折叠 + 同标题碎片合并）
# -----------------------------
# 华为库三题 trace 暴露：无关近邻块的高分其实是“图片喂出来的假相关”——VL 图注和 MinIO
# URL 里混进了“相机/打开/峰值容量”等 query 词，把真正的短答案块挤出 top-3；同一超长章节
# 又被 _split_long_section 切成“标题-1/-2/-3”各自占坑。两处修法都是结构性、与具体文档无关
# 的：折叠图片噪声 + 合并过切碎片。默认开启便于评测直接生效；设 RERANK_A_MERGE_DISABLE /
# RERANK_A_IMAGE_COLLAPSE_DISABLE =1 可分别回到旧行为做基线对比。
# （曾用“超长块按字符数降权”作为第二步，实测图片折叠后它完全 dormant 且不通用于其它文档，已移除。）
# 同标题过切块合并：把带尾部片段序号 “-<数字>” 的碎片按基标题并回一条，去重占坑。
_RERANK_A_FRAGMENT_SUFFIX_RE = re.compile(r'-\d+$')

# 图片 markdown 折叠：![..](url) → [图]。MinIO 图片链接把整篇文档标题嵌进 URL、
# 还带一段 VL 生成的长摘要，单张图虚占两百多字，把“块有多长”撑失真、又给交叉编码器喂
# 非正文 token。URL 里可能含成对括号（如 …-(DBY-W09,HarmonyOS2_01,ZH-CN)/xxx.jpg），
# 故内层用“非括号字符 或 一对平衡括号”交替来匹配。仅在重排阶段用，不改存储、不影响显示。
_RERANK_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\(\s*(?:[^()\s]|\([^()\s]*\))+\s*\)")
_RERANK_IMAGE_PLACEHOLDER = "[图]"

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

        # 2.5 方案A 排序修正：同标题过切块合并（图片折叠已在送精排时生效，见 _rerank_input_text）。
        # 仅正常精排路径生效；降级路径的分数是召回序投影，不做这些修正。
        if not state.get("rerank_failed"):
            reranked_docs = self._step_2c_merge_same_section_fragments(reranked_docs)

        # 3. 动态 Top_K 截取(断崖检测)。窗口宽度以用户 top_k 封顶（Phase3 决策1a）：
        # 出口从“恒 RERANK_MAX_TOPK”收窄到 clamp(top_k, MIN, MAX)，让“读几条=展示几条=top_k”；
        # 送精排的候选余量仍由 _candidate_limits（floor 到 RERANK_MAX_TOPK）保留，断崖才看得到下降段。
        window_cap = self._final_window_cap(state)
        if state.get("rerank_failed"):
            # 降级分数只是召回序的投影，而断崖阈值是给交叉编码器的分数刻度定的，
            # 套在它上面等于用一把没校准的尺子做截断；直接取头部，窗口宽度与正常路径一致。
            cutoff_docs = reranked_docs[:window_cap]
        else:
            cutoff_docs = self._step_3_cliff_cutoff(reranked_docs, window_cap)

        # 4. 更新state
        state['reranked_docs'] = cutoff_docs

        # 5. 返回state
        return state

    def _step_1_merge_multi_source_docs(self, state: QueryGraphState) -> List[Dict[str, Any]]:
        """合并本地 RRF 结果和网络搜索结果为统一格式（并按精排预算截断候选数）。"""

        final_docs = []
        local_limit, web_limit = self._candidate_limits(state)

        # 1. 获取本地 RRF 的文档（已按 RRF 得分降序，取头部就是最相关的一批）
        # 先剔除“正文即标题”的纯标题空块再截断：这类块零信息量，却因与查询
        # 词面/实体高度重合而被交叉编码器打高分，不剔除会霸榜、抬高相对淘汰线误杀真块、
        # 甚至在自己之后就触发断崖把答案切掉。先过再截，才能用更深的候选补回真实块。
        local_docs = [
            rrf_doc
            for rrf_doc in (state.get('rrf_chunks') or [])
            if not self._is_heading_only_doc(rrf_doc)
        ]
        for rrf_doc in local_docs[:local_limit]:
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
    def _is_heading_only_doc(doc: Dict[str, Any]) -> bool:
        """判定“正文即标题”的纯标题空块。

        按标题切分时，无直属正文的标题会生成 content 仅等于标题文本的块（见 node_document_split
        的 flush_section：current_parts 初始化为 [title]）。这类块不承载任何可回答信息。
        故意不用“长度 < 阈值”作判据：会误杀 60 字内但确有内容的块，也会扰动既有护栏；
        content==title 是最小、最精准的信号，且只在本地块上生效。用相等而非子串，
        避开将“标题+正文”的正常块错判。无标题的块（如旧数据/手工构造）不会命中。
        """
        content = str(doc.get("content") or "").strip()
        title = str(doc.get("title") or "").strip()
        return bool(content) and content == title

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
    def _final_window_cap(state: QueryGraphState) -> int:
        """最终喂模型/展示的来源条数上限：以用户 top_k 封顶、夹在 [RERANK_MIN_TOPK, RERANK_MAX_TOPK]。

        Phase3 决策1a：出口宽度不再与 top_k 脱钩（以前恒 = RERANK_MAX_TOPK）。top_k 缺省按 5
        （与接口/前端默认一致）；再小也保底 RERANK_MIN_TOPK，避免收窄到断崖无段可看。
        """
        try:
            top_k = int(state.get('top_k') or 5)
        except (TypeError, ValueError):
            top_k = 5
        return max(RERANK_MIN_TOPK, min(top_k, RERANK_MAX_TOPK))

    @classmethod
    def _rerank_input_text(cls, doc: Dict[str, Any]) -> str:
        """送精排的单条文本：先折叠图片 markdown（去 URL/摘要噪声），缺失/None 用空串占位，再按上限截断。

        web 结果没有 snippet 时上一步拿到的就是 None，把它塞进批量请求可能让整次
        外部调用失败——一条脏数据毁掉全部候选的重排，代价远大于这一条少几个字。
        只缩短送给模型的副本，doc["content"] 本体不变（生成阶段还要用全文）。
        """
        content = doc.get("content")
        text = "" if content is None else str(content)
        text = cls._collapse_image_markdown(text)
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

    # ---------------- 方案 A：图片折叠 + 同标题碎片合并 ----------------
    @staticmethod
    def _env_disabled(name: str) -> bool:
        """环境变量开关：设成 1/true/yes 视为关闭该步（默认开启，便于评测直接生效）。"""
        return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}

    @classmethod
    def _collapse_image_markdown(cls, text: str) -> str:
        """把 Markdown 图片 ![..](url) 折叠成 [图]，仅在重排阶段用（不改存储/不影响显示）。

        折叠只发生在送精排的副本与长度测量上；图片显示仍从原始 content 现取
        （node_answer_output 靠 ![](url) 提图）、embedding 也用导入期完整正文，均不受影响。
        设 RERANK_A_IMAGE_COLLAPSE_DISABLE=1 可关回旧行为。
        """
        if not text or cls._env_disabled("RERANK_A_IMAGE_COLLAPSE_DISABLE"):
            return text
        return _RERANK_IMAGE_MD_RE.sub(_RERANK_IMAGE_PLACEHOLDER, text)

    @classmethod
    def _is_fragment(cls, doc: Dict[str, Any]) -> bool:
        """是否 _split_long_section 产出的过切碎片：标题以 “-<数字>” 结尾。"""
        title = str(doc.get("title") or "").strip()
        return bool(title) and bool(_RERANK_A_FRAGMENT_SUFFIX_RE.search(title))

    @classmethod
    def _base_title(cls, doc: Dict[str, Any]) -> str:
        """还原片段所属的章节基标题（去掉尾部 “-<数字>” 片段序号）。"""
        return _RERANK_A_FRAGMENT_SUFFIX_RE.sub("", str(doc.get("title") or "").strip())

    def _step_2c_merge_same_section_fragments(self, ranked_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把同基标题的过切碎片并回一条，避免 “标题-1/-2/-3” 各自占坑、挤掉短答案块。

        合并代表分取全组最高分（保留该章节最强命中的分数），正文按片段序号
        顺序拼接并去掉重复标题前缀；只合并“确实带 -N 后缀且同基标题 ≥2 块”的碎片，孤立碎片
        或无后缀的同名不同章节一律不并，避免误伤。
        """
        if self._env_disabled("RERANK_A_MERGE_DISABLE") or not ranked_docs:
            return ranked_docs
        groups: Dict[str, List[Dict[str, Any]]] = {}
        standalone: List[Dict[str, Any]] = []
        for doc in ranked_docs:
            if self._is_fragment(doc):
                groups.setdefault(self._base_title(doc), []).append(doc)
            else:
                standalone.append(doc)
        mergeable = {base: members for base, members in groups.items() if len(members) >= 2}
        if not mergeable:
            return ranked_docs
        merged_docs = list(standalone)
        for base, members in mergeable.items():
            members_sorted = sorted(members, key=self._fragment_part_no)
            best = max(members, key=lambda d: d.get("score") if isinstance(d.get("score"), (int, float)) else float("-inf"))
            merged_docs.append({
                **best,
                "title": base,
                "content": self._merge_fragment_bodies(base, members_sorted),
                "merged_parts": len(members),
            })
        merged_docs.sort(key=self._score_or_min, reverse=True)
        return merged_docs

    @classmethod
    def _fragment_part_no(cls, doc: Dict[str, Any]) -> int:
        """从标题尾部 “-<数字>” 取片段序号，用于按阅读顺序拼接碎片（无后缀按 0）。"""
        match = _RERANK_A_FRAGMENT_SUFFIX_RE.search(str(doc.get("title") or ""))
        return int(match.group(0)[1:]) if match else 0

    @staticmethod
    def _merge_fragment_bodies(base: str, members_sorted: List[Dict[str, Any]]) -> str:
        """按片段顺序拼接正文：剥掉每个分片开头重复的基标题，最后统一补一份。"""
        chunks: List[str] = []
        for doc in members_sorted:
            content = str(doc.get("content") or "").strip()
            if base and content.startswith(base):
                content = content[len(base):].strip()
            if content:
                chunks.append(content)
        prefix = f"{base}\n\n" if base else ""
        return (prefix + "\n\n".join(chunks)).strip()

    @staticmethod
    def _score_or_min(doc: Dict[str, Any]) -> float:
        """排序键：无分数（None）沉底，不让 None 参与比较报 TypeError。"""
        value = doc.get("score")
        return float(value) if isinstance(value, (int, float)) else float("-inf")

    def _step_3_cliff_cutoff(self, ranked_docs: List[Dict[str, Any]], window_cap: int) -> List[Dict[str, Any]]:
        """断崖检测截断：在不超过 window_cap 的前提下，相邻得分差距超过阈值时提前截断。

        window_cap 由 _final_window_cap 给出（= 用户 top_k 夹在 [RERANK_MIN_TOPK, RERANK_MAX_TOPK]）；
        断崖只负责在这个封顶之内再砍明显掉队的尾巴，不再决定“最多给几条”（那条已交给 top_k）。
        """
        if not ranked_docs:
            return []

        upper_bound = min(window_cap, RERANK_MAX_TOPK, len(ranked_docs))
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
