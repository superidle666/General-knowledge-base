"""元指令与对话回指的判定与历史回看（R9-B：纯规则、零外呼）。

两类输入的**话题是这段对话本身**，不是知识库内容，因此拿字面串去检索必然白跑一趟：

- **对话回指**：`刚刚我问了你什么` / `上一个问题是什么` / `你刚才的回答是什么`。
  答案就是历史里那条用户原话，正确出口是零检索零生成直接回读。
  （A 面——这类句子不被闲聊闸吃——已随 R1 落地：`chitchat_gate._CONVERSATION_REFERENCE_RES`。
  本模块负责 B 面，也就是不再白跑一次向量检索。）
- **改写型指令**：`换个说法` / `再次回答` / `继续` / `精简点` / `用表格列一下` / `简单介绍`。
  用户要的是上一轮那个问题的另一种呈现。检索词必须换成**上一轮的真问题**，否则整条链路
  都在检索"换个说法"这四个字；而指令本身又不能留在检索词里（那正是本条要治的污染），
  所以把它单独带进生成阶段的 prompt 当呈现要求。

判定一律取 `fullmatch` 语义：**整句就是一句指令/回指**才认。句子里但凡带上实义内容
（`换个说法讲讲 HAK-180 的温度设置`、`之前问的保修期是几年`）就不认，退回现状继续检索——
方向与闲聊闸、清单闸同一条：漏判只是没优化，误判才会让用户看见答非所问。

@FilePath: processor/query_processor/meta_instruction.py
"""

from __future__ import annotations

import re

from processor.query_processor.chitchat_gate import normalize_for_match
from processor.query_processor.intents import INTENT_CONVERSATION_ECHO, INTENT_META_REWRITE
from processor.query_processor.inventory_gate import is_library_inventory_question

# 两个 intent 值现在住在 intents.py 统一登记（防同一语义两种拼法），本模块只 re-export
# 以保持既有调用方（node_prepare_query）的 import 路径不变。
__all__ = [
    "CONVERSATION_ECHO_TEMPLATE",
    "INTENT_CONVERSATION_ECHO",
    "INTENT_META_REWRITE",
    "is_conversation_echo",
    "is_meta_instruction",
    "is_regeneration_instruction",
    "latest_answerable_user_text",
    "latest_user_text",
]

# 回指命中后的固定话术：把上一轮问题原样回读，并给出两条明确的下一步。
CONVERSATION_ECHO_TEMPLATE = "你刚才问的是：“{question}”。需要我换个说法再答一遍，还是直接问新的问题？"

# 疑问代词：回指型句式必须落在"问的是什么"上，否则不成其为回指问句。
_ASK = r"(?:什么|啥|哪些|哪几?个?|多少|来着)"
# 回指时间词/指代词（历史位置），以及提问动词。
_ANAPHORIC_TIME = r"(?:刚刚|刚才|先前|之前|前面|上面|以上|上次|上一|上回|最初)"
_ASK_VERB = r"(?:问|提问|回答|提|说|讲)"
# 句尾语气与虚词：允许"呢/吗/啊/呀/吧"，也允许重复一遍"的了"。
_TAIL = r"(?:呢|吗|啊|呀|吧|的|了)*$"
# 句首状语：允许"请问/那/那么"这类起头，其它内容一律不许出现在骨架之外。
_HEAD = r"(?:请问|那|那么)?"

_CONVERSATION_ECHO_RES = (
    # 型一：提问动词 + 疑问代词，且必须满足“有历史位置词”或“主语+完成式+你”两者之一：
    # 例：刚刚我问了你什么 / 我之前问的啥 / 之前我问你来着
    # 主语可以出现在时间词前也可以在其后（“我之前问的啥”与“之前我问的啥”同义）。
    # 时间词与“了/过/的＋你”至少得有一个，否则裸“问什么”这类过宽写法会被误吃。
    re.compile(
        rf"^{_HEAD}(?:"
        rf"(?:我们|我|咱)?(?:又|还)?{_ANAPHORIC_TIME}(?:个|次)?(?:我们|我|咱)?(?:又|还)?"
        rf"{_ASK_VERB}(?:了|过)?(?:你|您)?(?:的)?(?:那|这)?(?:个|些)?(?:是|为)?{_ASK}"
        rf"|(?:我们|我|咱){_ASK_VERB}(?:了|过|的)(?:你|您)?(?:是|为)?{_ASK}"
        rf"){_TAIL}"
    ),
    # 型二：（时间词 +（我）+ 动词 + 的）+ 问题/答案 + 是 + 疑问代词
    # 例：上一个问题是什么 / 刚才我问的问题是啥 / 之前那个答案是什么
    re.compile(
        rf"^{_HEAD}(?:{_ANAPHORIC_TIME}(?:个|次)?(?:我们|我)?(?:{_ASK_VERB}(?:了|过)?)?(?:的)?(?:那|这)?(?:个|次)?轮?)?"
        rf"(?:问题|答案|回答|内容)(?:是|为)?{_ASK}{_TAIL}"
    ),
    # 型三：你 +（时间词）+ 的回答/答案 + 是 + 疑问代词
    # 例：你刚才的回答是什么
    re.compile(rf"^{_HEAD}(?:你|您)(?:{_ANAPHORIC_TIME})?(?:的)?(?:回答|答案)(?:是|为)?{_ASK}{_TAIL}"),
)

# 改写型指令：整句就是一句"换个方式再来一版"的短语。登记表而不是宽泛正则——
# 这类说法是封闭的小集合，宁可漏判（退回现状）也不要把带内容的句子当指令。
_REGENERATION_TERMS = frozenset({
    # 重述类
    "换个说法", "换种说法", "换一种说法", "换个表述", "换种表述", "换个方式", "换种方式",
    "换个讲法", "换个说法说", "重新组织", "重新组织一下", "重新说", "重新讲",
    "再次回答", "重新回答", "再回答一次", "再说一遍", "再说一次", "再说一下", "再讲一遍",
    "重新说一遍", "重新回答一遍", "再来一遍", "再来一次", "重复一遍", "重复说一遍", "重新生成",
    # 逗号的坑：这里曾经漏写行尾逗号，集合字面量里相邻两个字符串常量会被 Python
    # **隐式拼接成一个元素**（"再次生成" + "继续" → "再次生成继续"），两条同时失效且
    # 不报错——只有跑到"继续"那句时才会发现它没被识别。每个元素都必须带逗号。
    "再次生成",
    # 接续类
    "继续", "继续说", "继续讲", "接着说", "接着讲", "继续吧", "讲下去", "说下去",
    "然后呢", "接着呢", "往下说",
    # 繁简类
    "精简点", "精简些", "精简一下", "简单点", "简短点", "短一点", "说短点",
    "详细点", "详细说说", "多说一点", "展开说说", "展开讲", "具体点", "细说",
    # 介绍类（与"助手自我介绍"同形的歧义写法）：单独登记是因为线上实测这一族正好落在闲聊闸
    # L2 的门禁里（短、无实体信号、无问法骨架），且字面与"（助手）自我介绍一下"同形——孤立看
    # 只能理解为要助手自我介绍。历史里有产品话题时它的真实诉求是"把上一轮那个产品简述一遍"，
    # 属于本闸的换检索词通道，不该由意图分类去赌。只收**整句就是这几个字**的写法，
    # "简单介绍下 HAK-180 的烫金温度"这类带实义内容的仍然不认。
    "简单介绍", "简单介绍一下", "简单说说", "介绍一下", "介绍下",
    # 格式类
    "用表格", "用表格列", "用表格列一下", "用表格说一下", "用表格展示", "列个表",
    "列个表格", "表格形式", "做成表格", "分点列一下", "分条列一下",
    # 摘要类
    "总结一下", "总结下", "概括一下", "归纳一下", "简短总结", "提炼一下",
})

# 指令 + 回指宾语（"重新回答上面那个问题"）：动词组与回指组都锚死，句尾只能是内容名词。
_REGENERATION_WITH_ANAPHORA = re.compile(
    rf"^{_HEAD}(?:再|再次|重新|又)(?:回答|说|讲|组织|表述|来)(?:一遍|一次|一下)?"
    rf"(?:{_ANAPHORIC_TIME}(?:个|次)?(?:那|这)?(?:个|条|次|轮)?|上面|上一(?:个|条|次|轮))"
    rf"(?:的)?(?:问题|回答|答案|内容)?{_TAIL}"
)


def is_conversation_echo(text: object) -> bool:
    """这句是在问"这段对话本身"（刚才问了什么/上一个问题是什么）吗？只看整句骨架。"""
    normalized = normalize_for_match(text)
    if not normalized:
        return False
    return any(pattern.fullmatch(normalized) for pattern in _CONVERSATION_ECHO_RES)


def is_regeneration_instruction(text: object) -> bool:
    """这句只是要上一轮的另一种呈现（换个说法/再说一遍/用表格）吗？整句全等才算。"""
    normalized = normalize_for_match(text)
    if not normalized:
        return False
    if normalized in _REGENERATION_TERMS:
        return True
    # 允许"换个说法呢""继续吧。""用表格列一下好吗"这类带语气尾巴的写法
    stripped = re.sub(r"(?:呢|吗|啊|呀|吧|了|好)$", "", normalized)
    if stripped and stripped != normalized and stripped in _REGENERATION_TERMS:
        return True
    return bool(_REGENERATION_WITH_ANAPHORA.fullmatch(normalized))


def is_meta_instruction(text: object) -> bool:
    """两类元指令的合并判定，供闲聊闸与探针使用。"""
    return is_conversation_echo(text) or is_regeneration_instruction(text)


def _iter_user_texts(history: object):
    """按时间从后往前产出历史里的用户文本（只认 role == "user"，与 R2 同口径：
    助手答案里罗列的内容不是用户意图的来源）。"""
    if not isinstance(history, list):
        return
    for message in reversed(history):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        text = str(message.get("text") or "").strip()
        if text:
            yield text


def latest_user_text(history: object) -> str:
    """最近一条用户消息原文（回指型用它作答，**不做任何跳过**：上一轮哪怕是被拒答的
    清单句，"你刚才问了什么"的正确答案依然是它）。"""
    for text in _iter_user_texts(history):
        return text
    return ""


def latest_answerable_user_text(history: object) -> str:
    """最近一条**值得再检索一次**的用户问题（改写型用它当新检索词）。

    跳过清单型元问题（R16 已明确内容检索答不了）、元指令与回指句本身（连续两句"继续"
    不该把"继续"当问题重跑）。全部被跳过时返回空串，调用方据此保持现状不改写。
    """
    for text in _iter_user_texts(history):
        if is_meta_instruction(text) or is_library_inventory_question(text):
            continue
        return text
    return ""
