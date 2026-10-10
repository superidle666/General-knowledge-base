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
from processor.query_processor.entity_confirm import (
    ENTITY_CONFIRM_MODE_STRICT,
    build_inherited_retrieval_text,
    clarify_candidate_cap,
    clarify_max_rounds,
    count_clarify_rounds,
    distinct_writings,
    is_brand_level_turn,
    last_clarify_round,
    match_clarify_answer,
    mentions_written_this_turn,
    resolve_confirm_mode,
    same_family,
    split_product_mentions,
    unlisted_brand_group,
    unlisted_model_group,
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
from utils.entity_patterns import (
    brand_group_of,
    brand_groups_in_mentions,
    find_shape_mentions,
    is_specific_model_code,
)


# 实体形状（型号代码 / 字母数字串 / 品牌+品类短语）的正则与词表统一住在
# utils.entity_patterns（P2-8），本节点与闲聊闸共用同一套口径，不再自带一份。
REFERENCE_PATTERN = re.compile(r"(?:这款|这个|该设备|该产品|它|上述(?:设备|产品))")

# 检索侧实体对齐只在 required 库执行。LLM 抽取兜一个硬超时 + 零重试：同步问答跑在线程池里，
# 一次外部调用不能拖垮整条链路，超时/异常一律退回正则，正则也命中不了就走 best-effort 不带过滤。
ENTITY_ALIGN_LLM_TIMEOUT = 4.0
ENTITY_ALIGN_LLM_RETRIES = 0
MAX_ENTITY_MENTIONS = 5
MAX_HISTORY_MESSAGES_FOR_EXTRACT = 4
HISTORY_CHARS_FOR_EXTRACT = 600
# 闲聊 L2 兜底分类的历史窗口。与上面抽取通道**刻意不是同一口径**：那条只认 role=="user"，
# 因为助手罗列过的型号会被当成本轮提及、进而污染拒答判定；这一条必须带助手回答——
# 省略宾语的属性追问（线上实测："有什么存储类型"）本轮一个产品词都不沾，唯一的实体锚就
# 在上一轮助手的回答里，只给当前句必然被判成"让助手自我介绍"。
# 允许读助手的理由是后果不对称：分类结果只决定"要不要检索"，不参与任何拒答/清单判定，
# 最坏情况是"没拦住闲聊"＝多花一次检索，与宁查勿否同向。助手回答普遍上千字，
# 单条只取头部（产品名通常在首句），否则最近几轮会被一条长回答挤掉、反而丢了实体锚。
MAX_HISTORY_MESSAGES_FOR_INTENT = 4
INTENT_TURN_CHARS = 120
INTENT_HISTORY_CHARS = 600
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

# M4：省略型追问（"有什么存储类型""多少钱"）的话题锚回填窗口。这类句子把主语整个省掉，
# 拿裸串去全库检索的最近邻经常落在别的型号上（线上实测：话题是 iPhone，答成"该设备 UFS4.1"，
# 漂移从"点名兄弟型号"退化成隐名漂移，更难发现）。
MAX_HISTORY_MESSAGES_FOR_ANCHOR = 4
MAX_RETRIEVAL_TEXT_CHARS = 500
# 一次最多往检索词头上拼几个规范名：多实体本身就是"这句在比几个对象"，全拼上去等于没收窄。
MAX_INJECTED_ENTITY_NAMES = 2

# "本轮是在要产品信息"的骨架（M4 精度闸之一）。回填错产品比不回填更糟——答案会被
# 收窄到错型号上，所以表外短句一律不动；漏判只是退回现状（裸串继续检索），与宁查勿否同向。
_ELLIPSIS_FOLLOWUP_RES = (
    re.compile(r"(介绍|概览|总结|概况|详情|详细|说说|讲讲|看看|查查)"),
    re.compile(r"(有什么|有啥|有哪些|有多少|有几|包含哪些|分几?种|有哪几?种)"),
    re.compile(r"(多少|多大|多重|多长|多厚|几天|几年|多久|什么时候|啥时候|怎么样|咋样|如何)"),
    re.compile(r"(支持|能不能|可不可以|能否|是否|有没有|带不带)"),
    re.compile(r"(怎么|怎样|咋)(办|做|弄|设置|查|看|用|连接|调|换|开|关|清|洗|保养|解决|取消|恢复)"),
)
# 光秃秃一个属性名的极简追问（"续航呢""保修多久"里"多久"已在上面，这里兜住只报属性的写法）。
_ELLIPSIS_ATTRIBUTE_TERMS = (
    "续航", "电池", "屏幕", "内存", "存储", "像素", "相机", "重量", "尺寸", "保修",
    "价格", "售价", "参数", "配置", "版本", "颜色", "接口", "充电", "刷新率", "亮度",
)
# 主语指向助手自身的句子不是省略型追问（"你能做什么"是在打探助手，不是"（这台设备）能做什么"）。
# L1 闲聊闸已经接住大部分，这里是第二道网：它兼住"你可以帮我看看吗"这类不在 L1 表里的写法。
_SELF_DIRECTED_RES = (re.compile(r"(你|您)(都|自己|本人才)?(会|能|可以|有|是|叫|干|做|需要)"),)
# 骨架词/属性词必须顶头出现（最多带一个连接词），这是"本轮没自带主语"的结构性判据。
# 真正要接住的是"（它）有什么存储类型"这类把主语整个省掉的短句；"今天天气怎么样"把主语写
# 全了、与上文产品无关，回填不仅错，还会把句子撑成"带实体信号的业务问题"而卸掉闲聊 L2 闸。
_LEADING_CONNECTIVE_RE = re.compile(r"^(?:那么|那|还有|然后|另外|顺便|再|就|倒)?")

CLARIFY_TEMPLATE = "您是想问以下哪个产品：{candidates}？请补充更明确的型号或产品名。"
REFUSE_TEMPLATE = "当前知识库未收录与该型号相关的产品文档，请确认型号或改用其它知识库。"
# 确认闸新增的三句固定话术（铁律 2："没提型号"和"提了但认不出"问法不同，未收录则直说）。
NO_ENTITY_CLARIFY_TEMPLATE = "您想问哪个产品？本库收录了：{candidates}。"
UNLISTED_BRAND_TEMPLATE = "当前知识库未收录 {brand} 品牌的产品，请确认品牌或改用其它知识库。"
# 候选多到念不完时补的那一句。老项目在这里退回答不中，本次不退：用户直接报名字就能继续。
CLARIFY_OVERFLOW_SUFFIX = "（还有其他型号，请直接报名字）"
# R7：本轮只说到品牌（或品牌 + 品类）时按品牌组把检索收窄到本库那一款。一旦线上怀疑它
# 收窄错了，设 BRAND_GROUP_FILTER_DISABLE=1 就退回“品牌词一律不带过滤”的旧行为。
BRAND_GROUP_FILTER_DISABLE_ENV = "BRAND_GROUP_FILTER_DISABLE"

# ---------------------------------------------------------------------------
# 诊断埋点：实体对齐走了哪条出口（只记录，不参与任何判定）
# ---------------------------------------------------------------------------
# 代码上早就不止“六道出口”了：⑥“裸搜”是三合一兜底（没提到实体 / 词表为空 / 品牌组放弃 /
# 连品牌词都不是），四格的病因和治法完全不同（会话锚 / 重导入 / 品类维度 / 别名扩充），
# 合并成一个值统计就失去意义。再加两格“根本没跑到对齐”的早退，用来区分“没跑”与“跑了没命中”。
# 阶段一的确认闸又添了七格：五道闸各自的新出口（未收录品牌 / 未收录系列 / 多点名否决 /
# 第五档反问 / 单款库收窄 / 到上限降级）加闭环那一格“本轮在回答反问”。
# **增删必须同时改 tests\test_entity_align_exit_probe.py 的 EXPECTED_EXITS 与 eval\exit_probe.py
# 的中文标签**，否则分布表那一列永远空白并被读成“没有这类问题”。
EXIT_HIT_EXACT = "hit_exact"  # ①逐字/别名干净命中 → 带过滤
EXIT_SINGLE_CANDIDATE = "single_candidate"  # ②唯一模糊候选 → 直接采纳，不反问
EXIT_CLARIFY = "clarify"  # ③模糊候选 ≥2 → 反问（短路）
EXIT_REFUSE = "refuse"  # ④本轮点名了收紧档型号代码且查无 → 拒答（短路）
EXIT_BRAND_NARROWED = "brand_narrowed"  # ⑤品牌组恰好命中一款 → 收窄
EXIT_BRAND_GIVEUP = "brand_giveup"  # ⑤有品牌组但 0 款/≥2 款（或开关关了）→ 不猜，落裸搜
EXIT_NO_BRAND_GROUP = "no_brand_group"  # 提到了实体但既没匹配上、也不是品牌词（错字/别称的主信号）
EXIT_NO_MENTION = "no_mention"  # ⑥那句话里根本没有实体形状（属性追问、泛概念）
EXIT_EMPTY_VOCAB = "empty_vocab"  # ⑥本库词表为空（required 空壳库）：纯数据问题
EXIT_NOT_REQUIRED = "not_required"  # disabled 库：整段没跑对齐（对照组）
EXIT_PRE_GATED = "pre_gated"  # 前置闸/参数校验已预置 answer：还没走到对齐
# ---- 阶段一：严格确认闸的六格 + 闭环那一格 ----
EXIT_CLARIFY_ANSWERED = "clarify_answered"  # 本轮是在回答上一轮那句反问，已认下用户挑的那一项
EXIT_UNLISTED_BRAND = "unlisted_brand"  # 闸①：点名的品牌本库根本没有 → 直说未收录（短路）
EXIT_UNLISTED_MODEL = "unlisted_model"  # 闸①后半：品牌本库有、那一代却没有 → 裸搜，绝不替用户换型号
EXIT_MULTI_MENTION_DEFER = "multi_mention_defer"  # 闸④：点了两个写法只对上一个 → 不按实体收窄
EXIT_CONFIRM_CLARIFY = "confirm_clarify"  # 闸⑤：这句没写型号，列本库名单问“您想问哪个”（短路）
EXIT_SINGLE_PRODUCT_NARROWED = "single_product_narrowed"  # 闸⑤前半：本库仅一款 → 直接收窄，不问
EXIT_DEGRADED_ANSWER = "degraded_answer"  # 反问次数已到上限 → 不再问，退不带过滤检索
ENTITY_ALIGN_EXITS: tuple[str, ...] = (
    EXIT_HIT_EXACT, EXIT_SINGLE_CANDIDATE, EXIT_CLARIFY, EXIT_REFUSE,
    EXIT_BRAND_NARROWED, EXIT_BRAND_GIVEUP, EXIT_NO_BRAND_GROUP,
    EXIT_NO_MENTION, EXIT_EMPTY_VOCAB, EXIT_NOT_REQUIRED, EXIT_PRE_GATED,
    EXIT_CLARIFY_ANSWERED, EXIT_UNLISTED_BRAND, EXIT_UNLISTED_MODEL,
    EXIT_MULTI_MENTION_DEFER, EXIT_CONFIRM_CLARIFY, EXIT_SINGLE_PRODUCT_NARROWED,
    EXIT_DEGRADED_ANSWER,
)


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
        # M4：指代通道只认写得出来的"这个/它/该设备"，而省略主语的追问（"有什么存储类型"）
        # 它接不住。这一步在同一条**检索词通道**上把话题实体补进来，不碰原文通道（R2）。
        state["rewritten_query"] = self._backfill_ellipsis_anchor(state["rewritten_query"], state)
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
                self._mark_pre_gated(state, "meta_instruction")
                return self._seal_short_circuit(state)
        # 闲聊意图闸 v2（disabled/required 都适用）：命中静态话术就预置 answer 并直接返回，
        # 复用查询图既有的“answer 非空 → 跳过检索与生成”短路。
        # 两段式：L1 纯规则零成本先跑；没接住且输入“短且无实体信号”时再让 L2 问一次小模型。
        # 放在实体对齐之前：命中闲聊连带跳过抽取 LLM 调用，与兜底分类相抵；但“有调用可省”
        # 只对实体对齐真的会跑的请求成立（required 且词表非空），其余一律用更严的长度门禁
        # 控制纯新增的分类延迟（见 _gate_allows_intent_llm）。
        if not state.get("answer"):
            if self._chitchat_gate(state):
                self._mark_pre_gated(state, "chitchat")
                return self._seal_short_circuit(state)
        # 库级清单型元问题（R16）："知识库有哪些产品"这类问法内容检索答不了（库里没有"清单"
        # 这种切片，召回必然落在手册前言/免责声明上），且成本上也不该为它付检索与生成。
        # 放在实体对齐之前：命中就连带省掉那次抽取 LLM 调用。只在 required 且词表非空的库
        # 生效，判定只吃【本轮原文】（详见 _inventory_gate）。
        if not state.get("answer"):
            if self._inventory_gate(state):
                self._mark_pre_gated(state, "inventory")
                return self._seal_short_circuit(state)
        # 实体对齐只在 required 库、且前置校验未直接给出答案时进行；disabled 库整段跳过，保持零回归。
        mode = str(state.get("entity_recognition_mode") or "disabled")
        if mode == "required" and not state.get("answer"):
            self._align_entity(state)
            # M5：对齐命中的规范名顺手拼回检索词头上。实体过滤只作用在 Milvus 的标量列上，
            # 向量与 Sparse 两路召回看的还是那句话，只改过滤不改文本等于只治了一半。
            if not state.get("answer"):
                self._inject_aligned_entity(state)
        elif mode != "required":
            # disabled 库整段没跑对齐：单独记一格当对照组（也用来证明本埋点对它是零行为影响）。
            # 不记 mentions 维度：那一格需要抽取调用，disabled 库根本没抽，填 0 会被读成“抽不到”。
            self._mark_align_exit(state, EXIT_NOT_REQUIRED, mode=mode)
        else:
            # 走到这里是 mode=="required" 且 answer 已被参数校验预置（空问题 / 无效知识库）。
            # 三道前置闸在上一段就 return 了、各自标过自己的闸名，不会落到这一格。
            self._mark_pre_gated(state, "validation")
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
                # 带最近几轮（含助手回答）：省略宾语的追问孤立看就是"让助手自我介绍"，
                # 只给当前句必然误拦（线上实测），理由见 _recent_dialog_text。
                subtype, llm_reply = classify_chitchat_llm(
                    text, NodePrepareQuery._recent_dialog_text(state.get("history"))
                )
            except Exception as exc:  # noqa: BLE001 - classify 内部已降级，这里再兜一层防意外异常阻断问答
                logger.warning("闲聊兜底分类抛出意外异常，放行检索：%s", exc)
                subtype, llm_reply = None, ""
            if subtype:
                # 话术**一律**走静态表，不采纳模型顺带生成的 reply（M9）。原实现在 other 子类型
                # 上优先用模型现编的那句，等于在检索链路之外开了一个零来源、且被 R5 无证据守卫
                # 第一行（`answer` 非空即放行）直接放过的自由生成入口——线上实测「背一下静夜思」
                # 真的背了诗，而"这个药一次吃多少""默认密码是多少"同样会被凭空作答。
                # 模型那句只落日志供回看（调阈值时有用），从不进 answer。
                if llm_reply:
                    logger.info("闲聊兜底分类附带生成（仅留存不采纳，子类型=%s）：%s", subtype, llm_reply[:80])
                reply = reply_for(subtype)
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
        # 本轮是在回答系统自己发出的反问时不走 L2：“15 Pro”、“第二个”短而无实体信号，
        # 正好落在闲聊门禁里，一旦被 L2 判成闲聊就 pre_gated，闭环永远跑不到——那就是
        # 强制确认方案最怕的死循环（问一句、答一句、又回一句闲聊）。
        clarify_candidates, _predicate = last_clarify_round(state.get("history"))
        if clarify_candidates and match_clarify_answer(text, clarify_candidates):
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
    # 一·零、诊断埋点：出口标记的唯一写入点（只记录，不参与任何判定）
    # ------------------------------------------------------------------

    @staticmethod
    def _mark_align_exit(state: QueryGraphState, exit_tag: str, **signals) -> None:
        """写入本轮实体对齐走了哪条出口。**集中一处的理由只有一个**：静态守卫能钉住“只有这里写”，
        而十几条分支各写一份必然漏写，漏一格就把统计口径带偏。

        `entity_align_exit` / `entity_align_signals` 是诊断字段：检索、生成、接口与前端都不读它们，
        写它们不改变任何行为（包括 disabled 库）。同一请求只会命中一条出口，因此后写不存
        在覆盖问题；`signals` 取并集是为了允许上游先算好、分支再补本格独有的量（如 confirmed_count）。
        """
        merged = dict(state.get("entity_align_signals") or {})
        merged.update(signals)
        state["entity_align_exit"] = exit_tag
        state["entity_align_signals"] = merged
        detail = "、".join(f"{key}={value}" for key, value in sorted(signals.items()))
        logger.info("实体对齐出口=%s%s", exit_tag, f"｜{detail}" if detail else "")

    @staticmethod
    def _align_signals(state: QueryGraphState, mentions: list[str]) -> dict:
        """随出口一起记录的中间量：**全部取自判定过程本来就算过的值**，不引入新规则、不做任何外部调用。

        - `query_rewritten`：检索词与本轮原文是否不同 → 本轮实体是不是靠历史/改写补进来的；
        - `ellipsis_followup`：M4 那个纯文本骨架判据的原始结果（同一个函数、只为记录再跑一次，
          不参与任何决定）。注意它记的是“这句原文像不像省略追问”，**不等于回填是否发生**——
          `_backfill_ellipsis_anchor` 前面还有四道不叠加前置闸；省略主语的追问是“该转反问”的主信号；
        - `brand_group`：提及里解析出的品牌组（R7 的入口条件，按出现顺序），空列表意味这句连品牌都未沾。
        """
        query_text = str(state.get("query_entity_text") or "")
        return {
            "mention_count": len(mentions),
            "mentions": list(mentions),
            # 本轮走的是严格确认闸还是旧链。两档的分布必须分开统计，否则“确认闸有没有用”
            # 这个同题对比会被混成一堆。
            "confirm_mode": resolve_confirm_mode(state.get("entity_confirm_mode")),
            "query_rewritten": str(state.get("rewritten_query") or "") != query_text,
            "ellipsis_followup": NodePrepareQuery._looks_like_ellipsis_followup(query_text),
            "brand_group": list(brand_groups_in_mentions(mentions)),
        }

    def _mark_pre_gated(self, state: QueryGraphState, gate: str) -> None:
        """前置闸/参数校验已短路：对齐根本没跑到，单独标记才不会被当成“对齐后落裸搜”。"""
        self._mark_align_exit(
            state,
            EXIT_PRE_GATED,
            gate=gate,
            mode=str(state.get("entity_recognition_mode") or "disabled"),
        )

    # ------------------------------------------------------------------
    # 一、实体词表对齐：抽取 → 归一化匹配 → 命中过滤 / 候选反问 / 未收录拒答
    # ------------------------------------------------------------------

    def _align_entity(self, state: QueryGraphState) -> None:
        """实体对齐的唯一入口：按库级严格程度分到两条链上，**两条链各自只记自己的出口**。

        分成两条而不是在原链上插 `if strict`：`ENTITY_CONFIRM_GATE_DISABLE=1` 要能退回
        改造前的判定当线上基线（项目惯例：每道新闸都得能一键关掉对比）。原链里每一个分支
        都保着“本轮逐字命中答对的题”与“靠历史锚答对的属性追问”（阶段零的一票否决项），
        把新判据织进去就跟那条基线不可比了。

        严格链的六道出口顺序不能换（阶段零实测定的）：未收录品牌排在任何形状/相似度采纳之前，
        品牌级提问排在反问之前，第五档排在逐字命中与借历史锚之后。
        """
        mentions = self._extract_mentions(state)
        signals = self._align_signals(state, mentions)
        if resolve_confirm_mode(state.get("entity_confirm_mode")) != ENTITY_CONFIRM_MODE_STRICT:
            self._align_entity_best_effort(state, mentions, signals)
            return
        # 闭环排在全部闸之前：用户答反问时只甩一个型号名（“15 Pro”、“第二个”），
        # 那串没有品牌词、也常常抽不出形状，走正常判定会被当成歧义又问一遍。
        if self._resolve_clarify_answer(state, signals):
            return
        self._align_entity_confirm(state, mentions, signals)

    def _align_entity_best_effort(self, state: QueryGraphState, mentions: list[str], signals: dict) -> None:
        """宽松档（与改造前逐字相同的那条链）：命中即过滤，唯一模糊候选直接采纳，
        多候选才反问；查无且非明确型号代码时退回不带过滤检索（best-effort），不轻易拒答。

        五个出口之外还有一条 R7：品牌词对不上规范名不等于无法收窄，本库该品牌只有一款时
        照样能过滤（见 `_apply_brand_group_narrowing`）。
        """
        if not mentions:
            # 抽不到实体：不臆断也不拒答，退回不带过滤的检索，尽量仍给出答案（best-effort）。
            self._mark_align_exit(state, EXIT_NO_MENTION, **signals)
            return
        index = self._build_vocab_index(state.get("entity_vocabulary"))
        if not index:
            # 词表为空说明本库暂无可对齐实体，同样退回不带过滤检索，避免把正常问题误判为拒答。
            self._mark_align_exit(state, EXIT_EMPTY_VOCAB, vocab_size=0, **signals)
            return
        signals["vocab_size"] = len(index)
        confirmed, candidates = self._match_mentions(mentions, index)
        # item_names 会被下游 build_item_name_filter 转成 Milvus in 表达式并自行封顶。
        if confirmed:
            state["item_names"] = list(confirmed)
            logger.info("检索实体对齐命中 %s 个实体：%s", len(confirmed), "、".join(confirmed))
            self._mark_align_exit(state, EXIT_HIT_EXACT, confirmed_count=len(confirmed), **signals)
        elif len(candidates) == 1:
            # 只有一个模糊候选（如 "ace6t" → "一加 Ace 6T"）已足够确定，直接带过滤检索，不再反问：
            # 既避开"以下哪个产品"只有一项的怪问法，也消除了后续确认轮次反复反问的死循环。
            state["item_names"] = list(candidates)
            logger.info("检索实体对齐唯一候选直接采纳：%s", "、".join(candidates))
            self._mark_align_exit(state, EXIT_SINGLE_CANDIDATE, candidate_count=1, **signals)
        elif candidates:
            # 多个模糊候选才是真正的歧义，列出来让用户二选一。
            self._clarify_or_degrade(state, list(candidates), reason="multi_candidates", signals=signals)
        elif self._query_names_model_code(state, mentions):
            # 仅当【当前问题本身】点名了一个具体型号代码（如 APL-005）而本库词表查无，才拒答以防跨产品串答；
            # 品牌词/前缀/错别字一律走下面的 best-effort，不拒答。
            state["answer"] = REFUSE_TEMPLATE
            # `model_code_named` 只在这一格记：它是走到这个 elif 的前提条件，本身恒真；
            # 其余分支没评估过这个判据，补个 False 会被读成“评估过且为假”。
            self._mark_align_exit(state, EXIT_REFUSE, model_code_named=True, **signals)
        else:
            # R7：剩下才是"提到了但没干净命中"（品牌词 `苹果手机`、前缀 `一加`、错别字 `一家`）：
            # 能按品牌组唯一对上本库那一款就收窄，对不上就保持 item_names 为空、退回不带过滤
            # 检索，交给生成阶段判断有无相关内容——既不硬拒答，也不拿猜测去收窄。
            self._apply_brand_group_narrowing(state, mentions, index, signals)

    def _align_entity_confirm(self, state: QueryGraphState, mentions: list[str], signals: dict) -> None:
        """严格档：型号确认了才按它筛选，确认不了就问——包括那句压根没提取到型号的情况。

        六道出口按阶段零的数落成固定顺序，每条都只读不算（判定量全部由前置步骤备好）：

        0. `wifi7`/`PPS1.2`/`蓝牙 5.3` 这类协议版本号先摘掉（它们与型号同形、分数带还重叠，
           只能黑名单挡）；摘完一个不剩就当“这句没写型号”。
        1. 逐字命中优先于一切（一票否决：那批答对的题不能弄坏），但先过多点名否决。
        2. 本库没有的品牌直说未收录；提及只到品牌/品类粒度则不收窄也不反问。
        3. 本轮一个字都没写型号：借到了历史锚就裸搜，没借到才进第五档（单款收窄 / 列名单问）。
        4. 模糊命中：唯一候选且本库没同族另一款直接采纳；写法本身可指向同族多款就降为没把握去问。
        5. 收尾仍是那三条旧出口：收紧档型号代码拒答、品牌本库有但那代查无退裸搜、品牌收窄。
        """
        products, protocols = split_product_mentions(mentions)
        if protocols:
            signals["protocol_tokens"] = list(protocols)
        # 铁律 1：只有本轮用户自己写的才算“点名”，改写从历史借进来的不算（与 R2 拒答护栏同口径）。
        written = mentions_written_this_turn(products, str(state.get("query_entity_text") or ""))
        borrowed = [mention for mention in products if mention not in written]
        if borrowed:
            signals["borrowed_mentions"] = list(borrowed)
        index = self._build_vocab_index(state.get("entity_vocabulary"))
        if not index:
            self._mark_align_exit(state, EXIT_EMPTY_VOCAB, vocab_size=0, **signals)
            return
        signals["vocab_size"] = len(index)
        names = [canonical for canonical, _ in index]
        confirmed, candidates = self._match_mentions(products, index) if products else ([], [])
        # 同族候选只用本轮写法算：历史借来的串拿去圈同族会把上一轮的话题当成歧义对象去问。
        family = self._ambiguous_family(written, names)
        if family:
            signals["family_candidates"] = list(family)
        if confirmed:
            if self._multi_mention_veto(state, written, confirmed, index, names, signals):
                return
            state["item_names"] = list(confirmed)
            logger.info("检索实体对齐命中 %s 个实体：%s", len(confirmed), "、".join(confirmed))
            self._mark_align_exit(state, EXIT_HIT_EXACT, confirmed_count=len(confirmed), **signals)
            return
        unlisted_brand = unlisted_brand_group(written, names)
        if unlisted_brand:
            # 4.8 第 1 条：本库没有的品牌必须在这里否掉。下一档就是相似度（阶段三），
            # 而实测里 `小米 15` → `iPhone 15` 能拿 0.7489，比真变体还高。
            state["answer"] = UNLISTED_BRAND_TEMPLATE.format(brand=unlisted_brand)
            self._mark_align_exit(state, EXIT_UNLISTED_BRAND, unlisted_brand=unlisted_brand, **signals)
            return
        if is_brand_level_turn(products):
            # 4.8 第 2 条：`有苹果手机吗` 问的是库里有什么而不是哪一款，不收窄也不反问，
            # 交给品牌组那一格：恰好一款就收窄，多就退不带过滤检索让生成阶段把该品牌全列出来。
            self._apply_brand_group_narrowing(state, products, index, signals)
            return
        if not written:
            self._handle_missing_entity(state, borrowed=borrowed, names=names, signals=signals)
            return
        if len(candidates) == 1 and len(family) < 2:
            # 模糊命中唯一、且本库没有同族另一款（阶段零那 7 条走的就是这一格）：直接采纳。
            state["item_names"] = list(candidates)
            logger.info("检索实体对齐唯一候选直接采纳：%s", "、".join(candidates))
            self._mark_align_exit(state, EXIT_SINGLE_CANDIDATE, candidate_count=1, **signals)
            return
        ask_pool = self._merge_names(candidates, family)
        if len(ask_pool) > 1:
            # 该问的三种成因共用一条出口：模糊候选多个、写法本身可指向同族多款（`苹果15`）、
            # 以及逐字没命中但同族候选列得出来。问法相同，只换信号里的 reason。
            self._clarify_or_degrade(
                state, ask_pool,
                reason="family_ambiguous" if not candidates else "multi_candidates",
                signals=signals,
            )
            return
        if self._query_names_model_code(state, products):
            state["answer"] = REFUSE_TEMPLATE
            self._mark_align_exit(state, EXIT_REFUSE, model_code_named=True, **signals)
            return
        unlisted_model = unlisted_model_group(written, names)
        if unlisted_model:
            # 品牌本库有、那一代却没收录（`iPhone 17` 而库里只有 18）：同族候选都列不出来，
            # 既不收窄也不反问，退不带过滤检索。老规矩“继不替用户把 17 换成 18”接到新闸上。
            self._mark_align_exit(state, EXIT_UNLISTED_MODEL, unlisted_brand=unlisted_model, **signals)
            return
        self._apply_brand_group_narrowing(state, products, index, signals)

    def _resolve_clarify_answer(self, state: QueryGraphState, signals: dict) -> bool:
        """本轮在回答上一轮那句反问：认下用户挑的那一项并把谓词接回来，返回 True 就走完判定。

        现状是断的：候选列写进了助手消息正文，状态里却没有任何“我刚发了候选、正等回答”的
        痕迹，用户回“15 Pro”或“第二个”会被当成新一轮重抽 → 又问一遍。这里补的是 4.3 的五步：
        ① 候选清单从历史里的 `clarify_candidates` 字段读（不从回答正文正则回捐：正文是给人看的
        句子，顿号、“还有其他型号”都会变）；② 只认还没被回答的那一次反问；③ 命中就写
        `item_names` 并把上一轮的属性补回检索词；④ 对不上候选（用户换了型号或换了话题）
        一律返回 False 交回正常流程——这一条是本方案最隐蔽的坑；⑤ 候选里那一款已被删除时
        同样交回正常流程，不拿一个已经不存在的名字去收窄。
        """
        candidates, predicate = last_clarify_round(state.get("history"))
        if not candidates:
            return False
        text = str(state.get("query_entity_text") or state.get("original_query") or "")
        chosen = match_clarify_answer(text, candidates)
        if not chosen:
            return False
        index = self._build_vocab_index(state.get("entity_vocabulary"))
        if chosen not in {canonical for canonical, _ in index}:
            logger.info("反问候选“%s”已不在本库名单中（文档已删或重导入），交回正常实体判定", chosen)
            return False
        state["item_names"] = [chosen]
        inherited = build_inherited_retrieval_text(chosen, predicate, text)
        if inherited:
            state["rewritten_query"] = inherited[:MAX_RETRIEVAL_TEXT_CHARS]
        self._mark_align_exit(
            state, EXIT_CLARIFY_ANSWERED,
            chosen=chosen, clarify_candidate_count=len(candidates), predicate_inherited=bool(predicate),
            **signals,
        )
        logger.info("已从反问回答中确认实体并继承上一轮谓词：%s｜%s", chosen, state["rewritten_query"][:60])
        return True

    def _handle_missing_entity(
            self,
            state: QueryGraphState,
            *,
            borrowed: list[str],
            names: list[str],
            signals: dict,
    ) -> None:
        """第五档：这句没写出型号。先借历史锚，借不到就问（严格口径，4.2 第五档）。

        三个出口全部拍在阶段零定下的口径上：
        - 借到了线索（从上一轮用户话里回填、本轮自己没写）：算“有线索”，退不带过滤检索而不问。
          拿本库名单去问已经点过名的用户就是明知故问，而且会把“靠历史锚答对的属性追问”
          那批题弄坏（一票否决项）。名单里找不到那条线索时也不反问：宁可少走一路，
          也不替用户猜对象（与 R7 同取向）。
        - 本库只有一款：不问，直接收窄到那款（库里就一个对象，反问比拒答更难受）。
        - 多款：列名单问“您想问哪个产品”；多到念不完只列前几个并明说“还有其他型号，
          请直接报名字”，不像老项目那样退回答不中。
        """
        if borrowed:
            self._mark_align_exit(state, EXIT_NO_MENTION, borrowed_only=True, **signals)
            return
        if not names:
            self._mark_align_exit(state, EXIT_EMPTY_VOCAB, vocab_size=0, **signals)
            return
        if len(names) == 1:
            state["item_names"] = list(names)
            self._mark_align_exit(state, EXIT_SINGLE_PRODUCT_NARROWED, **signals)
            logger.info("本库仅收录一款实体，属性追问直接收窄：%s", names[0])
            return
        self._clarify_or_degrade(state, names, reason="no_entity", signals=signals, whole_library=True)

    def _clarify_or_degrade(
            self,
            state: QueryGraphState,
            pool: list[str],
            *,
            reason: str,
            signals: dict,
            whole_library: bool = False,
    ) -> None:
        """列得出候选就问；已经问到上限就不再问，退成不带过滤检索（4.4 防死循环）。

        **阶段一版降级答只做“不再反问、退裸搜”**：“按最像的那款先答 + 末尾声明”要等阶段三
        有相似度分数才有“最像”可挑，现在手里没有任何分数，挑一个就是拿猜测去收窄。

        候选清单原样写进 `entity_clarify_candidates`，由接口落到本轮助手历史消息上（闭环第 1 步）。
        发反问这一轮**不算确认上**：`item_names` 保持为空，候选也不拿去向下一轮当话题锚
        （现有规矩只锚用户真点过名的，绝不锚助手列过的清单）。
        """
        cap = clarify_candidate_cap()
        limit = clarify_max_rounds()
        asked = count_clarify_rounds(state.get("history"))
        if limit <= 0 or asked >= limit:
            self._mark_align_exit(
                state, EXIT_DEGRADED_ANSWER,
                clarify_reason=reason, candidate_count=len(pool), asked_rounds=asked, **signals
            )
            logger.info("本会话已反问 %s 次（上限 %s），这次退成不带过滤检索（原因=%s）", asked, limit, reason)
            return
        shown = list(pool)[:cap]
        state["entity_clarify_candidates"] = shown
        extra = {"clarify_reason": reason, "candidate_count": len(pool), "shown_count": len(shown)}
        if whole_library:
            tail = CLARIFY_OVERFLOW_SUFFIX if len(pool) > cap else ""
            state["answer"] = NO_ENTITY_CLARIFY_TEMPLATE.format(candidates="、".join(shown) + tail)
            # 出口名直接传 EXIT_* 常量（而不是先赋给一个局部变量）：静态守卫靠 `_mark_align_exit(state,` 背面的
            # 常量名认写入点，传变量会被报成“定义了却没人写”。
            self._mark_align_exit(state, EXIT_CONFIRM_CLARIFY, **extra, **signals)
        else:
            state["answer"] = CLARIFY_TEMPLATE.format(candidates="、".join(shown))
            self._mark_align_exit(state, EXIT_CLARIFY, **extra, **signals)

    def _multi_mention_veto(
            self,
            state: QueryGraphState,
            written: list[str],
            confirmed: list[str],
            index: list[tuple[str, set[str]]],
            names: list[str],
            signals: dict,
    ) -> bool:
        """一句话里两个以上互不相同的写法、却只对上一个 → 不收窄（本轮错二的堵法）。

        计数先过 `distinct_writings`：实测里 `thinkpad x1 carbon gen12` 的真实提及是
        `['x1', 'gen12', 'thinkpad x1 carbon gen12']` 三条，直接数个数会把一个型号当三个实体，
        反而拆掉自己本来正确的收窄。

        再过一道“本库认不认得”：`HAK-180 报错 E12 怎么处理` 抽出来是两条写法，但 `E12` 是
        报错码、在本库名单里圈不出任何对象，把它当成第二个型号会把“逐字命中答对”那批题弄坏
        （一票否决项）。只数那些能在本库对上号的写法。两个都对上时不管（多值筛选本来就是通的）。
        """
        writings = distinct_writings(written)
        if len(writings) < 2 or len(confirmed) != 1:
            return False
        product_like = [writing for writing in writings if self._writing_maps_to_library(writing, index, names)]
        if len(product_like) < 2:
            return False
        self._mark_align_exit(
            state, EXIT_MULTI_MENTION_DEFER, writing_count=len(product_like), confirmed_count=len(confirmed), **signals
        )
        logger.info("本轮点名了 %s 个本库认得的不同写法但只对上一个（%s），不按实体收窄检索",
                    len(product_like), confirmed[0])
        return True

    @staticmethod
    def _writing_maps_to_library(writing: str, index: list[tuple[str, set[str]]], names: list[str]) -> bool:
        """这个写法能在本库里圈出对象吗（逐字/别名/模糊候选/同族任一）。只读已备好的名单，不发外部调用。"""
        hit, candidates = NodePrepareQuery._match_mentions([writing], index)
        if hit or candidates:
            return True
        return bool(same_family(writing, names))

    @staticmethod
    def _ambiguous_family(written: list[str], names: list[str]) -> list[str]:
        """本轮写法在本库里能圈出两款以上同族型号时返回那个族（4.8 第 3 条“写法本质歧义”）。

        只算“同族”，不算“相邻”：`iphone15` 逐字等于 `iPhone 15` 时上面已经走 hit_exact，
        不会到这里；`苹果15`/`15`/`iphone15por` 这些写法语言上就定不下来（实测 top1 高到
        0.8849 仍指错款），圈出的就是 [iPhone 15, iPhone 15 Pro] 这一族。
        只圈出一款时不拦（`ace6t`）：拿“库里没另一款”这个事实就能定下来，去问是明知故问。
        """
        merged: dict[str, None] = {}
        for mention in written:
            for name in same_family(mention, names):
                merged[name] = None
        return list(merged)

    @staticmethod
    def _merge_names(candidates: list[str], family: list[str]) -> list[str]:
        """把模糊候选与同族候选并成一个反问清单（按首次出现顺序去重）。

        必须并而不是二选一：`iphone15por` 的模糊命中只有 `iPhone 15`（子串包含判据天然够不到
        15 Pro），只拿 candidates 念出来会把用户真正想要的那一款漏掉——反问念出的名单
        必须完整正确，否则这一格从“答错”变成“问错”，比不开闸还糟。
        """
        merged: dict[str, None] = {}
        for name in list(candidates) + list(family):
            merged[name] = None
        return list(merged)

    def _apply_brand_group_narrowing(
            self,
            state: QueryGraphState,
            mentions: list[str],
            index: list[tuple[str, set[str]]],
            signals: dict,
    ) -> None:
        """本轮只说到品牌（或品牌 + 品类）时，拿品牌组去比本库规范名；**恰好命中一款**才收窄。

        线上实测（库 33“手机商品介绍”）：`有苹果手机吗` → `有什么存储类型` 的第二句拿到的是
        别家型号的参数（`可选版本：12+256GB / … /16+1TB`、`存储不支持 TF 内存卡扩展` 逐字来自
        doc 83 一加 Ace 6T 的两条切片）。根因不在出口校验，而在品牌词 `苹果手机` 与规范名
        `iPhone 18 Pro` 字面永不相交→落到 best-effort 出口→三个型号的切片混在同一窗口里。

        三条刻意不动作的方向（都往"维持现状"收）：
        - 命中 ≥2 款（同品牌下了多篇）不猜：拿猜测收窄会把答案钉到用户没问的那一款上；
        - 提及里只要带了数字（`iPhone 17`）就不收窄：那是在点具体型号，由 `brand_groups_in_mentions`
          统一挡住，不在本节点再写一份同形判据；
        - 只比规范名、不比别名：实测别名里混着 `LPDDR5X`/`NFC` 这类规格噪声，拿它比品牌会
          把不相干实体圈进来。规范名就是 Milvus 的 `item_name`，过滤靠它字面命中。

        `signals` 是上游算好的诊断量，原样透传、只在出口处并集写入，本函数不据此做任何决定。
        """
        if self._env_disabled(BRAND_GROUP_FILTER_DISABLE_ENV):
            # 开关关掉时按“放弃收窄”记，另置 brand_disabled：这批是运维选择，
            # 归到数据问题（0 款/≥2 款）里会把“要不要加品类维度”的结论带偏。
            self._mark_align_exit(state, EXIT_BRAND_GIVEUP, brand_disabled=True, **signals)
            return
        groups = brand_groups_in_mentions(mentions)
        if not groups:
            # 提到了实体、但既没匹配上也不是品牌词：错字/别称（`一家 ace6t`、`iphone15pr`）
            # 主要落在这一格——它是“别名扩充还是向量软对齐”那条决策线的线上信号源。
            self._mark_align_exit(state, EXIT_NO_BRAND_GROUP, **signals)
            return
        matched = [canonical for canonical, _ in index if brand_group_of(canonical) in groups]
        if len(matched) != 1:
            # 0 款或 ≥2 款：不猜。日志把"为什么没收窄"写清楚，免得下个人又归错因。
            logger.info("品牌组 %s 在本库命中 %s 款实体，不做猜测收窄检索", "、".join(groups), len(matched))
            self._mark_align_exit(state, EXIT_BRAND_GIVEUP, brand_matched=len(matched), **signals)
            return
        state["item_names"] = list(matched)
        logger.info("品牌词按品牌组收窄检索：%s → %s", "、".join(groups), matched[0])
        self._mark_align_exit(state, EXIT_BRAND_NARROWED, brand_matched=1, **signals)

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
    def _recent_dialog_text(history: object) -> str:
        """最近几轮对话原文（**含助手回答**并标好说话人），供 L2 判断本轮是不是在追问上文的产品。

        与 _recent_history_text 的区别是刻意的（见该常量处的说明）：抽取通道只能看用户说过什么，
        这条必须看助手答过什么。角色前缀是给模型对齐"哪句是用户问的、哪句是它自己答的"——
        不带标注时模型会把助手上一轮的自述当成本轮诉求，反而更容易判成 identity。
        """
        if not isinstance(history, list):
            return ""
        turns: list[str] = []
        for message in reversed(history):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            speaker = "用户" if role == "user" else "助手" if role == "assistant" else ""
            if not speaker:
                continue
            text = str(message.get("text") or "").strip()
            if not text:
                continue
            turns.append(f"{speaker}：{text[:INTENT_TURN_CHARS]}")
            if len(turns) >= MAX_HISTORY_MESSAGES_FOR_INTENT:
                break
        joined = "\n".join(reversed(turns))
        return joined[-INTENT_HISTORY_CHARS:]

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
        return rewritten[:MAX_RETRIEVAL_TEXT_CHARS]

    # ------------------------------------------------------------------
    # 一·五、省略型追问的话题锚回填（M4）与对齐规范名回填（M5）：都只写检索词通道
    # ------------------------------------------------------------------

    @staticmethod
    def _backfill_ellipsis_anchor(retrieval_text: str, state: QueryGraphState) -> str:
        """本轮把主语整个省掉时，把上文的话题实体拼到检索词头上；拿不准就原样返回。

        与指代通道（`_rewrite_query_with_history`）的分工是刻意的：那条只在本轮**写出了**
        "这个/它/该设备"时才生效，而"有什么存储类型""多少钱"这类追问什么指代词都没有，
        孤立的一句话拿去全库检索，最近邻经常落在别的型号上（线上实测：话题是 iPhone，
        答成"该设备 UFS4.1"）。这一步补的就是那个空洞。

        四道前置闸全部朝"宁可不回填"收：回填错产品比不回填更糟（答案会被实体过滤硬收窄到
        错型号上），而漏回填只是退回现状（裸串继续检索）——
        ① 检索词里已有任何实体形状（本轮自己带的，或指代通道刚借来的）：不叠加；
        ② 本轮点名了本库词表里的实体（纯中文自有产品名正则抽不到，只能查词表）：不补；
        ③ 本轮写了指代词却没借到实体：那是指代通道的事，这里不重复试；
        ④ 本轮不像在要产品信息（"今天天气怎么样"）或主语是助手自己（"你能做什么"）：不动。

        只写 `rewritten_query`，绝不写 `query_entity_text`（R2，已有 AST 守卫钉住）：
        回填进去的历史型号如果被拒答判定当成"用户本轮点名了查无此型"，护栏就成了恒真命题。
        """
        text = str(retrieval_text or "").strip()
        if not text:
            return retrieval_text
        if find_shape_mentions(text) or NodePrepareQuery._query_names_known_entity(state, text):
            return retrieval_text
        if REFERENCE_PATTERN.search(text):
            return retrieval_text
        if not NodePrepareQuery._looks_like_ellipsis_followup(text):
            return retrieval_text
        anchor = NodePrepareQuery._topic_entity_from_history(state)
        if not anchor:
            return retrieval_text
        logger.info("省略型追问已回填话题实体（仅检索词通道）：%s → %s", text[:40], f"{anchor} {text}"[:60])
        return f"{anchor} {text}"[:MAX_RETRIEVAL_TEXT_CHARS]

    @staticmethod
    def _looks_like_ellipsis_followup(text: str) -> bool:
        """本轮是否在要"上文那个产品"的信息：以问法骨架或属性词**顶头**开头，且主语不是助手自己。

        骨架表是"有什么/多少/支持/怎么弄"这一族属性疑问式，加裸属性词（"续航呢"）兜底；
        表外写法一律不认。顶头只能带一个连接词（"那多少钱"）——这条结构性约束的作用是把
        "句子自己把主语写全了"的写法筛掉（"今天天气怎么样"），它们看着也像属性追问，实则是
        换了话题；主语指向助手自身的那句（"你能做什么"）同理是在打探助手，L1 闲聊闸已接住
        大部分，这里再兜一道，免得回填反而把闲聊句撑成业务问题。
        """
        normalized = normalize_for_match(text)
        if not normalized:
            return False
        if any(pattern.search(text) for pattern in _SELF_DIRECTED_RES):
            return False
        head = _LEADING_CONNECTIVE_RE.sub("", normalized, count=1)
        if any(pattern.match(head) for pattern in _ELLIPSIS_FOLLOWUP_RES):
            return True
        return any(head.startswith(term) for term in _ELLIPSIS_ATTRIBUTE_TERMS)

    @staticmethod
    def _topic_entity_from_history(state: QueryGraphState) -> str:
        """从最近几轮【用户】问题里找"当前唯一的话题实体"，有歧义或找不到就返回空串。

        只看用户说过什么（与 `_recent_history_text`/`_rewrite_query_with_history` 同口径，R2）：
        助手回答动辄罗列一排型号，从里面挑"话题"等于替用户猜对象。

        词表优先于正则：required 库的规范名是本库真实存在、且能直接喂给 Milvus 的那个名字，
        比正则的表面形式（"苹果手机"）更值得当锚点；同一句里两者混着收会把一个对象数成
        两个实体、白白放弃回填。disabled 库与词表为空的 required 库拿不到词表，自然走正则。
        """
        history = state.get("history")
        if not isinstance(history, list):
            return ""
        user_texts: list[str] = []
        for message in reversed(history):
            if not isinstance(message, dict) or str(message.get("role") or "").strip().lower() != "user":
                continue
            text = str(message.get("text") or "").strip()
            if text:
                user_texts.append(text)
            if len(user_texts) >= MAX_HISTORY_MESSAGES_FOR_ANCHOR:
                break
        if not user_texts:
            return ""
        vocab_hits = NodePrepareQuery._vocab_entities_in(state, user_texts)
        if vocab_hits:
            # 多个规范名＝上文提过不止一个产品，不猜；退回现状（裸串检索）比选错强
            return vocab_hits[0] if len(vocab_hits) == 1 else ""
        shape_hits: dict[str, str] = {}
        for text in user_texts:
            # 复用同一个形状抽取函数（不是另写一套正则）：口径必须与抽取/拒答那两路一致
            for mention in find_shape_mentions(text):
                shape_hits.setdefault(NodePrepareQuery._normalize(mention), mention)
        if len(shape_hits) == 1:
            return next(iter(shape_hits.values()))
        return ""

    @staticmethod
    def _vocab_entities_in(state: QueryGraphState, texts: list[str]) -> list[str]:
        """本轮之外的若干句用户话里出现过的本库实体规范名（按出现顺序去重）。

        归一化口径与 `_query_names_known_entity` 完全一致（同一个 `_normalize` +
        同一个最短键长闸门），否则会出现"闲聊探测算命中实体、话题锚算查无此型"的分叉。
        """
        vocabulary = state.get("entity_vocabulary")
        if not isinstance(vocabulary, list) or not texts:
            return []
        normalized_texts = [NodePrepareQuery._normalize(text) for text in texts if str(text or "").strip()]
        hits: dict[str, None] = {}
        for entry in vocabulary:
            if not isinstance(entry, dict):
                continue
            canonical = str(entry.get("name") or "").strip()
            if not canonical or canonical in hits:
                continue
            aliases = entry.get("aliases")
            keys = [NodePrepareQuery._normalize(canonical)]
            if isinstance(aliases, list):
                keys.extend(NodePrepareQuery._normalize(alias) for alias in aliases)
            keys = [key for key in keys if len(key) >= MIN_VOCAB_PROBE_KEY_LEN]
            if any(key in text for key in keys for text in normalized_texts):
                hits[canonical] = None
        return list(hits)

    @staticmethod
    def _inject_aligned_entity(state: QueryGraphState) -> None:
        """把对齐命中的规范名补到检索词头上（M5，零新增模型调用：名字本来就在词表里）。

        为什么光有 `item_names` 过滤不够：过滤只作用于 Milvus 的标量列，Dense/Sparse 两路
        召回的向量相似度看的仍是那句话。用户写的是简称（"ace6t"）、对齐出来的是规范名
        （"一加 Ace 6T"），检索词里压根没有品牌词，最近邻排序照样会被兄弟型号带跑。
        已经写在句子里的规范名不重复前置（归一化后比较，避免"HAK-180"与"HAK180"各拼一遍）。
        """
        names = [str(name).strip() for name in (state.get("item_names") or []) if str(name).strip()]
        text = str(state.get("rewritten_query") or "")
        if not names or not text.strip():
            return
        normalized_text = NodePrepareQuery._normalize(text)
        missing = [name for name in names if NodePrepareQuery._normalize(name) not in normalized_text]
        if not missing:
            return
        state["rewritten_query"] = (
            f"{' '.join(missing[:MAX_INJECTED_ENTITY_NAMES])} {text}".strip()[:MAX_RETRIEVAL_TEXT_CHARS]
        )
        logger.info("对齐规范名已补进检索词：%s", state["rewritten_query"][:60])
