"""查询侧闲聊意图闸（v2：L1 规则模板 + L2 门禁式 LLM 兜底分类）。

设计原则（宁查勿否）：
- L1 规则："短句 + 寒暄模式（子串/正则模板/整句全等）+ 不含任何产品/型号信号"才拦截；
- 通用护栏两道，都在子串表之前：① 实体信号（品牌/品类/型号形状）；② 问法骨架
  （知识问法“什么是X”/“X是什么”/“X和Y的区别”/“怎么办”，以及对话回指“我刚问了什么”）。
  第二道存在的原因是第一道只认识消费电子词表，对技术概念/软件/流程类知识库整体失配；
  骨架只认句式不认词表，换一个领域的库照样生效；
- L2 兜底：规则没接住不等于不是闲聊（中文变体是开放的，如"现在几点了""背一下静夜思"），
  但只给"短且无实体信号、且不含问法骨架"的缝隙输入问一次小模型；正常业务问题不经过这次调用。
  L2 必须携带最近几轮对话（含助手回答、标好说话人）：省略宾语的追问（"简单介绍一下"）
  本轮一个产品词都不沾，孤立看就是"让助手自我介绍"，只看当前句必然误拦（线上实测）；
- 任何不确定或 LLM 异常一律不拦截，交回原有"检索 + 生成"链路，
  保证不新增"产品问题被当闲聊"的回归。

命中闲聊时由调用方（node_prepare_query）预置 state["answer"]，复用查询图既有的
"answer 非空 → _route_after_prepare 走 finish → node_answer_output 原样输出"短路，
从而同时跳过 embedding/检索/重排/大模型生成；在 required 库还能连带跳过
实体对齐那次抽取 LLM，与兜底分类调用相抵，边缘闲聊的净增成本≈0。

@FilePath: processor/query_processor/chitchat_gate.py
"""

from __future__ import annotations

import random
import re

from tool.logger import logger
from utils.entity_patterns import ENTITY_SIGNAL_TERMS, looks_like_model

# 归一化用的标点集合（中英文常见分隔符 + 空白），命中判断按去标点后的整句做子串匹配。
# 连字符类单独说明：“HAK-180”与“HAK180”是同一个型号的两种写法，不去掉会
# 让本库词表探测（normalize_for_match 两侧同一口径）因写法差异漏判。
_PUNCT = "。！？，、；：~～!?,.；:\"'“”‘’（）()【】[]{}<>《》-—–―＿_ \t\n"

# 短句上限：闲聊普遍很短；超过这个长度大概率是有具体诉求，交给检索。
_MAX_QUERY_LEN = 16

# 更严的短上限：情绪/存在类等字面宽松的类目只在这个长度内才考虑（整句大概率只是寒暄）。
_SHORT_QUERY_LEN = 12

# LLM 兜底分类的触发门禁：只给"短且无实体信号"的可疑输入问一次小模型。
# 阈值刻意小于 _MAX_QUERY_LEN：纯知识问法极少短到这个程度；required 库多出的
# 13~16 字区间会先被实体对齐抽取调用覆盖，不再叠加意图分类，控制成本。
_LLM_GATE_MAX_LEN = 12

# 兜底分类的预算控制：与实体抽取同款——4s 硬超时 + 零重试，异常一律放行检索，
# 一次外部调用不能拖垮整条在线问答链路。
_INTENT_LLM_TIMEOUT = 4.0
_INTENT_LLM_RETRIES = 0

# 正则模板：接住固定短语的中文插入/语气词变体（"你是哪一家的模型"vs 登记表里的"哪家的模型"）。
# 去标点归一化后的整句上搜索，实体护栏 + 短句上限已在调用前兜底。
# 主干语序对齐"你+动词+疑问词+（的）+模型"模板，动词组不能吞掉"是"后让疑问词对不上
# （早期版本实测漏掉过"你是哪一家的模型"）；疑问词必选，避免裸"模型"误拦业务问句。
_MODEL_PROBE_RES = (
    re.compile(r"(你|您)们?(是什么|是啥|是|用的?|跑的?|指的?)?(什么|哪个|哪(一)?家|哪家公司|谁的?|啥)(的)?(大)?(模型|llm|ai|人工智能|聊天机器人)"),
    re.compile(r"(哪一家|哪家|哪家公司|什么公司|哪个公司|谁|哪个团队)(开发|研发|训练|生产|出品)"),
    re.compile(r"底层(?:(?:是|用|采用|跑|运行)的?)?(?:是)?(什么|啥)"),
    re.compile(r"(gpt|chatgpt|claude|gemini|文心|通义|千问|qwen|deepseek|豆包|kimi)"),
)
_IDENTITY_RES = (
    # identity 必须“你”是句子的话题主语：旧模板 `你(是|叫|用)?(谁|什么|哪位|啥)` 动词组可省且
    # **无左边界**，等价于“任何 你+疑问代词 的字符串都算”，于是“刚刚我问了你什么”（你在此处是
    # 宾语）被回了一整句自我介绍。左边界只放行“请问/那”这类句首状语（以及“你”之后的
    # “到底/究竟”强调词），子串表继续兜非句首位置的固定短语（“你有什么功能”这类）。
    re.compile(r"^(?:请问|那|那么)?(你|您)(?:到底|究竟)?(是|叫|用)?(谁|什么|哪位|啥)"),
)
_PRESENCE_RES = (
    re.compile(r"你在(哪|哪里|什么地方|干嘛|干什么|做什么|忙什么|有没有|在吗)"),
    re.compile(r"你(有(没有|无)?(生命|意识|实体|身体)|能(看|听)见?我)"),
)
_EMOTION_TERMS = (
    "开心", "高兴", "难过", "生气", "郁闷", "无聊", "emo", "心情",
    "好烦", "烦死", "点赞", "厉害", "辛苦了", "不错", "太棒",
)

# 情绪类目只收"纯陈述"：带疑问标记的句子（"今天心情怎么样"）语义上是开口子闲聊，
# 字面又可能沾业务词，不硬猜，留给 L2 分类决定。
_INTERROGATIVE_MARKERS = ("怎么", "怎样", "什么", "多少", "吗", "呢", "几", "哪", "为何", "为什么")


def _normalize(text: object) -> str:
    """去空白与标点、转小写，用于整句/子串匹配；只保留中英文与数字。"""
    return "".join(ch for ch in str(text or "").strip().lower() if ch not in _PUNCT)


def normalize_for_match(text: object) -> str:
    """归一化的对外入口：闲聊模板匹配、节点侧本库词表探测、以及检索侧实体词表对齐
    （NodePrepareQuery._normalize 直接转发到这里）共用同一口径，避免“去标点”与“只去空白”
    两套写法互相对齐不上导致探测与匹配静默失效（R4）。"""
    return _normalize(text)


def contains_entity_signal(text: object) -> bool:
    """判断文本里是否含产品/型号信号：型号代码、字母数字型号串、或品牌/产品品类词。

    形状正则与词表集中在 utils.entity_patterns（P2-8 归一），本函数只用它的**宽松档**：
    命中只会退化成现状（走检索），不会答错，所以判"像不像实体"越宽越好；拒答那种会造成
    用户可见错误的判定用的是同一模块里的收紧档，两者刻意不是同一个函数。
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    if looks_like_model(raw):
        return True
    normalized = _normalize(raw)
    return any(term in normalized for term in ENTITY_SIGNAL_TERMS)


# 知识问法骨架（R1-2）：只认句式、不认词表的"这是在问概念/问文档"特征。
# 为什么要它：原先唯一的"是不是业务问题"护栏是 entity_patterns 里的品牌/品类词表，
# 对技术概念类库整体失配——"什么是大语言模型"一个实体词都不沾，于是短到这个程度的
# 概念问句就会撞进闲聊子串表。骨架与词表无关，换任何领域的库都同样有效；命中即直接
# 进检索：既不吃 L1 子串表，也不为它付一次 L2 分类调用（最坏 4s）与误判风险。
_KNOWLEDGE_SKELETON_RES = (
    re.compile(r"^(什么|啥)是|^(什么|啥)叫|^何为"),   # 什么是大语言模型 / 什么叫向量库
    re.compile(r"是什么$|是啥$"),                    # llm是什么 / 测试方法是什么
    re.compile(r"(?:的|和)(区别|差别|差异|对比|定义|概念|原理|流程|架构|分类|优缺点|含义|意思)"),
    re.compile(r"怎么(办|做|工作|使用|连接|设置|回事)|如何(工作|使用|设置|连接)"),
)

# 对话回指骨架：问的是“这段对话本身”（刚问了什么 / 上一个问题 / 你刚才的回答），而不是知识库内容。
# 光把 identity 模板锚定还远远不够：这类句子短、又不沾实体信号，锚定后正好落进 L2 门禁
# （≤ 12 字且无实体信号），被小模型再判一次 identity 短路。正确出口是带历史的生成，
# 所以先把“不被闲聊闸吃”钉住；专门的历史回看通道（不白跑一次向量检索）属清单 R9。
_CONVERSATION_REFERENCE_RES = (
    re.compile(r"(?:刚刚|刚才|之前|上次|前面|最初|上面|以上).{0,6}(?:问|提|说|讲|回答|问题|答案)"),
    re.compile(r"(?:我们|我)(?:问|说|提|讲)了?(?:你|什么|啥|哪些|哪几)"),
)

# 骨架护栏的自我约束：句子主语指向助手自身时（"你是什么模型""你能做什么"），或是
# 省掉主语的"底层跑的是什么"这类省略句（上一句在聊助手时常见），句式再像知识问法
# 也仍然是闲聊——通用句式不能反过来把细粒度的身份模板吃掉。
# 这里只排除"指向助手自身"的锚点，不排除 _MODEL_PROBE_RES 里的竞品名单：否则
# "什么是通义千问"这种只是在问概念的句子会被竞品名反噬成 model_probe。
_SELF_REFERENCE_RES = (
    re.compile(r"(你|您)(是|叫|用|会|能|可以|在|有什么|有啥)"),
    re.compile(r"底层(是|啥|用的?|跑的?|采用|运行)"),
)


def is_knowledge_question(text: object) -> bool:
    """命中知识问法骨架或对话回指骨架则判为业务问题。纯函数、零依赖，不参与任何词表判定。"""
    normalized = _normalize(text)
    if not normalized:
        return False
    if any(rx.search(normalized) for rx in _SELF_REFERENCE_RES):
        return False
    return any(
        rx.search(normalized)
        for rx in _KNOWLEDGE_SKELETON_RES + _CONVERSATION_REFERENCE_RES
    )


# 有序匹配表：先细（型号/身份策略）后粗（问候/致谢/告别），用子串命中，靠实体护栏 + 短句上限兜精度。
# 注：这里**只登记带限定成分的短语**。早期版本登记过裸"模型/model"与裸"测试"，
# 结果是"什么是大语言模型""测试方法是什么"这类文档问句在 L1 被直接吃掉（零 LLM 调用、
# 无从降级）；"测试/试一下"这类无诉求输入改由 L2 兜底分类接，避免与"测试方法/测试模式"
# 这类文档词汇撞车。指向助手自身的打探由 _MODEL_PROBE_RES 的正则模板与竞品名接住。
_MATCH_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("model_probe", (
        "是什么模型", "什么大模型", "哪个模型", "哪家的模型", "底层模型", "底层是",
        "你用的什么", "你是gpt", "你是chatgpt", "你是claude", "是gpt吗", "gpt几",
    )),
    ("identity", (
        "你是谁", "你叫什么", "你是什么ai", "你是什么东西", "你是什么机器人", "你是干什么的",
        "你有什么用", "你有什么功能", "你有什么能力", "你叫什么名字",
    )),
    ("self_intro", (
        "自我介绍一下", "自我介绍", "介绍一下你自己", "介绍下你自己", "介绍你自己",
    )),
    ("capability", (
        # "会什么/能做什么"这类短短语收进"你(会|能)"锚定的正则模板（_CAPABILITY_RES），
        # 不再裸子串匹配，避免"设置里会不会什么字样"这类知识问法被误拦。
        # 同一理由摘掉"能提供什么/有什么功能/能回答什么/有什么作用"四条**无主语锚**的裸条目：
        # 它们与省略宾语的属性追问同形（"（这台设备）有什么功能介绍吗"），而 L1 命中是
        # **零降级路径**——不经任何模型、没有"拿不准就放行"的余地，新句子撞进来就是必然误拦
        # （线上实测：这句被回了能力介绍的固定话术）。带"你"的合法打探由 _CAPABILITY_RES 原样接住。
        "你能做什么", "你能干什么", "你能帮我做什么", "你能帮我干嘛", "你会些什么",
        "你都能帮到我",
    )),
    ("greeting", (
        "你好", "您好", "哈喽", "嗨", "hello", "hi", "在吗", "早上好", "晚上好", "下午好",
    )),
    ("thanks", ("谢谢", "多谢", "感谢", "thanks", "thank you", "thankyou")),
    ("bye", ("再见", "拜拜", "goodbye", "bye")),
)

_CAPABILITY_RES = (
    re.compile(r"(你|您)(都)?(会|能|可以|有的?)做?(些什么|什么|啥|哪几?些)"),
    re.compile(r"(你|您)(都)?(能|可以|会)(帮)?我?(们)?(干|做|回答|解决|查|讲|提供)(什么|啥)"),
    re.compile(r"(你|您)有什么(功能|作用|能力)"),
)

# 超短输入走"整句全等"而不是子串：像"你是"只有 2 字，做子串会误伤"你是技术支持吗"这类
# 有实际诉求的问法；但用户单独发一个"你是"（常见于试探/打字中断）时应当被接住，
# 否则就会漏进实体抽取和检索链路，白烧两次大模型调用。键为去标点归一化后的整句。
_EXACT_MATCH: dict[str, str] = {
    "你": "greeting",
    "你是": "identity",
    "您是": "identity",
    "请问": "greeting",
    "在不在": "greeting",
    "你在哪": "presence",
}

# 各闲聊子类型的候选应答，每类 3 条变体，由 reply_for() 随机抽取——
# 同一句话问几次拿到一模一样的答案最容易暴露"固定话术"，变体能明显降低这种感觉；
# 对外统一以"知识库助手"身份自述，不透露底层模型。
# 结构约定：对外只走 reply_for()，不要直接 CHITCHAT_REPLIES[key] 取值（那是元组）。
CHITCHAT_REPLIES: dict[str, tuple[str, ...]] = {
    "model_probe": (
        "我是本知识库的智能助手，专注于结合你上传的文档来解答产品使用、参数配置、售后等技术问题。"
        "至于底层采用了什么模型，这里就不便透露啦——你可以直接把想了解的型号或问题告诉我。",
        "关于我底层用的是什么模型，这边就不方便透露了。我能做的事很明确："
        "基于知识库里的文档，帮你解答产品使用、参数配置、故障排查这些技术问题。",
        "底层模型信息我暂时保密。我更拿手的是查你库里的产品资料——"
        "操作步骤、参数规格、售后政策，报个型号来试试。",
    ),
    "identity": (
        "我是本知识库的智能助手，可以基于知识库里的产品文档回答问题。"
        "把你的问题和具体型号告诉我，我来帮你查找和解答。",
        "我是这个知识库的问答助手，专门帮你查文档：使用说明、参数配置、售后政策都能问。",
        "我是你的知识库助手，负责把库里文档的内容翻出来讲明白。有具体型号或问题尽管问。",
    ),
    "self_intro": (
        "你好！我是本知识库的智能助手。我的工作是：根据已导入知识库的文档，帮你解答产品使用、参数配置、"
        "故障排查、售后政策等问题。你可以直接提问，附上型号会更精准!",
        "简单说，我是这个知识库的专职解答员——产品怎么用、参数怎么查、故障怎么处理、保修怎么算，"
        "我都按库里的文档来回答。提问时带上型号，结果会更准。",
        "我是本知识库的助手，职责是把导入的文档变成能问的答案：操作、配置、故障、售后都可以聊。"
    ),
    "capability": (
        "我可以基于知识库中的文档回答技术问题，例如：产品操作步骤、参数规格、常见故障处理、保修与售后政策等。"
        "请把你关心的型号和问题告诉我。",
        "查操作步骤、对参数规格、找故障处理办法、问保修政策——这些都是我的主场。"
        "说个型号加你的问题就行。",
        "我能帮你解答产品文档里的问题：怎么操作、参数多少、故障怎么排、售后怎么算。"
        "具体想了解哪台设备的什么？",
    ),
    "presence": (
        "我是运行在这个知识库服务里的智能助手，没有具体的物理位置。"
        "随时在线，把你想查的产品或技术问题告诉我就好。",
        "我就住在这些文档里，没有实体的位置。你想查什么产品问题，随时都能叫到我。",
        "我部署在知识库里，谈不上在哪里，但肯定随时都在。有产品或技术问题尽管丢过来。",
    ),
    "emotion": (
        "谢谢你的分享！产品使用、参数配置、故障排查这类问题都可以问我，帮你查资料我在行。",
        "哈哈，收下啦。聊完开心或烦心事，需要查资料的时候记得还有我。",
        "有心情分享是好事！想趁机查点什么产品资料吗？我查文档还挺在行的。",
    ),
    "other": (
        "哈哈，我就当你在夸我啦。产品资料、参数配置、故障排查这些问题，随时丢给我。",
        "收到你的信号啦～比起闲聊，我更擅长查文档，有技术问题随时开问。",
        "嗯哼？这个话题我不接，但论查知识库我可毫不含糊，换个问题试试。",
    ),
    "greeting": (
        "你好！我是本知识库的智能助手，很高兴为你服务。有什么产品或技术方面的问题，随时问我。",
        "你好呀！关于产品使用、参数、故障、售后的问题都可以直接问我。",
        "嗨，我在！今天想查点什么资料？产品或技术问题都接。",
    ),
    "thanks": (
        "不客气！还有其他需要帮忙的，随时告诉我。",
        "能帮上就好～还有想查的尽管说。",
        "不客气，有问题随时回来找我。",
    ),
    "bye": (
        "好的，再见！有需要随时回来找我。",
        "下次见！祝你今天顺利。",
        "拜拜～资料我都替你记着，随时回来查。",
    ),
}


def reply_for(subtype: object) -> str:
    """取该子类型的一条话术（候选内随机）；未知子类型返回空串，由调用方兜底。"""
    candidates = CHITCHAT_REPLIES.get(str(subtype or ""))
    return random.choice(candidates) if candidates else ""


def detect_chitchat(text: object, *, l1_only: bool = False, history_text: str = "") -> str | None:
    """返回命中的闲聊子类型；含产品/型号信号、非短句或未在白名单内时一律返回 None（放行检索）。

    l1_only=True 时只跑规则（供调用方单独验证规则面）；默认规则未命中且输入
    "短且无实体信号"时触发 LLM 兜底分类。节点链路为了控制话术与 intent 的组装顺序，
    直接调用 classify_chitchat_llm 拿完整结果，不走这里的单返回值。
    """
    return detect_chitchat_with_reply(text, l1_only=l1_only, history_text=history_text)[0]


def detect_chitchat_with_reply(
    text: object, *, l1_only: bool = False, history_text: str = ""
) -> tuple[str | None, str]:
    """(子类型, 话术) 一体返回：无论规则命中还是 LLM 兜底命中，话术**一律**取自静态表。

    history_text 只对 L2 有意义：省略宾语的追问孤立看就是"让助手自我介绍"，必须带上文
    （见 classify_chitchat_llm）。曾经这里会把 other 子类型的模型生成 reply 当作话术返回，
    那个口子已按 M9 封掉：它绕过无证据守卫，模型问什么都能"答"出来。
    """
    subtype = _detect_by_rules(text)
    if subtype is not None:
        return subtype, reply_for(subtype)
    if l1_only or not should_ask_llm(text):
        return None, ""
    llm_subtype, llm_reply = classify_chitchat_llm(str(text or ""), history_text)
    if llm_subtype is None:
        return None, ""
    if llm_reply:
        logger.info("闲聊兜底分类附带生成（仅留存不采纳，子类型=%s）：%s", llm_subtype, llm_reply[:80])
    return llm_subtype, reply_for(llm_subtype)


def _detect_by_rules(text: object) -> str | None:
    """L1 规则通道：实体护栏 → 知识问法骨架 → 短句上限 → 正则模板（先细后粗）→ 子串表 → 整句全等表。"""
    if contains_entity_signal(text):
        return None
    if is_knowledge_question(text):
        return None
    normalized = _normalize(text)
    if not normalized or len(normalized) > _MAX_QUERY_LEN:
        return None
    for name, res in (("model_probe", _MODEL_PROBE_RES), ("identity", _IDENTITY_RES), ("capability", _CAPABILITY_RES)):
        if any(rx.search(normalized) for rx in res):
            return name
    for name, phrases in _MATCH_ORDER:
        if any(phrase in normalized for phrase in phrases):
            return name
    # 存在/情绪类字面宽松（"开心""你在哪"），只收更短的整句，降低误伤面。
    if len(normalized) <= _SHORT_QUERY_LEN:
        if any(rx.search(normalized) for rx in _PRESENCE_RES):
            return "presence"
        if (
            any(term in normalized for term in _EMOTION_TERMS)
            and not any(marker in normalized for marker in _INTERROGATIVE_MARKERS)
        ):
            return "emotion"
    # 子串/模板未命中再查整句全等表："你是GPT吗"这类仍会先被上面 model_probe 抢先命中，
    # 全等通道只兜住单独成句的超短输入。
    return _EXACT_MATCH.get(normalized)


def should_ask_llm(text: object) -> bool:
    """L2 门禁：只有"去标点后≤12字、无任何产品/型号信号、且不含问法骨架（知识问法与对话回指）"
    的可疑短输入才值得问一次小模型（命中骨架已确定是业务问题，再问只是白付一次延迟与误判风险）。"""
    return (
        within_llm_gate_len(text, _LLM_GATE_MAX_LEN)
        and not contains_entity_signal(text)
        and not is_knowledge_question(text)
    )


def within_llm_gate_len(text: object, max_len: int) -> bool:
    """按去标点归一化后的长度判断是否在兜底分类门禁内（供节点对 disabled 库收紧阈值）。"""
    normalized = _normalize(text)
    return bool(normalized) and len(normalized) <= max_len


def classify_chitchat_llm(text: str, history_text: str = "") -> tuple[str | None, str]:
    """LLM 兜底分类：返回 (闲聊子类型或 None, 模型给的 reply)。

    history_text 是带角色标注的最近几轮对话（**含助手回答**）。省略宾语的追问本轮不沾任何
    产品词，只看当前句会被判成"让助手自我介绍"（线上实测），必须给它上文。

    返回的 reply **只作日志留存，调用方不得直接采纳**：other 类曾经拿它当答案，等于开了一个
    零检索、零来源、还绕过 R5 无证据守卫的自由生成入口（实测「背一下静夜思」真的背了诗），
    用药剂量/价格/默认密码一类问法同样会被凭空作答。话术一律走 reply_for()。

    任何异常/超时都判为"非闲聊"放行检索（宁查勿否）；未知子类型统一归一到 other，
    保证话术查找不落入无 key 的空白。
    """
    try:
        # 惰性导入：与实体抽取同款，把 langchain/模型配置的重量级依赖留给真正需要调模型的分支。
        from config.lm_config import lm_config
        from processor.query_processor.prompt.intent_classify import (
            INTENT_CLASSIFY_SYSTEM,
            build_intent_classify_prompt,
        )
        from utils.llm_utils import get_llm_client

        client = get_llm_client(
            model=lm_config.item_model,
            json_mode=True,
            timeout=_INTENT_LLM_TIMEOUT,
            max_retries=_INTENT_LLM_RETRIES,
        )
        response = client.invoke([
            ("system", INTENT_CLASSIFY_SYSTEM),
            ("user", build_intent_classify_prompt(str(text or ""), history_text)),
        ])
        verdict = _parse_intent_verdict(getattr(response, "content", response))
    except Exception as exc:  # noqa: BLE001 - 兜住网络/超时/解析等一切异常，放行检索
        logger.warning("闲聊意图兜底分类失败，放行检索：%s", exc)
        return None, ""
    if verdict is None:
        return None, ""
    subtype, reply = verdict
    if subtype not in _KNOWN_SUBTYPES:
        # 白名单内的子类型用静态话术表（reply_for 随机取一条）；表外子类型归入 other，
        # 它的 reply 同样**不采纳**、统一走 reply_for("other")（理由见函数 docstring：
        # 采纳模型生成内容会开出无来源的自由回答入口），保证不会在话术表里查不到 key。
        subtype = "other"
    return subtype, reply


_KNOWN_SUBTYPES = frozenset({"greeting", "thanks", "bye", "identity", "self_intro", "capability", "model_probe", "presence", "emotion", "other"})


def _parse_intent_verdict(content: object) -> tuple[str, str] | None:
    """容错解析分类返回；非闲聊/解析失败返回 None。"""
    data = _loads_json_object(content)
    if not isinstance(data, dict) or not bool(data.get("is_chitchat")):
        return None
    subtype = str(data.get("subtype") or "other").strip().lower()
    if subtype not in _KNOWN_SUBTYPES:
        subtype = "other"
    reply = str(data.get("reply") or "").strip()
    return subtype, reply


def _loads_json_object(content: object) -> object:
    """把模型输出转成 dict：兼容 content 为多段列表 / JSON 字符串；非法 JSON 返回 None。"""
    import json

    if isinstance(content, list):
        content = " ".join(str(part) for part in content)
    raw = str(content or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def chitchat_reply(text: object, history_text: str = "") -> str | None:
    """若判定为闲聊则返回话术（候选内随机），否则 None。话术永远来自静态表，不采纳模型生成内容。"""
    subtype, reply = detect_chitchat_with_reply(text, history_text=history_text)
    if subtype is None:
        return None
    return reply
