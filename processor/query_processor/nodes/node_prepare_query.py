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
from processor.query_processor.inventory_gate import (
    INVENTORY_REJECT_REPLY,
    is_library_inventory_question,
)
from processor.query_processor.meta_instruction import (
    CONVERSATION_ECHO_TEMPLATE,
    INTENT_CONVERSATION_ECHO,
    INTENT_META_REWRITE,
    is_conversation_echo,
    is_regeneration_instruction,
    latest_answerable_user_text,
    latest_user_text,
)
from processor.query_processor.prompt.entity_extract import (
    ENTITY_EXTRACT_SYSTEM,
    build_entity_extract_prompt,
)
from processor.query_processor.state import QueryGraphState
from tool.logger import logger
from utils.entity_patterns import find_shape_mentions, is_specific_model_code


# 实体形状（型号代码 / 字母数字串 / 品牌+品类短语）的正则与词表统一住在
# utils.entity_patterns（P2-8），本节点与闲聊闸共用同一套口径，不再自带一份。
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
        # 两条文本通道仍刻意分开（R2）：检索词需要改写把历史实体补进来（“它/这个”靠它指代消解），
        # 但实体对齐的拒答判定只能看【本轮原文】——否则上一轮出现过的型号会被当成“用户本轮
        # 点名了查无此型”，护栏恒真、泛问题被硬拒答（见 _query_names_model_code）。
        state["query_entity_text"] = query
        state["rewritten_query"] = self._rewrite_query_with_history(query, state["history"])
        state["item_names"] = list(state.get("item_names") or [])
        state["session_id"] = str(state.get("session_id") or "query-session")
        state["is_stream"] = bool(state.get("is_stream", False))
        state["use_hyde"] = bool(state.get("use_hyde", False))
        state["use_web_search"] = bool(state.get("use_web_search", False))
        state["retrieval_top_k"] = int(state.get("retrieval_top_k") or 20)
        state["top_k"] = int(state.get("top_k") or 5)
        # 元指令与对话回指（R9-B）：这类输入的话题是“这段对话本身”，拿字面串检索必然零召回。
        # 必须排在闲聊闸**之前**：“继续”“换个说法”短而无实体信号，正好落在闲聊 L2 的门禁里，
        # 先过闲聊闸会被小模型判成闲聊并预置固定回复，用户要的“再来一版”就永远出不来。
        # 回指型命中即短路（零检索零生成）；改写型不短路、只换检索词，所以必须早于实体对齐
        # ——抽取吃 rewritten_query，换词后实体过滤才跟得上（详见 _meta_instruction_gate）。
        if not state.get("answer"):
            if self._meta_instruction_gate(state):
                return self._seal_short_circuit(state)
        # 闲聊意图闸 v2（disabled/required 都适用）：命中静态话术就预置 answer 并直接返回，
        # 复用查询图既有的“answer 非空 → 跳过检索与生成”短路。
        # 两段式：L1 纯规则零成本先跑；没接住且输入“短且无实体信号”时再让 L2 问一次小模型。
        # 放在实体对齐之前：命中闲聊连带跳过抽取 LLM 调用，与兜底分类相抵；但“有调用可省”
        # 只对实体对齐真的会跑的请求成立（required 且词表非空），其余一律用更严的长度门禁
        # 控制纯新增的分类延迟（见 _gate_allows_intent_llm）。
        if not state.get("answer"):
            if self._chitchat_gate(state):
                return self._seal_short_circuit(state)
        # 库级清单型元问题（R16）："知识库有哪些产品"这类问法内容检索答不了（库里没有"清单"
        # 这种切片，召回必然落在手册前言/免责声明上），且成本上也不该为它付检索与生成。
        # 放在实体对齐之前：命中就连带省掉那次抽取 LLM 调用。只在 required 且词表非空的库
        # 生效，判定只吃【本轮原文】（详见 _inventory_gate）。
        if not state.get("answer"):
            if self._inventory_gate(state):
                return self._seal_short_circuit(state)
        # 实体对齐只在 required 库、且前置校验未直接给出答案时进行；disabled 库整段跳过，保持零回归。
        mode = str(state.get("entity_recognition_mode") or "disabled")
        if mode == "required" and not state.get("answer"):
            self._align_entity(state)
        return self._seal_short_circuit(state)

    @staticmethod
    def _seal_short_circuit(state: QueryGraphState) -> QueryGraphState:
        """静态短路的统一出口：预置了 answer 却没算过来源时补成空列表（J2）。

        拒答/反问/闲聊/校验这几条短路都不进答案节点，`sources` 于是**缺键而不是空列表**；
        下游用硬下标会 KeyError，用 `.get()` 又分不清"算过但没有来源"和"压根没算"（前端 R15
        要靠这个区分是否显示"基于 N 个来源"）。本节点有多处提前 return，兜底只能放在唯一出口上。

        未被短路的请求必须原样不带 sources：来源最终由答案节点按"真正进 prompt 的切片"产出
        （同源口径），这里预置反而会把"还没检索"说成"检索过且没有来源"。
        """
        if state.get("answer") and "sources" not in state:
            state["sources"] = []
        return state

    # ------------------------------------------------------------------
    # 零·三、元指令与对话回指：回指型回读历史短路，改写型只换检索词（R9-B）
    # ------------------------------------------------------------------

    def _meta_instruction_gate(self, state: QueryGraphState) -> bool:
        """命中“对话回指”则预置回读答案并返回 True（短路）；改写型指令只改 state、返回 False。

        - 回指型（`刚刚我问了你什么`）：上一轮的用户原话就是答案，直接回读，零检索零生成零抽取。
          回读**不做任何跳过**——哪怕上一轮是被 R16 拒答的清单句，“你刚才问了什么”的正确答案仍是它。
        - 改写型（`换个说法`/`继续`/`用表格列一下`）：用户要的是上一轮那个问题的另一种呈现，
          于是把检索词换成最近一条“值得再检索一次”的用户问题（跳过清单句与指令句本身），
          指令原文单独放进 `meta_instruction` 交给生成阶段：它绝不能进检索词（那正是本条要治的
          污染），丢了又答不出表格/简略。这条**不短路**，仍要检索证据，所以 R5 的无证据守卫
          照样生效——换个说法也变不出库里没有的内容。

        判定只吃【本轮原文】（与 R2/R16 同口径）：两条骨架都是整句 fullmatch，句子带任何
        实义内容就不认；历史里没有可回看的问题时一律不改写、不短路（宁查勿否，退回现状）。
        """
        text = str(state.get("query_entity_text") or state.get("original_query") or "").strip()
        if not text:
            return False
        history = state.get("history") or []
        if is_conversation_echo(text):
            previous = latest_user_text(history)
            if not previous:
                return False
            state["intent"] = INTENT_CONVERSATION_ECHO
            state["answer"] = CONVERSATION_ECHO_TEMPLATE.format(question=previous[:200])
            # J2：回读型短路同样要产出空来源列表（而不是缺键）
            state["sources"] = []
            logger.info("对话回指已从历史回读（零检索零生成）：%s", text)
            return True
        if is_regeneration_instruction(text):
            previous = latest_answerable_user_text(history)
            if not previous:
                return False
            state["intent"] = INTENT_META_REWRITE
            state["meta_instruction"] = text
            state["rewritten_query"] = previous[:500]
            logger.info("改写型元指令已换用上一轮检索词：%s → %s", text, previous[:80])
        return False

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

    # ------------------------------------------------------------------
    # 〇·五、库级清单型元问题闸：命中就固定话术短路，不进检索也不进生成（R16）
    # ------------------------------------------------------------------

    def _inventory_gate(self, state: QueryGraphState) -> bool:
        """问的是"库本身有哪些产品"而不是"某个产品的内容"时，就地预置拒答话术并返回 True。

        三道反护栏全部取"宁查勿否"：误拒答是用户可见的硬错误，漏拒只是退回现状（继续检索），
        所以只要本轮原文里出现任何实体线索就放走：
        ① 任何型号形状（宽松档 `find_shape_mentions`，本库没有也算"用户给了具体对象"）；
        ② 本库词表里的规范名/别名（纯中文名如"星辰一号"，正则抽不到形状，靠词表命中）；
        ③ 指代词（"这款有哪些型号"问的是上下文里那个对象，该由改写 + 检索处理）。

        文本通道只吃 `query_entity_text`（本轮原文）：与 R2 给拒答护栏立的规矩同口径，
        改写串里被历史回填的内容一律不算"用户本轮在要清单"，否则上一轮问过清单问题之后，
        本轮的普通追问会被连环误拒。

        不启用即零回归：disabled 库、以及 required 但词表为空的空壳库都没有可对齐能力，
        护栏②失效，启用只会把中文实体名误拒（与 `_entity_alignment_available` 同口径）。
        """
        if not self._entity_alignment_available(state):
            return False
        text = str(state.get("query_entity_text") or state.get("original_query") or "")
        if not is_library_inventory_question(text):
            return False
        if REFERENCE_PATTERN.search(text):
            return False
        if find_shape_mentions(text):
            return False
        if self._query_names_known_entity(state, text):
            return False
        state["intent"] = "inventory_refusal"
        state["answer"] = INVENTORY_REJECT_REPLY
        # J2：短路也要产出"来源"，且必须是空列表——缺键与"算过但没有来源"是两回事，
        # 前端按后者隐藏徽标，按前者会拿到 undefined。
        state["sources"] = []
        logger.info("库级清单型元问题已前置拒答（零检索零生成）：%s", text)
        return True

    @staticmethod
    def _gate_allows_intent_llm(state: QueryGraphState, text: str) -> bool:
        """L2 分级门禁：先过实体护栏，再过本库词表探测；只有**实体对齐真的可用**时才用 12 字
        宽门禁。宽门禁的理由是“L2 命中可省掉后面那次抽取 LLM 调用，净成本≈0”；词表为空时
        对齐一进门就早退、无任何调用可省，放宽只会白白多付一次分类延迟并抬高误拦概率
        （required 空壳库实测现场：后台切了 required 但未重导入，Entity 表 0 条）。
        """
        if not should_ask_llm(text):
            return False
        if NodePrepareQuery._query_names_known_entity(state, text):
            return False
        if NodePrepareQuery._entity_alignment_available(state):
            return True
        return within_llm_gate_len(text, CHITCHAT_LLM_GATE_DISABLED_LEN)

    @staticmethod
    def _entity_alignment_available(state: QueryGraphState) -> bool:
        """本请求是否会真正执行实体对齐：mode 为 required **且**接口注入了非空词表。

        必须与 process() 的对齐入口以及 _align_entity 的“词表为空即早退”保持同一口径，
        否则闲聊门禁会按一个换不来收益的条件放宽。
        """
        if str(state.get("entity_recognition_mode") or "disabled") != "required":
            return False
        vocabulary = state.get("entity_vocabulary")
        return isinstance(vocabulary, list) and bool(vocabulary)

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
        """拒答只针对"当前问题本身点名了一个具体型号代码"：提及既要形如收紧档型号代码
        （带连字符且含≥2 位数字，如 APL-005），又要确实出现在**本轮用户原文**里。

        判定对象刻意是 query_entity_text 而不是 rewritten_query（R2）：改写为了指代消解会把
        历史里的型号拼到检索词头上，拿它当"用户点名了型号"等于把拒答护栏变成恒真命题。
        老逻辑里历史（包括助手自己列出的型号清单）和改写串都会走进这个判定，
        用户从没提过的代码足以把一句泛问题回成"未收录该型号"。

        这里刻意不用抽取侧那个宽松档：windows10 / wifi6 / G740 这类形状上跟型号无差异的
        版本号一旦被当成"点名型号"，代价是把本可以 best-effort 回答的问题直接拒掉。
        """
        query_norm = NodePrepareQuery._normalize(
            str(state.get("query_entity_text") or state.get("original_query") or "")
        )
        return any(
            is_specific_model_code(mention)
            and NodePrepareQuery._normalize(mention) in query_norm
            for mention in mentions
        )

    def _extract_mentions(self, state: QueryGraphState) -> list[str]:
        """正则 + 有界 LLM 双通道抽取实体名，按归一化去重并封顶，保留首个出现的表面形式。

        抽取扫的是 rewritten_query（含历史回填的实体，指代追问靠它才能对齐到本库型号）：
        改写只从 role == "user" 的历史取值，所以这里不会出现"助手自己列过的型号"。
        拒答判定不跟着这里走，它只认本轮原文（见 _query_names_model_code）。
        """
        text = str(state.get("rewritten_query") or state.get("original_query") or "")
        history_text = self._recent_history_text(state.get("history"))
        # 正则只扫当前（含历史回填的）问题串：历史文本本身混有大量型号代码（尤其助手
        # 自己罗列的清单），一并扇入会把"上一轮答案引用过的实体"误当成本轮用户提及。
        raw_mentions: list[str] = find_shape_mentions(text)
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
        """实体匹配归一化：去空白 + 去标点（含连字符/下划线）+ casefold，直接复用闲聊闸的
        `normalize_for_match` 口径（R4）。

        为什么必须去连字符：用户照真实写法问 `HAK-180`（旧口径归一化为 `hak-180`），而词表
        规范名多从文件名派生（`hak180产品安全手册`，天然没有连字符），两侧互不包含 →
        判"查无此型" → 又恰好是收紧档型号代码 → 库里明明有这份文档却直接拒答。
        同一句话也不能在闲聊闸的本库词表探测里算"命中实体"、在对齐里算"查无"，
        所以两个口径必须是同一个函数。

        与导入侧 node_persist_chunks._normalize_entity_name 不再是同一规则，这是有意为之：
        后者只写进 `Entity.normalized_name`，全仓无读取方（检索词表由 _build_vocab_index 现算），
        改口径不需要 DB 迁移、也不需要回补存量数据；下一个人请按本说明而不是按"两侧同规则"
        的旧约束把它改回去。
        """
        return normalize_for_match(value)

    @staticmethod
    def _rewrite_query_with_history(query: str, history: list[dict]) -> str:
        """将多轮对话中的最近型号或产品实体补充到当前检索问题。

        只从 role == "user" 的历史取实体（与 _recent_history_text 同口径，R2）：助手回答里
        动辄罗列一堆型号代码，其中全库唯一的"未收录"代码也能被抽出来拼到检索词头上，
        再把本轮泛问题拒答掉。用户自己上一轮提过的型号才是合法的指代对象。
        """
        if not history or not REFERENCE_PATTERN.search(query):
            return query

        recent_text = "\n".join(
            str(message.get("text") or "")
            for message in reversed(history)
            if isinstance(message, dict)
            and str(message.get("role") or "").strip().lower() == "user"
        )
        entities = find_shape_mentions(recent_text)
        if not entities:
            return query
        rewritten = f"{' '.join(entities[:3])} {query}".strip()
        return rewritten[:500]
