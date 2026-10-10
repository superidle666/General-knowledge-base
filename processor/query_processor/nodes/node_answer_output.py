"""
答案生成与输出节点。

processor/query_processor/nodes/node_answer_output.py

@Description: 组装问答提示词、调用模型生成答案、筛选回答引用图片并发送最终结果。
@BusinessRule: 图片只能返回模型明确引用且来自当前召回内容的 URL；
              白名单外的 markdown 图片（模型臆造的 URL）要从答案正文中剜除，
              否则前端按 markdown 渲染整段答案时会直接展示出裂图。
              用户端展示的引用来源必须与生成时真正喂进 prompt 的切片同源：若另走一套
              “按相对淘汰线二次裁剪”的来源整形，高分噪声块抬高淘汰线时会漏掉真正
              支撑答案的切片，故本节点直接以“进入 prompt 的切片集合”产出 state['sources']。
"""

import re
import time
from typing import List, Dict, Tuple

from processor.query_processor.chitchat_gate import normalize_for_match
from processor.query_processor.prompt.answer_prompt import ANSWER_PROMPT
from tool.logger import configure_logging, logger

from processor.query_processor.base import NodeBase
from processor.query_processor.state import QueryGraphState
from utils.llm_utils import get_llm_client
from utils.mongo_history_utils import save_chat_message
from utils.sse_utils import push_to_session, SSEEvent
from utils.task_utils import set_task_result, add_done_task

MAX_CONTEXT_CHARS = 12000
MAX_HISTORY_CHARS = 3000
# 说明句里最多点几个实体名：一句“本库未收录 A、B、C 的资料”同等于没说，
# 而实体多到三个以上时本来就不是“单一型号窄窄收窄”的那种场景。
MAX_DISCLOSED_ENTITIES = 2
# 静态短答（闲聊话术/校验提示/拒答反问）不经检索与生成，瞬时送达会像"机器人占位"；
# 推送前稍作停顿，让对话节奏接近真实回答。只加在流式 delta 推送前，非流式接口不拖慢。
STATIC_REPLY_DELAY_SECONDS = 1.5

# 无证据不进生成（R5）的两套固定话术：刻意区分"跑了一遍但没查到"与"根本没跑成"。
# 把服务故障说成"库里没有"是在向用户输出一条错误事实（§6-7 "故障 ≠ 零命中"的用户侧）。
INSUFFICIENT_EVIDENCE_REPLY = (
    "当前知识库没有找到与这个问题相关的内容，可以换个说法、补充具体型号，或改用其他知识库再问一次。"
)
RETRIEVAL_INCOMPLETE_REPLY = "本次检索没有完成，暂时无法依据知识库回答，请稍后重试或换个问法。"

# 生成调用本身挂掉时的兜底话术（与“库里没证据”是两回事，提出来才能被别处准确识别）。
GENERATION_FAILED_REPLY = "抱歉，生成回答时出现错误。"

# M7：实体过滤被放宽过（用户点名的型号在本库带过滤召不回任何切片，于是拆掉过滤重查了一次）
# 时，窗口里坐的全是“别的型号”的切片。让模型照着它们答，等于拿隔壁产品的参数冒充答案
# （线上实测：话题是 iPhone，答成“该设备 UFS4.1 / 12+256GB”）。这既不是“库里没有”（那是 R5
# 的语义槽，而本句前面刚刚检索到了内容），也不是“检索故障”，所以另起一条**补充说明**前置到
# 生成的答案上：把“已经拆过过滤”这个事实告知用户，而不是默默交付一份看着完整的答非所问。
RELAXED_FILTER_NOTICE_TEMPLATE = (
    "（说明：本库暂未收录「{entity}」的专属资料，以下内容取自最接近的相关文档，"
    "可能与该型号不完全对应。）"
)

# 图片出口的语义否决词表：召回切片里有一类图压根不是“产品外观/结构示意”，而是封面
# 条形码、铭牌、认证标志这类标识图。它们的 alt 是导入期多模态模型写的，本身就写着“条形码”；
# 而白名单只校验 URL 出处不看内容，所以模型一旦把它当外观图引用，用户看到的就是“一台机器
# 配了一张条码”（线上实测：HAK 180 烫金机的回答底部渲染了手册封面条码）。提示词里的
# 4.2/4.3/4.4 三条约束都明确禁过这种用法，模型仍概率性违反 → 只能在出口做确定性否决。
NON_APPEARANCE_ALT_TERMS: Tuple[str, ...] = (
    "条形码", "条码", "二维码", "铭牌", "标签", "封签", "防伪", "合格证",
    "包装", "封面", "水印", "认证标志", "logo",
)
# 与 DRIFT_GUARD_DISABLE 同族：设成 1/true/yes 时退回“只看 URL 出处”的旧白名单行为。
IMAGE_ALT_VETO_DISABLE_ENV = "IMAGE_ALT_VETO_DISABLE"


def _non_appearance_term(alt: str, question: str) -> str:
    """alt 命中标识图词表时返回命中的词，否则返回空串（空串 = 放过）。

    两个放过方向是刻意的：alt 缺失（旧集合、无摘要图）时拿不到语义依据，不能因此屏蔽真图；
    本轮问题自己就在问这个东西（“条码在哪看”“铭牌上写了什么”）时，这张图正是答案。
    """
    normalized_alt = normalize_for_match(alt)
    if not normalized_alt:
        return ""
    normalized_question = normalize_for_match(question)
    for term in NON_APPEARANCE_ALT_TERMS:
        normalized_term = normalize_for_match(term)
        if not normalized_term or normalized_term not in normalized_alt:
            continue
        if normalized_term in normalized_question:
            return ""
        return term
    return ""


# 检索链路的失败标记：任一为真就说明至少有一路没跑成，不能当成"查过了但没有"。
# 与 node_search_embedding / node_web_search_mcp / node_rerank 写入 state 的键一一对应。
# `metadata_query_failed` 不是检索链路的故障而是元数据画像读挂：元数据短路自带答案、
# 根本进不到本节点，登记它是为了另一个调用点（接口层日志与静态守卫），而且必须登记：
# 三个语义槽（库里没证据 / 检索没跑成 / 元数据查挂了）一旦共用一个判据，用户就会拿
# 系统故障当事实否定。键名以 _failed 结尾的集合被 tests/test_answer_evidence_gate.py 钉住全等。
RETRIEVAL_FAILURE_MARKERS = (
    "embedding_search_failed",
    "hyde_failed",
    "web_search_failed",
    "rerank_failed",
    "metadata_query_failed",
)

class NodeAnswerOutput(NodeBase):
    """
    节点功能: 答案输出
    流程: 检查已有答案 → 构建提示词 → LLM 生成 → 写入历史 → 发送结束事件
    """

    # 覆盖基类的 name 属性，标识节点名称
    name: str = "node_answer_output"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        """
        1 判断state 中的answer是否已经存在，如果存在直接输出answer中的答案，注意判断是否需要流式输出需要则流式输出
        2 根据state中的问题、重新问题、历史对话、提问商品（item_names）、 重排内容 组织prompt 并调用llm 生成答案
        3 调用大模型输出答案 注意判断是否需要流式输出需要则流式输出
        4 把答案写入到mongodb的history中 利用utils/mongo_history_utils.py中的save_chat_message方法
        5 做最后一次push操作（主要是为了触发前端图片渲染)
             {
                "answer": "HAK 180 烫金机的操作面板位于...（大模型生成的纯文本）...",
                "status": "completed",
                "image_urls": [
                    "http://local-server/images/panel_view.jpg",
                    "http://local-server/images/button_detail.jpg"
                ]
              }
        """
        # 阶段零：没有任何可用证据时预置固定话术、不进生成（R5）。
        # 刻意放在阶段一之前：预置好 answer 就能复用"已有答案即原样输出"的既有出口
        # （流式推 delta + 静置、非流式写任务结果），不另写一套推送与落库逻辑。
        self._step_0_guard_without_evidence(state)

        # 阶段一：检查answer是否存在,如果存在直接输出answer中的答案
        answer_exists = self._step_1_check_answer(state)

        # # 阶段二  如果没有answer则 构建 Prompt
        if not answer_exists:
            prompt = self._step_2_construct_prompt(state)
            state["prompt"] = prompt

            # 阶段三：  如果没有answer则 调用大模型输出答案
            self._step_3_generate_response(state, prompt)

            # 阶段三·五：把“实体过滤已放宽”这件事补进答案（M7）。必须排在生成之后、
            # 图片提取与写历史之前：说明要跟着正文一起落库与推 FINAL（流式下 delta 已推完，
            # 但接口层 FINAL 携的是全量 answer、前端按它覆盖式渲染，所以补得上去）。
            # 放在这个分支里而不是函数入口：“无证据”的固定话术走的是 answer 已存在的分支，
            # 它已经说清楚了，叠一条“已拆过滤”只会自相矛盾。
            self._step_3b_disclose_relaxed_filter(state)


        # 阶段四： 提取图片URL（用于历史记录和前端展示）
        # 判定“本轮在问什么”只吃本轮原文（与 R2 给拒答判定立的口径一致）：历史里上一轮问过
        # “条码在哪看”，不应让本轮的外观图被豁免。
        image_urls = self._extract_images_from_answer(
            state.get("answer") or "",
            state.get("reranked_docs") or [],
            str(state.get("query_entity_text") or state.get("original_query") or ""),
        )
        state["image_urls"] = image_urls
        # 图片白名单之外的 markdown 引用是模型臆造的（参考内容里根本没有这张图），
        # 前端按 markdown 渲染整段答案会直接出裂图；从正文剜除，写历史/非流式返回/FINAL
        # 事件都走这一份清洗后的文本。
        state["answer"] = self._sanitize_unverified_images(state.get("answer") or "", set(image_urls))

        # 阶段五：把答案写入到mongodb的history中
        if state.get("answer") and state.get("persist_history", True):
            logger.info("---写入MongoDB历史记录---")
            self._step_4_write_history(state, image_urls=image_urls)

        add_done_task(state['session_id'], self.name, state.get("is_stream"))

        # 阶段六: 流式输出结束，发送 final 事件 [最后兜底，确保图片都能争取渲染和结束]
        logger.info(f"---发送 final 事件---图片为：{image_urls}")
        if state.get("is_stream"):
            push_to_session(
                state['session_id'],
                SSEEvent.FINAL,
                {
                    "answer": state["answer"],
                    "status": "completed",
                    "image_urls": image_urls  # 发送图片URL给前端
                }
            )

        logger.info("---node_answer_output 节点处理结束---")
        return state

    def _step_0_guard_without_evidence(self, state: QueryGraphState) -> None:
        """
        阶段零：检索没有产出任何可用切片时，预置固定话术并阻断生成（R5）。

        为什么必须挡在生成之前：context 为空时 prompt 里填的是"无参考内容"，模型仍会
        就着问题本身编出"建议查阅《xxx 手册》"这样的引文；而接口层的兜底条件是
        `if not sources and not answer` —— 只要模型吐出文字就不触发，于是 sources=0 也能
        出一份"看着有凭据"的答案。对零召回的模型输出做检查属于不可控优化，在入口断掉才是确定性的。

        "有没有证据"看的是正文而不是列表长度：空切片对模型同样没有信息量。
        话术二选一只看失败标记：任一为真就不能宣称"库里没有"（§6-7）。
        """
        if state.get("answer"):
            return
        docs = state.get("reranked_docs") or []
        if any(isinstance(doc, dict) and str(doc.get("content") or "").strip() for doc in docs):
            return
        retrieval_failed = any(bool(state.get(marker)) for marker in RETRIEVAL_FAILURE_MARKERS)
        state["answer"] = RETRIEVAL_INCOMPLETE_REPLY if retrieval_failed else INSUFFICIENT_EVIDENCE_REPLY
        # J2：这条短路不进 `_step_2_construct_prompt`，sources 必须显式置空而不是缺键
        # （缺键与"算过但没有来源"是两回事，前端要靠它区分是否显示"基于 N 个来源"）
        state["sources"] = []
        logger.warning(
            "【答案节点】检索无可用证据，走固定话术不进生成（是否检索故障=%s）问题=%s",
            retrieval_failed,
            state.get("original_query", ""),
        )

    def _step_3b_disclose_relaxed_filter(self, state: QueryGraphState) -> None:
        """本轮拆过实体过滤时，在答案头上补一句“这份资料不是那个型号的”（M7）。

        只动描述、不动结论：不拒答、不改 sources、也不抢 `INSUFFICIENT_EVIDENCE_REPLY` /
        `RETRIEVAL_INCOMPLETE_REPLY` 的语义槽——那两条说都是“结果”（库里没有 / 检索没跑成），
        本条说的是“依据的来源”，三者不能混用。

        不触发的三种情形都是“说了也没用或会说错”：话题实体不明确（`item_names` 为空，
        本来就是不带过滤检索，谈不上放宽）、生成自己挂了（输出的是兜底错误句，没结论可修正）、
        说明已在正文里（同一请求重入时不叠第二遍）。
        """
        if not state.get("entity_filter_relaxed"):
            return
        names = [str(name).strip() for name in (state.get("item_names") or []) if str(name).strip()]
        answer = str(state.get("answer") or "").strip()
        if not names or not answer or answer == GENERATION_FAILED_REPLY:
            return
        notice = RELAXED_FILTER_NOTICE_TEMPLATE.format(entity="、".join(names[:MAX_DISCLOSED_ENTITIES]))
        if notice in answer:
            return
        state["answer"] = f"{notice}\n\n{answer}"
        logger.info("【答案节点】实体过滤已放宽，已在答案前补充说明：%s", notice[:60])

    def _step_1_check_answer(self, state) -> bool:
          """
          阶段一：检查 state 中是否已有 answer。
          - 若已存在：按需推送流式 delta（用于 SSE），并返回 True
          - 若不存在：返回 False
          """
          answer = state.get("answer")
          is_stream = state.get("is_stream" )
          if answer:
            if is_stream:
              logger.info("---Step 1: 发现已有答案，执行流式推送---")
              # 预置短答（闲聊话术等）零检索零生成，瞬时送达体验像占位符；停顿一下再推。
              time.sleep(STATIC_REPLY_DELAY_SECONDS)
              push_to_session(state["session_id"], SSEEvent.DELTA, {"delta": answer})
            else:
              set_task_result(state["session_id"], "answer", answer)
            return True
          else:
            return False

    def _step_2_construct_prompt(self, state: QueryGraphState) -> str:

        """
        阶段二：构建 Prompt
        根据state中的问题、重新问题、历史对话、提问商品（item_names）、 重排内容 组装 LLM 提示词
        """
        char_budget = MAX_CONTEXT_CHARS

        # 1. 获取问题和商品名
        # 优先使用重写后的问题
        question = state.get("rewritten_query") or state.get("original_query", "")
        # 改写型元指令（R9-B）：检索词已换成上一轮的真问题，本轮的“呈现要求”（换个说法/
        # 用表格/精简点）不能丢。它刻意不进检索词（指令字面串污染召回正是 R9 要治的），
        # 只在这里补给模型：这是对表述方式的要求，不是新的事实来源。
        instruction = str(state.get("meta_instruction") or "").strip()
        if instruction:
            question = (
                f"{question}\n（本轮的呈现要求：{instruction}。请据此重新组织表述，"
                "事实仍以【参考内容】为准）"
            )
        # 图内跑下来 node_prepare_query 总会先把 item_names 归一成列表；但绕开入口
        # 直接调本节点（脚本、单测）时它可能根本没这个键，硬下标会让“没有实体”
        # 这种正常情况抛 KeyError。
        item_names = state.get("item_names") or []

        # 2. 为历史对话预留独立预算，避免长文档挤掉多轮上下文
        history_str, _ = self._format_chat_history(
            state.get("history") or [], MAX_HISTORY_CHARS
        )

        # 3. 格式化上下文文档，并记录"真正进入 prompt 的那批切片"
        context_str, used_docs, _ = self._select_context_docs(
            state.get("reranked_docs") or [], MAX_CONTEXT_CHARS - MAX_HISTORY_CHARS
        )

        # 3.1 引用与生成同源：用户端 sources 直接等于模型实际读到的切片集合。
        # 曾因“另一套按相对地板分+正文长度二次裁剪”的来源整形会在高分噪声块
        # 抬高淘汰线时，把真正支撑答案的切片挤出引用区（答案对、来源却查不到依据）；
        # 该整形节点 node_source_collection 已于 Phase3 决策3A 摘除，来源统一在此产出。
        state["sources"] = self._step_2b_generation_sources(state, used_docs)

        # 4. 格式化 Item Names (提问商品)
        item_names_str = ", ".join(item_names) if item_names else "无指定商品"

        # 5. 组装提示词
        prompt = ANSWER_PROMPT.format(
            context=context_str or "无参考内容",
            history=history_str if history_str else "暂无历史对话",
            item_names=item_names_str,
            question=question,
        )
        logger.info(f"组装后的提示词为：{prompt}")
        return prompt

    @staticmethod
    def _step_2b_generation_sources(state: QueryGraphState, used_docs: List[Dict]) -> List[Dict]:
        """把进入 prompt 的切片整理为用户端来源结构，并标注分数字段口径。

        score_kind：
          - "rerank"：交叉编码器给出的相关性分，可作为"相对可信"的参考分；
          - "recall"：精排失败降级后的召回序投影分，只能用于排序、不代表置信度，
            前端据此避免把它渲染成误导性的百分比。
        """
        score_kind = "recall" if state.get("rerank_failed") else "rerank"
        sources: List[Dict] = []
        for doc in used_docs:
            item = dict(doc)
            # rerank_probe 是 node_rerank 为合并体拼的送精排副本（已按分片均摊预算、折叠过
            # 图片），既不是正文也不是元数据：留着它会随 sources 进日志并写进 Mongo 历史。
            item.pop("rerank_probe", None)
            raw_score = item.get("score")
            try:
                item["score"] = float(raw_score) if raw_score is not None else None
            except (TypeError, ValueError):
                item["score"] = None
            item.setdefault("source", "local")
            item["score_kind"] = score_kind
            # 合并来源才带溯源字段：合并体只以代表分片的 chunk_id 入 sources，模型实际读到的
            # 其余几片必须靠 part_chunk_ids 才能回源（否则它们“进了 prompt 却无法溯源”）。
            # 非合并来源的条目结构逐字不变，新字段仅在确实发生合并时出现。
            part_chunk_ids = [part_id for part_id in (doc.get("part_chunk_ids") or []) if part_id is not None]
            if len(part_chunk_ids) > 1:
                item["part_chunk_ids"] = part_chunk_ids
                item["merged_parts"] = int(doc.get("merged_parts") or len(part_chunk_ids))
            else:
                # 只剩一片就构不成“合并”，把切片上可能残留的标注一并摘掉：来源条目要么两个
                # 字段都带、要么都不带，前端才不会拿到 merged_parts=1 这种自相矛盾的标注。
                item.pop("part_chunk_ids", None)
                item.pop("merged_parts", None)
            sources.append(item)
        return sources

    def _format_reranked_docs(self, reranked_docs: List[Dict], char_budget: int) -> Tuple[str, int]:
        """[兼容旧签名] 返回 (拼好的上下文, 剩余预算)。

        实际"纳入哪些切片"的逻辑在 _select_context_docs；这里只是它的薄封装，保留
        (str, int) 两值返回给既有调用与回归用例。需要拿到被选中的切片时请直接调用
        _select_context_docs。
        """
        formatted, _used_docs, remaining = self._select_context_docs(reranked_docs, char_budget)
        return formatted, remaining

    def _select_context_docs(
        self, reranked_docs: List[Dict], char_budget: int
    ) -> Tuple[str, List[Dict], int]:
        """按字符预算把重排切片拼进 prompt，返回 (上下文, 实际纳入的切片, 剩余预算)。

        "实际纳入的切片"用于让引用展示与生成所用同源：预算截断后没进 prompt 的切片，
        模型没读过，就不该出现在给用户的来源里；反之进了 prompt 的都必须可被引用。
        装不下时按条跳过、继续试后面的短块（而不是整段收尾）：一条超长块不该挡住短答案块。
        """
        formatted_lines = []
        used_docs: List[Dict] = []
        used_chars = 0
        skipped: List[Tuple[int, int]] = []

        # 从重排内容中，提取为资料字符串，不可超过限额
        # 优先使用结构化 reranked_docs（包含 source/chunk_id/url/score），便于约束与引用
        # ---------------------------------------------------------
        # 逻辑解释：
        # 1. 遍历重排序后的文档列表 (reranked_docs)，这些文档已经按相关性从高到低排序。
        # 2. 对每个文档提取关键信息 (text, source, chunk_id, url, title, score)。
        # 3. 构造 "元数据头 + 正文" 格式的字符串，例如：
        #    "[1] [local] [chunk_id=123] [score=0.95] [title=操作手册]
        #     这里是文档的正文内容..."
        # 4. 累加字符长度，如果超过 MAX_CONTEXT_CHARS (如 12000 字符)，则停止添加，
        #    确保 Prompt 长度在 LLM 的处理范围内，避免 Token 溢出。
        # ---------------------------------------------------------
        for idx, doc in enumerate(reranked_docs, start=1):
            content = str(doc.get("content") or "").strip()
            if not content:
                # 空正文对模型没有任何信息量，进 prompt 只是白占预算
                continue
            meta_tags = [f"[{idx}]"]
            for field, template in [
                ("source", "[source={}]"),
                ("chunk_id", "[chunk_id={}]"),
                ("url", "[url={}]"),
                ("title", "[title={}]"),
            ]:
                raw_value = doc.get(field)
                if raw_value is None:
                    # 跳过而不是 str(None)：否则本地来源会带上 [url=None]，模型会把它当成真实链接引用
                    continue
                field_value = str(raw_value).strip()
                if field_value:
                    meta_tags.append(template.format(field_value))

            relevance_score = doc.get("score")
            if relevance_score is not None:
                meta_tags.append(f"[score={float(relevance_score):.4f}]")

            doc_entry = " ".join(meta_tags) + "\n" + content

            if used_chars + len(doc_entry) > char_budget:
                # 预算不够装下这一条时只跳过它、继续试后面的短块（R10）：旧写法用 break，
                # 于是一条超长块（碎片合并恰好会造出这种）就能把后面本可装下的答案块
                # 整体挡住，而 sources 与本函数同源，被挡掉的块连引用都不展示。
                # 刻意不按剩余预算截断本条：截断可能把 ![](url) 从中间切断，前端按
                # markdown 提图会直接裂图。
                skipped.append((idx, len(doc_entry)))
                continue

            formatted_lines.append(doc_entry)
            used_docs.append(doc)
            used_chars += len(doc_entry) + 2

        if skipped:
            # 以前这类丢弃完全不可见：只能从“答案少了点东西”反推。留下序号与长度便于排查
            # “为什么高分块没进参考内容”。
            logger.debug(
                "---参考内容预算不足，跳过 %d 条（总预算 %d 字、已用 %d 字）：%s---",
                len(skipped), char_budget, used_chars, skipped,
            )

        return "\n\n".join(formatted_lines), used_docs, char_budget - used_chars

    def _format_chat_history(self, chat_history: List[Dict], char_budget: int) -> Tuple[str, int]:
        """格式化历史对话；预算截断时保留最近的几轮。

        入参列表是时间正序（旧→新，get_recent_messages 保证）。此前从最旧开始
        累加、超预算即停，留下的偏偏是最早的寒暄、追问最依赖的最近几轮被整体丢弃。
        改为从新往旧收集，预算耗尽即止，输出时再恢复时间正序拼接。
        """
        role_label_map = {"user": "用户", "assistant": "助手"}

        collected: List[str] = []
        used_chars = 0

        for message in reversed(chat_history):
            role = message.get("role", "")
            text = message.get("text", "")
            if not text or role not in role_label_map:
                continue

            formatted_line = f"{role_label_map[role]}: {text}"
            line_chars = len(formatted_line) + 1
            if used_chars + line_chars > char_budget:
                # 连最近一条都装不下时选择截断而非丢弃：丢掉最新一轮对追问的伤害
                # 大于预算轻微溢出，且下游文档预算本就先于历史被压缩。
                if not collected:
                    collected.append(formatted_line[:max(char_budget, 0)])
                    used_chars = char_budget
                break

            collected.append(formatted_line)
            used_chars += line_chars

        return "\n".join(reversed(collected)), char_budget - used_chars


    def _step_3_generate_response(self, state: QueryGraphState, prompt: str) -> QueryGraphState:
        """
        阶段三：生成回答
        调用llm生成答案，支持流式输出
        """
        logger.info("---Step 3: 开始生成回答 (LLM Generation)---")

        # 获取 LLM 客户端
        # 注意：这里我们使用统一的 get_llm_client 获取实例
        llm = get_llm_client()

        # 判断是否需要流式输出
        # 通常 state 中会注入 stream_queue 用于 SSE 推送
        session_id = state.get("session_id")
        is_stream = state.get("is_stream")

        if is_stream:
            logger.info(f"模式: 流式输出 (Streaming), Session: {session_id}")
            final_text = ""
            try:
                # 使用 stream 方法进行流式生成
                for chunk in llm.stream(prompt):
                    delta = getattr(chunk, "content", "") or ""
                    if delta:
                        final_text += delta
                        # 将增量内容放入队列
                        push_to_session(session_id, SSEEvent.DELTA, {"delta": delta})

                logger.info(f"流式输出完成，总长度: {len(final_text)}")

            except Exception as e:
                logger.error(f"流式生成出错: {e}", exc_info=True)
                # 发生错误时，尝试推送到前端
                push_to_session(session_id, SSEEvent.ERROR, {"error": str(e)})

            state["answer"] = final_text
        else:
            # 非流式直接调用
            logger.info(f"模式: 非流式输出 (Blocking), Session: {session_id}")
            try:
                response = llm.invoke(prompt)
                content = response.content
                state["answer"] = content
                set_task_result(session_id, "answer", content)
                logger.info(f"生成回答完成，长度: {len(content)}")
            except Exception as e:
                logger.error(f"生成回答出错: {e}", exc_info=True)
                state["answer"] = GENERATION_FAILED_REPLY

        return state


    def _extract_images_from_docs(self, docs):
        """
        辅助方法：从文档列表中提取图片URL

        核心逻辑：
        1. 遍历所有相关文档（包括本地知识库切片和联网搜索结果）。
        2. 策略一：直接检查文档的 'url' 字段（常见于联网搜索结果）。
           - 验证后缀名是否为图片格式 (.jpg, .png 等)。
        3. 策略二：使用正则表达式扫描文档 'text' 正文内容（常见于本地 Markdown 文档）。
           - 匹配 Markdown 图片语法: ![alt text](image_url)。
        4. 对提取到的 URL 进行去重处理，返回唯一图片列表。

        :param docs: 文档列表，每个文档为字典格式
        :return: 图片 URL 字符串列表
        """
        images = []
        seen = set()  # 用于去重，避免同一张图片重复出现
        if not docs:
            return []
        # ---------------------------------------------------------
        # 正则表达式解释：r'!\[.*?\]\((.*?)\)'
        # 1. !\[   -> 匹配 Markdown 图片语法的开头 "![" (注意 [ 需要转义)
        # 2. .*?   -> 非贪婪匹配图片描述文本 (Alt Text)，即 [] 中间的内容
        # 3. \]    -> 匹配描述文本的结束符 "]"
        # 4. \(    -> 匹配 URL 部分的开始符 "("
        # 5. (.*?) -> 捕获组 (Group 1)：非贪婪匹配括号内的实际 URL 内容
        # 6. \)    -> 匹配 URL 部分的结束符 ")"
        # ( ... ) （不带反斜杠）：这就是 捕获组 。
        # 它的作用是告诉程序：“虽然我匹配了整个 ![...](...) 结构，但我 只要 这括号里的内容”。
        # ---------------------------------------------------------
        md_img_pattern = re.compile(r'!\[.*?\]\((.*?)\)')

        logger.info(f"开始提取图片，待处理文档数: {len(docs)}")

        for i, doc in enumerate(docs):
            # 1. 优先检查 url 字段 (主要针对 Web Search 结果)
            url = (doc.get("url") or "").strip()
            if url:
                # 简单后缀判断：确保是静态图片资源
                if url.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg')):
                    if url not in seen:
                        logger.debug(f"文档[{i}] 发现图片 URL (字段): {url}")
                        seen.add(url)
                        images.append(url)

            # 2. 检查 text 字段中的 Markdown 图片 (主要针对 Local Chunk)
            text = (doc.get("content") or "").strip()
            if text:
                # findall 机制解释：
                # 正则表达式 r'!\[.*?\]\((.*?)\)' 中包含一个捕获组 (.*?)
                # 当存在捕获组时，findall 只返回括号内匹配到的内容（即 URL），而不是整个 ![...](...) 字符串
                # 示例：
                # 输入 text: "参考图片 ![面板图](http://img.com/1.jpg) 如下"
                # 返回 matches: ['http://img.com/1.jpg']
                matches = md_img_pattern.findall(text)
                for img_url in matches:
                    img_url = img_url.strip()
                    if img_url and img_url not in seen:
                        logger.debug(f"文档[{i}] 正文发现 Markdown 图片: {img_url}")
                        seen.add(img_url)
                        images.append(img_url)

        logger.info(f"图片提取完成，共找到 {len(images)} 张唯一图片: {images}")
        return images

    def _extract_images_from_answer(self, answer: str, docs: list[dict], question: str = "") -> list[str]:
        """只返回模型在答案中明确引用、且来自召回内容的图片。

        出处白名单之后还有一道语义否决（alt 写着条形码/铭牌的标识图不当外观图用），
        被否决的 URL 不进 `image_urls`、也就自动从答案正文被剥掉（见 `_sanitize_unverified_images`）。
        """
        available_images = set(self._extract_images_from_docs(docs))
        if not available_images:
            return []

        image_urls: list[str] = []
        markdown_pattern = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
        legacy_pattern = re.compile(r"【图片】\s*([\s\S]*?)$")
        candidates = markdown_pattern.findall(answer)
        legacy_block = legacy_pattern.search(answer)
        if legacy_block:
            candidates.extend(legacy_block.group(1).splitlines())

        for candidate in candidates:
            image_url = candidate.strip().rstrip(",.;，。；）")
            if image_url in available_images and image_url not in image_urls:
                image_urls.append(image_url)
        return self._veto_non_appearance_images(image_urls, docs, question)

    # 召回切片正文里的 markdown 图片（带 alt）：alt 是导入期多模态模型写的图摘要，取它作语义依据。
    _DOC_MD_IMAGE_WITH_ALT_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

    def _extract_image_alts_from_docs(self, docs: list[dict]) -> Dict[str, str]:
        """URL → alt 映射（只来自召回切片，与 `_extract_images_from_docs` 同一数据源）。

        拿 alt 而不是拿模型自己写的描述：提示词 4.4 要求模型原样沿用 alt，但真发生错配时
        模型完全可能把 alt 改写成“外观图”——只有切片里那一份是文档自己的事实。
        """
        alts: Dict[str, str] = {}
        for doc in docs or []:
            if not isinstance(doc, dict):
                continue
            content = str(doc.get("content") or "")
            for alt, url in self._DOC_MD_IMAGE_WITH_ALT_RE.findall(content):
                url = url.strip()
                if url and url not in alts:
                    alts[url] = alt.strip()
        return alts

    def _veto_non_appearance_images(self, image_urls: list[str], docs: list[dict], question: str) -> list[str]:
        """把“不是外观/结构图”的引用从白名单里拿掉；开关关掉或拿不到 alt 时一律放过。"""
        if not image_urls or self._env_disabled(IMAGE_ALT_VETO_DISABLE_ENV):
            return image_urls
        alts = self._extract_image_alts_from_docs(docs)
        if not alts:
            return image_urls
        kept: list[str] = []
        for image_url in image_urls:
            term = _non_appearance_term(alts.get(image_url, ""), question)
            if term:
                logger.info("图片出口语义否决：这张图的 alt 写着「%s」，不是外观/结构图，不随答案展示", term)
                continue
            kept.append(image_url)
        return kept

    # 答案正文里的 markdown 图片（允许描述文字带空格）；URL 捕获后交给白名单判定。
    _ANSWER_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(\S*?)(?:\s+\"[^\"]*\")?\s*\)")

    @classmethod
    def _sanitize_unverified_images(cls, answer: str, verified_urls: set[str]) -> str:
        """剥掉答案正文里未通过白名单校验的 markdown 图片。

        模型会臆造\n![外观图](http://...不存在的链接)\n（尤其参考内容里有图片语法模板时），
        这些 URL 不在召回切片里，前端渲染就是裂图；整行删除并收敛多余空行，
        顺带去掉了配套的\n图片说明：...\n文字（用户要求只贴图不写说明）。
        """
        if not answer:
            return answer

        removed: list[str] = []

        def _drop_if_unverified(match: re.Match) -> str:
            url = match.group(1).strip().rstrip(",.;，。；）")
            if url and url in verified_urls:
                return match.group(0)
            removed.append(match.group(0))
            return ""

        cleaned = cls._ANSWER_MD_IMAGE_RE.sub(_drop_if_unverified, answer)
        if removed:
            logger.info("剔除了 %d 处白名单外的臆造图片引用", len(removed))
            # 配套的"图片说明：…"行（可能在图片前或后）只为解释图片而存在，图都没了就没必要保留；
            # 用户也明确要求只贴图不写说明。按整行删，不限定紧邻被删图片（说明行位置多变）。
            kept_lines = [
                line for line in cleaned.splitlines()
                if not re.match(r"^\s*(?:[-*]\s*)?图片说明\s*[:：]", line)
            ]
            cleaned = "\n".join(kept_lines)
            # 行内删除后会留下连续空行，收敛到最多一个空行。
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    def _step_4_write_history(self, state: QueryGraphState, image_urls=None) -> QueryGraphState:
        """
        阶段四：把本轮答案写入 MongoDB history。
        利用 utils/mongo_history_utils.py 中的 save_chat_message 方法。
        """
        session_id = state.get("session_id", "default")
        answer = (state.get("answer") or "").strip()
        item_names = state.get("item_names") or []

        try:
            if answer:
                save_chat_message(
                    session_id=session_id,
                    role="assistant",
                    text=answer,
                    rewritten_query="",
                    item_names=item_names,
                    image_urls=image_urls,
                    message_id=None
                )
        except Exception as e:
            # 写历史失败不应影响主链路
            logger.error(f"写入Mongo历史记录失败: {e}")

        return state

if __name__ == "__main__":

    # 日志只由进程入口配置，手动跑本脚本时显式挂一次控制台 handler。
    configure_logging()
    logger.info("开始测试: 答案生成节点")

    # 1. 构造模拟数据
    # 模拟重排序后的文档列表 (reranked_docs)
    # 包含：本地文档（带Markdown图片）、联网结果（带URL字段）、纯文本文档
    mock_reranked_docs = [
        {
            "chunk_id": "local_101",
            "source": "local",
            "title": "HAK 180 烫金机操作手册_v2",
            "score": 0.95,
            "url": None,
            "content": """
HAK 180 烫金机的操作面板位于机器正前方。
开启电源后，您需要先设置温度，默认建议设置在 110℃ 左右。
具体的操作面板布局请参考下图：
![操作面板布局图](http://127.0.0.1:9000/knowledge-base/upload-images/hak180产品安全手册/048c005b198be5c9fff80ad6a6ba02496f38fa109ec20dbaabde3110f3eb1574.jpg)

如果是进行局部烫金，请调节侧面的旋钮。
![侧面旋钮细节](http://127.0.0.1:9000/knowledge-base/upload-images/hak180产品安全手册/f77da4df52517fc50b9efb528540e1351dd1a08dce6f801cf08366540f2c59ce.jpg)
"""
        },
        {
            "chunk_id": None,
            "source": "web",
            "title": "HAK 180 常见故障排除 - 官网",
            "score": 0.88,
            "url": "http://127.0.0.1:9000/knowledge-base/upload-images/%E5%8D%8E%E4%B8%BA%E6%93%8E%E4%BA%91G740%E7%94%A8%E6%88%B7%E6%8C%87%E5%8D%97-(KLVG-16Z,Windows11_02,zh-cn)/c28a751c315a89fb5f3b52736a7996b56971c9a260a0e2b850eb5ef18beabf3c.jpg",  # 这是一个直接指向图片的URL（虽然少见，但用于测试提取）
            "content": "如果机器无法加热，请检查保险丝是否熔断..."
        },
        {
            "chunk_id": "local_102",
            "source": "local",
            "title": "安全注意事项",
            "score": 0.82,
            "url": None,
            "content": "操作时请务必佩戴隔热手套，避免高温烫伤。"
        }
    ]

    # 模拟历史记录
    mock_history = [
        {"role": "user", "text": "你好，这款机器怎么用？"},
        {"role": "assistant", "text": "您好！请问您具体指的是哪一款机器？"},
        {"role": "user", "text": "HAK 180 烫金机"}
    ]

    # 模拟输入状态
    mock_state = {
        "session_id": "test_answer_session_001",
        "original_query": "HAK 180 烫金机怎么操作？",
        "rewritten_query": "HAK 180 烫金机的具体操作步骤和面板设置方法",
        "item_names": ["HAK180烫金机"],
        "history": mock_history,
        "reranked_docs": mock_reranked_docs,
        "is_stream": False,  # 测试非流式
        # "is_stream": True, # 若要测试流式，需确保 SSE 环境或 mock 相关函数
        "answer": None  # 初始无答案
    }

    # 运行节点
    node_answer_output = NodeAnswerOutput()
    result = node_answer_output(mock_state)

    print(result)

