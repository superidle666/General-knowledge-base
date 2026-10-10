# -*- coding: utf-8 -*-
"""实体确认闸的纯判据（阶段一）。

processor/query_processor/entity_confirm.py

这里只放"给一段文本和一份名单、回一个布尔或一个列表"的判据：**不发 IO、不读 state、
不写 state**。判定顺序、出口标记与话术都住在 `nodes/node_prepare_query.py`——那里有
`_mark_align_exit` 这个唯一写入点（静态守卫钉住），把判据搬过去就等于把埋点拆散。

为什么判据要单独成一个模块：阶段一的闸有五条，其中四条要在"本轮原文 / 本库名单 / 会话历史"
三个方向上交叉判断。写在节点里会变成第九个私有方法，而节点已经 900 多行；抽出来之后
每条判据都能拿假名单直接验，不必构造 state、不必 monkeypatch LLM 抽取。

与 `utils/entity_patterns.py` 的分工：那份模块回答"这串像不像型号、落在哪个品牌组"（导入侧
与闲聊闸也在用，形状口径必须一致）；本模块回答"确认闸该不该拦、拦下来列谁"。
"""

from __future__ import annotations

import os
import re

from processor.query_processor.base import NodeBase
from processor.query_processor.chitchat_gate import normalize_for_match
from utils.entity_patterns import (
    brand_group_of,
    brand_groups_in_mentions,
    brand_groups_of_names,
    digit_runs,
    has_identity_token,
    is_protocol_or_spec_token,
)

# ---------------------------------------------------------------------------
# 一、严格程度怎么定（库级字段 + 全局兜底开关）
# ---------------------------------------------------------------------------

ENTITY_CONFIRM_MODE_STRICT = "strict"
ENTITY_CONFIRM_MODE_LENIENT = "lenient"

# 确认闸总开关：设成 1/true/yes 时**所有库**一律按宽松档走（＝改造前的判定链）。
# 项目惯例是每道新闸必须能线上一键关掉做基线对比（与 BRAND_GROUP_FILTER_DISABLE 同手法）；
# 这一道尤其重要，因为严格档会把约 8% 的提问变成先反问一句，线上要是不能接受必须能立刻停。
ENTITY_CONFIRM_GATE_DISABLE_ENV = "ENTITY_CONFIRM_GATE_DISABLE"
# 一次会话里最多反问几次（4.4 防死循环）。默认 1：同一道题问第二遍就等于承认系统接不住回答。
# 刻意按"整场会话"而不是"同一道题"计数：判"是不是同一道题"要猜话题，猜错的代价是又问一遍，
# 而那正是这条上限要消灭的东西。
ENTITY_CLARIFY_MAX_ENV = "ENTITY_CLARIFY_MAX"
# 一次反问最多念出几个名字。沿用改造前那个候选封顶值，改 here 不改那里（旧常量已删）。
ENTITY_CLARIFY_CANDIDATE_CAP_ENV = "ENTITY_CLARIFY_CANDIDATE_CAP"

DEFAULT_ENTITY_CLARIFY_MAX = 1
DEFAULT_ENTITY_CLARIFY_CANDIDATE_CAP = 8


def env_int(name: str, default: int) -> int:
    """读一个非负整数环境变量；写坏、写成负数或留空一律回落默认值。

    留 0 是有意义的使用法（`ENTITY_CLARIFY_MAX=0` = 从不反问，等价于把确认闸降级成只筛选），
    所以这里不拿 0 当"没配"，也不加 `if value > 0` 那种把 0 吞掉的判断。
    """
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def resolve_confirm_mode(entity_confirm_mode: object) -> str:
    """库级字段（严格/宽松）+ 全局兜底开关 → 本轮实际走哪一档。

    字段缺失按**严格**处理，不按宽松：接口漏注入一个键就把闸静默关掉，等于线上看起来
    "改造上了但没效果"，比多问一句难发现得多。要临时全库关闸请显式设
    `ENTITY_CONFIRM_GATE_DISABLE=1`，日志与出口统计都会留下宽松档的痕迹。
    """
    if NodeBase._env_disabled(ENTITY_CONFIRM_GATE_DISABLE_ENV):
        return ENTITY_CONFIRM_MODE_LENIENT
    normalized = str(entity_confirm_mode or "").strip().lower()
    if normalized == ENTITY_CONFIRM_MODE_LENIENT:
        return ENTITY_CONFIRM_MODE_LENIENT
    return ENTITY_CONFIRM_MODE_STRICT


def clarify_candidate_cap() -> int:
    """本轮反问能念出几个候选。"""
    return env_int(ENTITY_CLARIFY_CANDIDATE_CAP_ENV, DEFAULT_ENTITY_CLARIFY_CANDIDATE_CAP)


def clarify_max_rounds() -> int:
    """本会话还允许反问几次。"""
    return env_int(ENTITY_CLARIFY_MAX_ENV, DEFAULT_ENTITY_CLARIFY_MAX)


# ---------------------------------------------------------------------------
# 二、把"不是型号"的提及摘出去，并分清本轮自写与历史借来
# ---------------------------------------------------------------------------

def split_product_mentions(mentions: object) -> tuple[list[str], list[str]]:
    """把协议/能力版本号从实体提及里摘出去，返回（像型号的提及，被摘掉的协议词）。

    依据是阶段零实测第 4 条：`wifi7`、`pps1.2`、`蓝牙 5.3` 与型号**形状完全一样**，而它们的
    相似度分数带和真变体重叠（协议词最高 0.5711 > 真变体最低 0.5290），所以只能靠词表黑名单
    排除，不能交给分数或阈值。摘掉之后如果一句里什么都不剩，本轮就按"没提取到实体"处理
    （第五档：单款库直接收窄、多款库先问），而不是拿 `wifi7` 去跟名单做模糊比对。
    """
    products: list[str] = []
    protocols: list[str] = []
    for mention in mentions or []:
        text = str(mention or "").strip()
        if not text:
            continue
        (protocols if is_protocol_or_spec_token(text) else products).append(text)
    return products, protocols


def mentions_written_this_turn(mentions: list[str], raw_text: object) -> list[str]:
    """本轮用户**自己写出来**的提及（铁律 1：从历史借来的不算点名）。

    口径与既有拒答判据 `_query_names_model_code` 完全一致：归一化后必须是本轮原文的子串。
    同一条规矩在这里的作用是界定第五档——"本轮一个型号都没写、只借到一个历史锚"的省略追问
    不算"没提取到实体"，因为拿本库名单去问已经点过名的用户是明知故问（那也是阶段零里
    一票否决不许弄坏的那批题）。
    """
    normalized_text = normalize_for_match(raw_text)
    if not normalized_text:
        return []
    written: list[str] = []
    for mention in mentions or []:
        normalized = normalize_for_match(mention)
        if normalized and normalized in normalized_text:
            written.append(str(mention).strip())
    return written


def distinct_writings(mentions: list[str]) -> list[str]:
    """把同一个型号的多段写法并成一条（多点名否决的**计数前置步骤**）。

    实测里 `thinkpad x1 carbon gen12` 的真实提及是
    `['x1', 'gen12', 'thinkpad x1 carbon gen12']` 三条，直接数条数会把一个型号当成三个实体，
    反而把本来正确的收窄拆掉。合并规则只有一条：归一化后互为包含（短的那条是长的那块的子串）
    就算同一处写法，**保留最长的那条**。代价是 `iPhone 15` 与 `iPhone 15 Pro` 也会被并成一条——
    那种情况下两个写法都能逐字命中名单、走的是"两个都对上不用管"那条，多点名否决本来就不该触发。
    """
    pairs: list[tuple[str, str]] = []
    for mention in mentions or []:
        text = str(mention or "").strip()
        normalized = normalize_for_match(text)
        if normalized:
            pairs.append((text, normalized))
    # 长的优先：先拿最完整的那条写法当基准，后面的短写法只要能被它包含就并进去。
    # 反过来做（短的优先）会把 `thinkpad x1 carbon gen12` 判成两条独立写法，因为基准一上来
    # 就被最短的 `x1` 占了，长写法反而被当成重复项剔掉。
    kept: list[str] = []
    for text, normalized in sorted(pairs, key=lambda pair: len(pair[1]), reverse=True):
        if any(normalized in existing for existing in (normalize_for_match(item) for item in kept)):
            continue
        kept.append(text)
    return kept


# ---------------------------------------------------------------------------
# 三、品牌组与同族：未收录、可列候选都从这里出
# ---------------------------------------------------------------------------

def unlisted_brand_group(mentions: list[str], canonical_names: list[str]) -> str:
    """本轮点名了具体某款（提及带数字），而那个品牌本库压根没有 → 返回品牌组名，否则空串。

    这条必须排在任何"靠形状/相似度认款"的采纳之前（4.8 第 1 条）：实测 `小米 15` 会被推到
    `iPhone 15` 上拿 0.7489，比 `雅巨6t`、`x1` 这些**真变体**的分数还高——只看分数就会把
    用户没问过的型号当成他问的那款，比现状更重。

    只认带数字的提及：`有苹果手机吗` 问的是库里有什么而不是哪一款，那是品牌级那一格，
    不该报未收录。
    """
    covered = brand_groups_of_names(canonical_names)
    if not covered:
        # 名单里一个品牌都认不出来（自有产品名如"星辰一号"）：没资格断言"本库没有这个牌子"
        return ""
    for mention in mentions or []:
        text = str(mention or "").strip()
        if not has_identity_token(text):
            continue
        label = brand_group_of(text)
        if label and label not in covered:
            return label
    return ""


def same_family(mention: str, canonical_names: list[str]) -> list[str]:
    """本库里与这个写法"同一族"的款（品牌组相同或无法定品牌 + 数字系列有交集）。

    同族 = `iPhone 15` 与 `iPhone 15 Pro`（都带 15），不等于 `iPhone 18 Pro`。
    用途有两个，都指向"这一格该问"：

    1. 写法本身歧义（`苹果15`、`15`、`苹果一五`）时把可疑对象列出来——实测这类写法的
       top1 分数高到 0.8849 仍然指错款，**不是模型不准，是语言上就定不下来**；
    2. 反过来证明"查无此系列"：品牌在本库、数字系列却列不出任何一款（`iPhone 17`），
       那绝不能替用户换成 18，退不带过滤检索（R7 那条老规矩接到新闸上）。
    """
    runs = set(digit_runs(mention))
    if not runs:
        return []
    label = brand_group_of(mention)
    hits: list[str] = []
    for name in canonical_names or []:
        text = str(name or "").strip()
        if not text:
            continue
        if label and brand_group_of(text) != label:
            continue
        if runs & set(digit_runs(text)):
            hits.append(text)
    return hits


def unlisted_model_group(mentions: list[str], canonical_names: list[str]) -> str:
    """品牌本库有、那一代却一款都列不出来（`iPhone 17` 库里只有 18）→ 返回那个品牌组名。

    这一格存在的理由只有一条：**绝不能替用户把 17 换成 18**。列不出同族候选时，
    既不能拿本库那款直接收窄（那就是替用户换型号），也不该反问“您想问哪个”（用户已经
    答得很明确，是他要的东西库里没）——正确做法是退不带过滤检索并把它单独记一格。
    与 R7 那条“提及带数字就不按品牌收窄”是同一个取向，只是现在把成因记下来了。
    """
    covered = brand_groups_of_names(canonical_names)
    if not covered:
        return ""
    for mention in mentions or []:
        text = str(mention or "").strip()
        if not has_identity_token(text):
            continue
        label = brand_group_of(text)
        if label and label in covered and not same_family(text, canonical_names):
            return label
    return ""


def is_brand_level_turn(mentions: list[str]) -> bool:
    """本轮提及只到品牌/品类粒度（没带任何数字型号）且能对上品牌组吗（4.8 第 2 条）。

    判据直接复用 `brand_groups_in_mentions`：它内部的规矩就是"只要有一个提及带数字就整体返回空"，
    所以非空 ⇒ 全部提及都不带数字 ⇒ 用户问的是"库里有没有这类东西"，不是"哪一款"。
    这一格**不收窄也不反问**：现状两条都答对了（把库内该品牌全列出来），按确认闸的默认写法
    会变成明知故问。
    """
    return bool(brand_groups_in_mentions(mentions))


# ---------------------------------------------------------------------------
# 四、反问闭环：候选清单怎么读回来、回答怎么认
# ---------------------------------------------------------------------------

# 序号说法：第N个 / 最后那个 / 后面那个 / N款。整句 fullmatch，避免把"第2个USB接口"当成选择。
_ORDINAL_PATTERN = re.compile(
    r"^(?:第|最后|末尾|后面?|前面?)?\s*"
    r"(?P<num>[0-9]{1,2}|[零一二三四五六七八九十两])\s*"
    r"(?:个|条|款|号|项|种|选择|选一)?"
    r"(?:吧|呢|的|了)?[。！!，,、\s]*$"
)
# 中文数字：候选封顶用得到的一到十，`两` 当 2。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# "回答反问"的那句最多这么长（归一化后）。超了就当普通新问题：用户顺手又问了别的事，
# 谓词与实体都该由正常流程重新抽，不该被当成在报型号。
MAX_CLARIFY_ANSWER_CHARS = 24
# 报名字时要求的最短匹配长度，与候选匹配同口径，免得一个字符的碎片命中好几项。
MIN_CLARIFY_ANSWER_MATCH_LEN = 3

# 历史消息里那个新字段的键名：助手发反问时把候选清单原样存下来（4.3 第 1 步）。
CLARIFY_CANDIDATES_FIELD = "clarify_candidates"


def _ordinal_number(token: str) -> int | None:
    """把序号写法转成整数；转不了返回 None。"""
    if token.isdigit():
        return int(token)
    if len(token) == 1:
        return _CN_DIGITS.get(token)
    return None


def clarify_answer_ordinal_index(text: object, candidate_count: int) -> int | None:
    """这句是在按序号挑第几个候选吗（0-based）；不是或越界返回 None。"""
    normalized = normalize_for_match(text)
    if not normalized or candidate_count <= 0:
        return None
    match = _ORDINAL_PATTERN.match(normalized)
    if not match:
        return None
    number = _ordinal_number(match.group("num"))
    if number is None or number < 1 or number > candidate_count:
        return None
    # "最后一个/后面那个/末尾"这类说法不带序号时也认（上面 fullmatch 允许光秃秃"最后"）
    head = normalized[: match.start("num")]
    if number and any(word in head for word in ("最后", "末尾")):
        return candidate_count - 1
    return number - 1


def clarify_answer_by_name(text: object, candidates: list[str]) -> str:
    """这句是直接报名字吗？唯一对上某个候选就返回那个候选，否则空串。

    对不上（含"同时像两项"与"一项都不像"）都返回空串：交回正常抽取流程，
    这样用户被反问后**换了个型号或换了话题**时不会被钉死在旧话题上——4.3 第 4 步说这是
    本方案最隐蔽的坑。
    """
    normalized = normalize_for_match(text)
    if not normalized or len(normalized) > MAX_CLARIFY_ANSWER_CHARS:
        return ""
    hits: list[str] = []
    for candidate in candidates or []:
        key = normalize_for_match(candidate)
        if not key:
            continue
        if normalized == key:
            return candidate
        if min(len(normalized), len(key)) >= MIN_CLARIFY_ANSWER_MATCH_LEN and (
            normalized in key or key in normalized
        ):
            hits.append(candidate)
    return hits[0] if len(hits) == 1 else ""


def match_clarify_answer(text: object, candidates: list[str]) -> str:
    """本轮是不是在回答上一轮反问：是就返回被选中的那一项，否则空串。"""
    if not candidates:
        return ""
    by_name = clarify_answer_by_name(text, candidates)
    if by_name:
        return by_name
    index = clarify_answer_ordinal_index(text, len(candidates))
    return candidates[index] if index is not None else ""


def last_clarify_round(history: object) -> tuple[list[str], str]:
    """最近一次"系统发出的、还没被回答的反问"：返回（候选清单, 触发它的用户问题）。

    两个硬条件，都是为了让闭环不越界：
    1. 那条带候选清单的助手消息之后**不能再有别的用户消息**（有的话那次反问已经翻篇了，
       本轮是新一轮提问，不该再拿旧候选去解释它）；
    2. 清单来自 `clarify_candidates` 字段而不是从回答正文里正则回捞——正文是给人看的句子，
       顿号、括号、"还有其他型号"都可能变，字段才是当时那份名单。
    """
    if not isinstance(history, list):
        return [], ""
    messages = list(history)
    for position in range(len(messages) - 1, -1, -1):
        message = messages[position]
        if not isinstance(message, dict):
            continue
        if _role_of(message) == "user" and str(message.get("text") or "").strip():
            # 反问之后用户又说过话：那次反问已经翻篇
            return [], ""
        if _role_of(message) != "assistant":
            continue
        candidates = _candidate_list_of(message)
        if not candidates:
            continue
        predicate = ""
        for earlier in range(position - 1, -1, -1):
            row = messages[earlier]
            if isinstance(row, dict) and _role_of(row) == "user":
                predicate = str(row.get("text") or "").strip()
                break
        return candidates, predicate
    return [], ""


def count_clarify_rounds(history: object) -> int:
    """本场会话到现在为止已经反问过几次（4.4 的反问上限靠它，不再猜"是不是同一道题"）。"""
    if not isinstance(history, list):
        return 0
    return sum(1 for row in history if isinstance(row, dict) and _candidate_list_of(row))


def _role_of(message: dict) -> str:
    return str(message.get("role") or "").strip().lower()


def _candidate_list_of(message: dict) -> list[str]:
    raw = message.get(CLARIFY_CANDIDATES_FIELD)
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item or "").strip()]


def build_inherited_retrieval_text(chosen: str, predicate: str, current_text: str) -> str:
    """闭环第 3 步：把检索词补成"那一项 + 上一轮问的那个属性"。

    最容易漏的就是这一步——用户答反问时只甩一个型号名（"15 Pro"），不会把"电池多大"重说一遍，
    光有型号没有谓词等于拿半句话去检索。谓词取触发那次反问的那句用户原话；
    里面残留的歧义写法（`苹果15`）刻意不去抠掉：抠中文子串很容易把句子抠烂，
    而这一轮真正起作用的是 `item_names` 过滤 + 补在最前面的规范名，向量那一路被旧写法
    轻微带向同族另一款的余地很小（同族切片本来就常在同一篇文档里）。
    """
    if predicate:
        return f"{chosen} {predicate}"
    return f"{chosen} {current_text}".strip()
