"""实体形状识别的单一来源：型号正则 + 品牌/品类词表（P2-8）。

查询侧原本散落着三套 subtly 不同的型号正则（prepare_query 的 MODEL_PATTERN、闲聊闸的
_MODEL_CODE_RE / _MODEL_SLUG_RE），导入侧还有一套文件名型号 token。同一串文本在不同
链路里会被判成"是型号 / 不是型号"，改一处忘一处。这里集中成 **两档判定**，各处交叉复用：

- 宽松档 `looks_like_model` / `find_shape_mentions`：只回答"像不像实体"，供闲聊护栏与
  实体提及抽取使用。误判的代价只是多走一次检索，所以尽量放宽（支持单字母开头、无连字符
  的型号串，如 G740 / S25 / X-200）。
- 收紧档 `is_specific_model_code`：只回答"本库查无此型号时该不该拒答"。误拒答是用户可见的
  硬错误，漏拒答只是退回不带过滤的 best-effort 检索，所以坚持确定性最高的"连字符 + 含数字"
  形状：windows10 / ios17 / wifi6 与 G740 的形状无法区分，宁可漏拒。

边界写法：Python re 的 ``\\b`` 把中日韩字符也算作 word char，"问下APL-005多少钱"这种中文
紧邻的写法在旧的 ``\\b[A-Z]...\\b`` 下根本不命中（两侧都取不到边界）。这里统一改用只排除
ASCII 字母数字的前后析观，中文/标点紧邻才算独立 token。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 一、词表：品牌与品类（既是"产品信号"，也用来拼出品类短语）
# ---------------------------------------------------------------------------

# 注：词条一律使用小写与无标点形式——消费方（闲聊闸）是在"去标点 + 转小写"后的文本上做
# 子串匹配的，写成 "Huawei" 这种带大写的形式会静默失配。
BRAND_TERMS: tuple[str, ...] = (
    "华为", "huawei", "小米", "红米", "xiaomi", "redmi",
    "苹果", "apple", "iphone", "ipad", "imac", "macbook", "airpods",
    "一加", "oneplus", "oppo", "vivo", "三星", "samsung", "荣耀", "honor",
    "联想", "thinkpad",
)

# 品类词：可独立成词的产品名词。与品牌词组合即"品牌 + 品类"实体提及（见下 PRODUCT_MENTION_PATTERN）。
CATEGORY_TERMS: tuple[str, ...] = (
    "笔记本", "电脑", "平板", "手机", "耳机", "手表", "设备",
    "路由器", "交换机", "服务器", "打印机", "显示器", "烫金机",
)

# 规格/咨询意图词：沾上就说明问的是产品，但它们拼不出"品牌 + 品类"短语，只进信号表。
SPEC_TERMS: tuple[str, ...] = ("机型", "型号", "配置", "参数")

# 产品信号总表：命中任一即认为"这是业务问题，不是闲聊"。
ENTITY_SIGNAL_TERMS: tuple[str, ...] = BRAND_TERMS + CATEGORY_TERMS + SPEC_TERMS


def _alternation(terms: tuple[str, ...]) -> str:
    """词表转正则分支，长词优先（避免"笔记本电脑"被短词"电脑"截断）并逐个转义。"""
    return "|".join(re.escape(term) for term in sorted({term for term in terms if term}, key=len, reverse=True))


# ---------------------------------------------------------------------------
# 二、两档型号判定
# ---------------------------------------------------------------------------

# 带连字符的型号代码：HAK-180 / DBY-W09 / APL-005 / X-200（单字母开头也可以）。
MODEL_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+(?![A-Za-z0-9])")
# 无连字符的"字母 + 数字"型号串：G740 / S25 / HAK180 / FX8P。
MODEL_SLUG_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]+[0-9]+[A-Za-z0-9]*(?![A-Za-z0-9])")

MODEL_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (MODEL_CODE_PATTERN, MODEL_SLUG_PATTERN)

# 品牌 + 品类短语，如"华为平板""小米 14 手机"。中间间隔用懒惰量词：贪婪会把
# "苹果手机和华为手机"整串吞成一个提及（词表必然匹配不上），懒惰则切成两个短提及。
PRODUCT_MENTION_PATTERN = re.compile(
    rf"(?:{_alternation(BRAND_TERMS)})[^，。！？\n]{{0,24}}?(?:{_alternation(CATEGORY_TERMS)})",
    re.IGNORECASE,
)

# 实体形状抽取顺序：先具体型号代码，再字母数字串，最后品牌品类短语。
ENTITY_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = MODEL_SHAPE_PATTERNS + (PRODUCT_MENTION_PATTERN,)

# 文件名型号先验（导入侧 node_entity_recognition_required 专用）：命中即把**整个文件名**当主实体，
# 所以刻意比查询侧更严——要求至少两个字母的前缀，排除"a-b"这类分隔符写法。
# 它与查询侧不是同一档，不收进 MODEL_SHAPE_PATTERNS，只是集中登记在同一处便于对照。
FILENAME_MODEL_TOKEN = re.compile(r"[A-Za-z]{2,}[A-Za-z0-9]*(?:[-][A-Za-z0-9]+)+")


def looks_like_model(text: object) -> bool:
    """宽松档：文本里是否出现任意一种型号形状（大小写、有无连字符都不挑）。"""
    raw = str(text or "")
    return any(pattern.search(raw) for pattern in MODEL_SHAPE_PATTERNS)


def is_specific_model_code(text: object) -> bool:
    """收紧档：是否"点名了一个具体型号代码"，只用于查无实体时的拒答判定。

    只认带连字符且含 ≥2 位数字的形状（APL-005 / DBY-W09 / HAK-999）。无连字符的
    G740/S25 形状故意不认：windows10、ios17、wifi6 与它同形，判错方向是用户可见的
    错误拒答，而漏判只是退回不带过滤检索（见模块 docstring 的两档划分）。
    """
    match = MODEL_CODE_PATTERN.search(str(text or ""))
    if not match:
        return False
    return sum(char.isdigit() for char in match.group()) >= 2


def find_shape_mentions(text: object) -> list[str]:
    """按形状抽取实体提及候选（不查词表），返回按出现顺序、大小写不敏感去重后的表面形式。

    供"从当前问题抽实体"与"多轮改写补实体"两处共用，避免同一套形状在两个调用点各写一遍
    循环、改一处漏一处。
    """
    raw = str(text or "")
    ordered: dict[str, str] = {}
    for pattern in ENTITY_SHAPE_PATTERNS:
        for match in pattern.findall(raw):
            surface = match.strip()
            if surface:
                ordered.setdefault(surface.casefold(), surface)
    return list(ordered.values())
