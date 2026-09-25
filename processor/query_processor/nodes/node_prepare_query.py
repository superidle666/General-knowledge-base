"""查询参数校验、默认值准备与检索侧实体词表对齐节点。"""

from __future__ import annotations

import json
import re

from processor.query_processor.base import NodeBase
from processor.query_processor.chitchat_gate import (
    classify_chitchat_llm,
    detect_chitchat,
    normalize_for_match,
    reply_for,
    should_ask_llm,
    within_llm_gate_len,
)
from processor.query_processor.prompt.entity_extract import (
    ENTITY_EXTRACT_SYSTEM,
    build_entity_extract_prompt,
)
from processor.query_processor.state import QueryGraphState
from tool.logger import logger


MODEL_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)+\b")
PRODUCT_PATTERN = re.compile(r"(?:华为|Huawei)[^，。！？\n]{0,24}(?:平板|电脑|笔记本|手机|设备)")
REFERENCE_PATTERN = re.compile(r"(?:这款|这个|该设备|该产品|它|上述(?:设备|产品))")

# 检索侧实体对齐只在 required 库执行。LLM 抽取兜一个硬超时 + 零重试：同步问答跑在线程池里，
# 一次外部调用不能拖垮整条链路，超时/异常一律退回正则，正则也命中不了就走 best-effort 不带过滤。
ENTITY_ALIGN_LLM_TIMEOUT = 4.0
ENTITY_ALIGN_LLM_RETRIES = 0
MAX_ENTITY_MENTIONS = 5
MAX_CLARIFY_CANDIDATES = 8
MAX_HISTORY_MESSAGES_FOR_EXTRACT = 4
HISTORY_CHARS_FOR_EXTRACT = 600
# 子串模糊匹配要求较短串至少这么长，避免把 "1"、"Pro" 这类碎片误配到不相关实体上。
MIN_SUBSTRING_MATCH_LEN = 3
# 闲聊 L2 兜底分类在 disabled 库的收紧门禁：required 库命中闲聊能省掉一次实体抽取
# LLM 调用，兜底分类净成本≈ 0，故沿用 gate 模块的 12 字宽门禁；disabled 库本来没有
# 抽取调用可省，多出的这次分类是纯新增延迟，只在更短的、几乎不可能是业务问题的
# 输入上才付出。
CHITCHAT_LLM_GATE_DISABLED_LEN = 8
# 闲聊 L2 之前的本库词表探测：问题里点名了已收录实体，就是业务问题，不必再花
# 一次小模型调用去确认“这不是闲聊”。过短的键不参与，否则“Pro”这类碎片会把
# “你是谁”误判成业务问题送进检索（与实体对齐侧 MIN_SUBSTRING_MATCH_LEN 同源）。
MIN_VOCAB_PROBE_KEY_LEN = 3

CLARIFY_TEMPLATE = "您是想问以下哪个产品：{candidates}？请补充更明确的型号或产品名。"
REFUSE_TEMPLATE = "当前知识库未收录与该型号相关的产品文档，请确认型号或改用其它知识库。"


class NodePrepareQuery(NodeBase):
    """校验问题和知识库，关闭高成本检索增强默认开关，并对 required 库做实体词表对齐。"""

    name = "node_prepare_query"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        query = str(state.get("original_query") or "").strip()
        library_id = state.get("library_id")
        if not query:
            state["answer"] = "请输入要查询的问题。"
        elif not isinstance(library_id, int) or library_id <= 0:
            state["answer"] = "请选择有效的知识库后再进行查询。"
        state["history"] = list(state.get("history") or [])
        state["rewritten_query"] = self._rewrite_query_with_history(query, state["history"])
        state["item_names"] = list(state.get("item_names") or [])
        state["session_id"] = str(state.get("session_id") or "query-session")
        state["is_stream"] = bool(state.get("is_stream", False))
        state["use_hyde"] = bool(state.get("use_hyde", False))
        state["use_web_search"] = bool(state.get("use_web_search", False))
        state["retrieval_top_k"] = int(state.get("retrieval_top_k") or 20)
        state["top_k"] = int(state.get("top_k") or 5)
        # 闲聊意图闸 v2（disabled/required 都适用）：命中静态话术就预置 answer 并直接返回，
        # 复用查询图既有的“answer 非空 → 跳过检索与生成”短路。
        # 两段式：L1 纯规则零成本先跑；没接住且输入“短且无实体信号”时再让 L2 问一次小模型。
        # 放在实体对齐之前：required 库命中闲聊连带跳过抽取 LLM 调用，与兜底分类相抵；
        # disabled 库无调用可省，故用更严的长度门禁控制新增延迟（见 _gate_allows_intent_llm）。
        if not state.get("answer"):
            if self._chitchat_gate(state):
                return state
        # 实体对齐只在 required 库、且前置校验未直接给出答案时进行；disabled 库整段跳过，保持零回归。
        mode = str(state.get("entity_recognition_mode") or "disabled")
        if mode == "required" and not state.get("answer"):
            self._align_entity(state)
        return state

    # ------------------------------------------------------------------
    # 零、闲聊意图闸：L1 规则 → L2 门禁式 LLM 兜底；命中则预置话术短路整条检索链路
    # ------------------------------------------------------------------

    def _chitchat_gate(self, state: QueryGraphState) -> bool:
        """命中闲聊则就地把 intent/话术写进 state 并返回 True；未命中返回 False 继续主流程。"""
        text = str(state.get("rewritten_query") or state.get("original_query") or "")
        subtype = detect_chitchat(text, l1_only=True)
        # 同子类型候选内随机取一条，避免多次询问得到一模一样的回复暴露固定话术。
        reply = reply_for(subtype)
        if subtype is None and self._gate_allows_intent_llm(state, text):
            try:
                subtype, llm_reply = classify_chitchat_llm(text)
            except Exception as exc:  # noqa: BLE001 - classify 内部已降级，这里再兜一层防意外异常阻断问答
                logger.warning("闲聊兜底分类抛出意外异常，放行检索：%s", exc)
                subtype, llm_reply = None, ""
            if subtype:
                # 已知子类型用静态表（候选内随机）；other 是唯一例外——它代表"闲聊但无固定类目"，
                # 优先用模型顺带生成的回复，生成缺失时才退 other 静态话术，
                # 绝不预置空字符串（空 answer 会被图当成“未短路”带进检索链路）。
                reply = (llm_reply or reply_for("other")) if subtype == "other" else reply_for(subtype)
        if subtype is None:
            return False
        state["intent"] = "chitchat"
        state["answer"] = reply
        return True

    @staticmethod
    def _gate_allows_intent_llm(state: QueryGraphState, text: str) -> bool:
        """L2 分级门禁：先过实体护栏，再过本库词表探测；required 库用 12 字宽门禁（命中可省一次抽取
        调用，净成本≈0）；disabled 库无调用可省，收紧到 8 字，控制纯新增的分类延迟。"""
        if not should_ask_llm(text):
            return False
        if NodePrepareQuery._query_names_known_entity(state, text):
            return False
        mode = str(state.get("entity_recognition_mode") or "disabled")
        if mode == "required":
            return True
        return within_llm_gate_len(text, CHITCHAT_LLM_GATE_DISABLED_LEN)

    @staticmethod
    def _query_names_known_entity(state: QueryGraphState, text: str) -> bool:
        """零成本本库词表探测：命中规范名或别名就判业务问题，跳过闲聊 L2 分类。

        通用品牌词护栏（contains_entity_signal）认不出自有产品名（如“星辰一号”），
        而这正是 required 库最坏 +4s 的那段分类延迟要付出的输入。词表由接口在
        required 库注入（disabled 库拿到空表，探测自然不生效），无需新增任何查库。
        两侧都走闲聊闸的去标点口径，否则“HAK 180”与“HAK-180”会因写法差异漏判。
        探测不中只退化成现状（多一次分类），不会把产品问题误拦成闲聊。
        """
        vocabulary = state.get("entity_vocabulary")
        if not isinstance(vocabulary, list):
            return False
        normalized_query = normalize_for_match(text)
        if not normalized_query:
            return False
        for entry in vocabulary:
            if not isinstance(entry, dict):
                continue
            aliases = entry.get("aliases")
            names = [entry.get("name")]
            if isinstance(aliases, list):
                names.extend(aliases)
            for name in names:
                key = normalize_for_match(name)
                if len(key) >= MIN_VOCAB_PROBE_KEY_LEN and key in normalized_query:
                    return True
        return False

    # ------------------------------------------------------------------
    # 一、实体词表对齐：抽取 → 归一化匹配 → 命中过滤 / 候选反问 / 未收录拒答
    # ------------------------------------------------------------------

    def _align_entity(self, state: QueryGraphState) -> None:
        """把问题里的实体与接口注入的本库词表对齐：命中即过滤，唯一模糊候选直接采纳，
        多候选才反问；查无且非明确型号代码时退回不带过滤检索（best-effort），不轻易拒答。"""
        mentions = self._extract_mentions(state)
        if not mentions:
            # 抽不到实体：不臆断也不拒答，退回不带过滤的检索，尽量仍给出答案（best-effort）。
            return
        index = self._build_vocab_index(state.get("entity_vocabulary"))
        if not index:
            # 词表为空说明本库暂无可对齐实体，同样退回不带过滤检索，避免把正常问题误判为拒答。
            return
        confirmed, candidates = self._match_mentions(mentions, index)
        # item_names 会被下游 build_item_name_filter 转成 Milvus in 表达式并自行封顶。
        if confirmed:
            state["item_names"] = list(confirmed)
            logger.info("检索实体对齐命中 %s 个实体：%s", len(confirmed), "、".join(confirmed))
        elif len(candidates) == 1:
            # 只有一个模糊候选（如 "ace6t" → "一加 Ace 6T"）已足够确定，直接带过滤检索，不再反问：
            # 既避开"以下哪个产品"只有一项的怪问法，也消除了后续确认轮次反复反问的死循环。
            state["item_names"] = list(candidates)
            logger.info("检索实体对齐唯一候选直接采纳：%s", "、".join(candidates))
        elif candidates:
            # 多个模糊候选才是真正的歧义，列出来让用户二选一。
            shown = list(candidates)[:MAX_CLARIFY_CANDIDATES]
            state["answer"] = CLARIFY_TEMPLATE.format(candidates="、".join(shown))
        elif self._query_names_model_code(state, mentions):
            # 仅当【当前问题本身】点名了一个具体型号代码（如 APL-005）而本库词表查无，才拒答以防跨产品串答；
            # 品牌词/前缀/错别字一律走下面的 best-effort，不拒答。
            state["answer"] = REFUSE_TEMPLATE
        # 其余"提到了但没干净命中"（如品牌词 "一加"、错别字 "一家"）：保持 item_names 为空，
        # 退回不带过滤检索，交给生成阶段判断有无相关内容，避免比禁用库还差的硬拒答。

    @staticmethod
    def _build_vocab_index(vocabulary: object) -> list[tuple[str, set[str]]]:
        """接口注入词表转 (规范名 -> 归一化匹配键集合)；规范名即 Milvus item_name，过滤靠它命中。

        同名实体可能来自多篇文档（系列拆篇），按键名合并去重，别名并进同一规范名。
        """
        merged: dict[str, set[str]] = {}
        if not isinstance(vocabulary, list):
            return []
        for entry in vocabulary:
            if not isinstance(entry, dict):
                continue
            canonical = str(entry.get("name") or "").strip()
            if not canonical:
                continue
            keys = merged.setdefault(canonical, set())
            keys.add(NodePrepareQuery._normalize(canonical))
            aliases = entry.get("aliases")
            if isinstance(aliases, list):
                for alias in aliases:
                    normalized = NodePrepareQuery._normalize(alias)
                    if normalized:
                        keys.add(normalized)
        return list(merged.items())

    @staticmethod
    def _match_mentions(mentions: list[str], index: list[tuple[str, set[str]]]) -> tuple[list[str], list[str]]:
        """归一化精确/别名命中进 confirmed；仅子串模糊命中进 candidates；两者都按首次出现顺序去重。"""
        confirmed: dict[str, None] = {}
        candidates: dict[str, None] = {}
        for mention in mentions:
            mention_norm = NodePrepareQuery._normalize(mention)
            if not mention_norm:
                continue
            exact = [canonical for canonical, keys in index if mention_norm in keys]
            if exact:
                for canonical in exact:
                    confirmed[canonical] = None
                continue
            for canonical, keys in index:
                if canonical in confirmed:
                    continue
                if any(
                    min(len(mention_norm), len(key)) >= MIN_SUBSTRING_MATCH_LEN
                    and (mention_norm in key or key in mention_norm)
                    for key in keys
                ):
                    candidates[canonical] = None
        return list(confirmed), list(candidates)

    @staticmethod
    def _query_names_model_code(state: QueryGraphState, mentions: list[str]) -> bool:
        """拒答只针对"当前问题本身点名了一个具体型号代码"：提及既要形如 MODEL_PATTERN（大写字母数字带连字符，
        如 APL-005），又要确实出现在当前问题文本里。历史或助手答案中出现过的代码不算用户提及，
        避免上一轮助手罗列过型号时，本轮品牌/泛问题被误判为"点名了查无的型号"。"""
        query_norm = NodePrepareQuery._normalize(
            str(state.get("rewritten_query") or state.get("original_query") or "")
        )
        return any(
            MODEL_PATTERN.search(str(mention))
            and NodePrepareQuery._normalize(mention) in query_norm
            for mention in mentions
        )

    def _extract_mentions(self, state: QueryGraphState) -> list[str]:
        """正则 + 有界 LLM 双通道抽取实体名，按归一化去重并封顶，保留首个出现的表面形式。"""
        text = str(state.get("rewritten_query") or state.get("original_query") or "")
        history_text = self._recent_history_text(state.get("history"))
        raw_mentions: list[str] = []
        # 正则只扫当前问题：历史里（尤其助手自己罗列的型号清单）混有大量型号代码，
        # 一并扫描会把"上一轮答案引用过的实体"误当成本轮用户提及，污染候选甚至误触发拒答。
        for pattern in (MODEL_PATTERN, PRODUCT_PATTERN):
            raw_mentions.extend(match.strip() for match in pattern.findall(text))
        raw_mentions.extend(self._call_entity_llm(text, history_text))

        ordered: dict[str, str] = {}
        for mention in raw_mentions:
            surface = str(mention).strip()
            normalized = self._normalize(surface)
            if normalized and normalized not in ordered:
                ordered[normalized] = surface
        return list(ordered.values())[:MAX_ENTITY_MENTIONS]

    def _call_entity_llm(self, text: str, history_text: str) -> list[str]:
        """调用模型抽取实体；任何异常/超时都退回空列表，让正则结果继续兜底，绝不阻断问答。"""
        if not text.strip():
            return []
        try:
            # 惰性导入：把 langchain/模型配置的重量级依赖留给真正需要调模型的分支。
            from config.lm_config import lm_config
            from utils.llm_utils import get_llm_client

            client = get_llm_client(
                model=lm_config.item_model,
                json_mode=True,
                timeout=ENTITY_ALIGN_LLM_TIMEOUT,
                max_retries=ENTITY_ALIGN_LLM_RETRIES,
            )
            response = client.invoke(
                [
                    ("system", ENTITY_EXTRACT_SYSTEM),
                    ("user", build_entity_extract_prompt(text, history_text)),
                ]
            )
            return self._parse_entities(getattr(response, "content", response))
        except Exception as exc:  # noqa: BLE001 - 兜住网络/超时/解析等一切异常，退化为正则
            logger.warning("检索实体抽取失败，退回正则结果：%s", exc)
            return []

    @staticmethod
    def _parse_entities(content: object) -> list[str]:
        """容错解析模型返回：兼容 JSON 字符串、{"entities": [...]}、纯列表与多段 content。"""
        if isinstance(content, list):
            content = " ".join(str(part) for part in content)
        raw = str(content or "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(data, dict):
            entities = data.get("entities") or data.get("item_names") or data.get("names")
        elif isinstance(data, list):
            entities = data
        else:
            entities = None
        if not isinstance(entities, list):
            return []
        return [str(entity).strip() for entity in entities if str(entity).strip()]

    @staticmethod
    def _recent_history_text(history: object) -> str:
        """取最近若干条【用户】历史文本拼成 LLM 消歧上下文——刻意排除助手回答，
        否则助手自己罗列过的型号会被模型再抽出来当用户提及。按字符预算从尾部裁剪避免超长。"""
        if not isinstance(history, list):
            return ""
        texts: list[str] = []
        for message in reversed(history):
            if not isinstance(message, dict) or str(message.get("role") or "").strip().lower() != "user":
                continue
            text = str(message.get("text") or "").strip()
            if text:
                texts.append(text)
            if len(texts) >= MAX_HISTORY_MESSAGES_FOR_EXTRACT:
                break
        joined = "\n".join(reversed(texts))
        return joined[-HISTORY_CHARS_FOR_EXTRACT:]

    @staticmethod
    def _normalize(value: object) -> str:
        """归一化：去所有空白 + casefold。必须与导入侧 node_persist_chunks._normalize_entity_name 同规则，
        否则查询侧归一化名与词表/存量 normalized_name 对不齐，精确匹配会静默失效。"""
        return re.sub(r"\s+", "", str(value or "").strip()).casefold()

    @staticmethod
    def _rewrite_query_with_history(query: str, history: list[dict]) -> str:
        """将多轮对话中的最近型号或产品实体补充到当前检索问题。"""
        if not history or not REFERENCE_PATTERN.search(query):
            return query

        recent_text = "\n".join(
            str(message.get("text") or "")
            for message in reversed(history)
            if isinstance(message, dict)
        )
        entities: list[str] = []
        for pattern in (MODEL_PATTERN, PRODUCT_PATTERN):
            for match in pattern.findall(recent_text):
                value = match.strip()
                if value and value not in entities:
                    entities.append(value)
        if not entities:
            return query
        rewritten = f"{' '.join(entities[:3])} {query}".strip()
        return rewritten[:500]
