"""实体形状识别的单一来源：型号正则 + 品牌组/品类词表（P2-8）。

查询侧原本散落着三套 subtly 不同的型号正则（prepare_query 的 MODEL_PATTERN、闲聊闸的
_MODEL_CODE_RE / _MODEL_SLUG_RE），导入侧还有一套文件名型号 token。同一串文本在不同
链路里会被判成"是型号 / 不是型号"，改一处忘一处。这里集中成 **两档判定 + 一份品牌组**，
各处交叉复用：

- 宽松档 `looks_like_model` / `find_shape_mentions`：只回答"像不像实体"，供闲聊护栏与
  实体提及抽取使用。误判的代价只是多走一次检索，所以尽量放宽（支持单字母开头、无连字符
  的型号串，如 G740 / S25 / X-200）。
- 收紧档 `is_specific_model_code`：只回答"本库查无此型号时该不该拒答"。误拒答是用户可见的
  硬错误，漏拒答只是退回不带过滤的 best-effort 检索，所以坚持确定性最高的"连字符 + 含数字"
  形状：windows10 / ios17 / wifi6 与 G740 的形状无法区分，宁可漏拒。

品牌组（`BRAND_GROUPS` / `brand_group_of` / `brand_groups_in_mentions`）与 `has_identity_token`
回答的是另一个问题：本轮只说到品牌（`苹果手机`）时能不能把它对上本库那一款（R7），以及这个
名字算不算"点到了具体型号"（M6 用它决定要不要比正文）。

边界写法：Python re 的 ``\\b`` 把中日韩字符也算作 word char，"问下APL-005多少钱"这种中文
紧邻的写法在旧的 ``\\b[A-Z]...\\b`` 下根本不命中（两侧都取不到边界）。这里统一改用只排除
ASCII 字母数字的前后析观，中文/标点紧邻才算独立 token。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 一、词表：品牌组与品类（既是"产品信号"，也用来拼出品类短语与品牌收窄）
# ---------------------------------------------------------------------------

# 品牌同义组（R7）：`(组名, 组内词条)`。组名只用于日志与人工阅读，真正参与匹配的是词条。
# 注：词条一律使用小写与无标点形式——消费方（闲聊闸）是在"去标点 + 转小写"后的文本上做
# 子串匹配的，写成 "Huawei" 这种带大写的形式会静默失配。
# 一个品牌必须把机型名里的词也列进同一组：`iPhone 18 Pro` 里压根没有"苹果"二字，
# 只写"苹果/apple"就认不出它，而库里最常见的实体规范名恰恰就是机型名。
BRAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("苹果", ("苹果", "apple", "iphone", "ipad", "imac", "macbook", "airpods")),
    ("华为", ("华为", "huawei")),
    ("小米", ("小米", "红米", "xiaomi", "redmi")),
    ("一加", ("一加", "oneplus")),
    ("OPPO", ("oppo",)),
    ("vivo", ("vivo",)),
    ("三星", ("三星", "samsung")),
    ("荣耀", ("荣耀", "honor")),
    ("联想", ("联想", "thinkpad")),
)

# 品牌词总表由品牌组摊平得到：加一个品牌只改 BRAND_GROUPS 一处，信号表与品类短语正则自动跟上。
BRAND_TERMS: tuple[str, ...] = tuple(term for _, terms in BRAND_GROUPS for term in terms)

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


# ---------------------------------------------------------------------------
# 三、品牌组判定（R7：本轮只说到品牌时，把检索收窄到本库那一款）
# ---------------------------------------------------------------------------

def has_identity_token(name: object) -> bool:
    """这个名字能指到"某一个型号"吗：含数字就算（带连字符型号串与字母数字串都算）。

    话题一致性校验（M6）与品牌收窄（R7）共用这一条判据：`苹果手机` 拼不出具体型号，
    `iPhone 18 Pro` / `HAK180` 能。纯形状、不查词表，所以两处都用它，而不是各自猜一份。

    不要求 `is_specific_model_code` 那个收紧档：这两处只决定"能不能拿它去比正文/能不能按
    品牌收窄"，判错的代价只是少走一路；而收紧档会连同 G740/S25 这类无连字符型号一起否掉。
    """
    return any(char.isdigit() for char in str(name or ""))


def brand_group_of(text: object) -> str:
    """文本落在哪个品牌组，不属于任何组返回空串。

    只 casefold、不做去标点：组内词条本身就没有标点与空格，`HUAWEI 平板` 与 `华为平板`
    都能靠子串命中，而去标点会把 `ipad` 这种词条拼进别的词里。
    """
    normalized = str(text or "").casefold()
    if not normalized:
        return ""
    for label, terms in BRAND_GROUPS:
        if any(term and term in normalized for term in terms):
            return label
    return ""


def brand_groups_in_mentions(mentions: list[str]) -> list[str]:
    """提及里"纯品牌 / 品牌+品类"那部分落在哪些品牌组，按出现顺序去重。

    只要有一个提及带了数字就整体不返回：用户写 `iPhone 17` 就是在点某一款，本库只有 18 时
    该走"查无此型"那条判定，而不是拿品牌组替他换成 18。宁可少收窄一次（退成现状的
    不带过滤检索），也不替用户换个型号——方向与拒答闸、M6 校验同一套 fail-open 划分。
    """
    labels: list[str] = []
    for mention in mentions or []:
        text = str(mention or "").strip()
        if not text:
            continue
        if has_identity_token(text):
            return []
        label = brand_group_of(text)
        if label and label not in labels:
            labels.append(label)
    return labels


# ---------------------------------------------------------------------------
# 四、确认闸用的另外三份判据（阶段一：未收录品牌、协议词、数字系列号）
# ---------------------------------------------------------------------------

# 协议/能力版本号词干：`wifi7`、`pps1.2`、`蓝牙 5.3`、`ip68` 这类串与型号形状完全一样，但没有一款产品叫这个名。
# 为什么必须用黑名单而不是相似度/阈值：阶段零实测里，要防的协议词最高分 0.5711（`蓝牙 5.3` → `iPhone 15`），
# 而真变体的最低分是 0.5290——**两个分数带是重叠的**，拍任何一条切线都会同时误杀真变体、误纳协议词。
# 表外写法只是少排一个（退成"当成型号提及"），不会造成错答，所以宁少勿误：
# 短到会撞上真实型号名的词干（`p3`、`bd`、`ik`、`nl`、`pe`、`apt`）一律不进表。
PROTOCOL_STEMS: tuple[str, ...] = (
    "wifi", "bluetooth", "蓝牙", "nfc", "irda", "airplay", "carplay", "dlna",
    "pps", "qc", "afc", "pd", "ufs", "emmc", "ssd", "nvme", "sata", "pcie", "ddr", "lpddr",
    "hdmi", "mipi", "usb", "typec", "otg", "thunderbolt", "lightning",
    "ldac", "aptx", "sbc", "aac", "opus", "lpcm", "dolby", "dts", "hires",
    "gps", "galileo", "glonass", "qzss", "navic", "uwb", "ble", "zigbee", "thread", "matter",
    "esim", "tdma", "cdma", "lte", "volte", "wcdma",
    "ip", "ois", "eis", "srgb", "vrr", "freesync", "gsync", "hdr",
    "ios", "ipados", "macos", "windows", "android", "harmonyos", "miui", "coloros", "originos",
    "emui", "magicos", "linux", "ubuntu", "python", "directx", "opengl", "vulkan", "webgl",
)

# 整个提及就是"词干 + 版本号"才算协议词：子串匹配会把 `A1206`、`iPad 10` 这类真型号一起否掉
# （`ip` 是词干，但 `ipad 10` 里 `ip` 后面接的是字母，正则要的是分隔符或直接跟数字）。
PROTOCOL_TOKEN_PATTERN = re.compile(
    rf"^(?:{_alternation(PROTOCOL_STEMS)})[\s\-_.]*[0-9]+(?:[._:][0-9]+)*[a-z0-9]?$",
    re.IGNORECASE,
)


def is_protocol_or_spec_token(text: object) -> bool:
    """这串是个协议/能力版本号而不是型号吗（整串匹配，不做子串）。

    命中就等于"这句本轮其实没点名任何产品"：确认闸会把它当"没提取到实体"处理
    （单款库直接收窄、多款库先问），而不是拿 `wifi7` 去跟名单做模糊比对。
    光秃秃一个词干（`蓝牙`、`nfc`）也算：那是在问能力，不是在点型号。
    """
    normalized = str(text or "").strip().casefold().replace(" ", "")
    if not normalized:
        return False
    if normalized in PROTOCOL_STEMS:
        return True
    return bool(PROTOCOL_TOKEN_PATTERN.match(normalized))


def digit_runs(text: object) -> list[str]:
    """取出文本里的连续数字串（`iPhone 15 Pro` → ['15']，`DBY-W09` → ['09']）。

    确认闸靠它区分两件完全不同的事：`苹果15` 库里有 iPhone 15/15 Pro 是**同族歧义**（该问），而 `iPhone 17`
    库里只有 18 是**未收录**（绝不能替用户把 17 换成 18——这条老规矩由 R7 立着，本函数把它接到新闸上）。
    """
    return re.findall(r"[0-9]+", str(text or ""))


def brand_groups_of_names(names: object) -> set[str]:
    """本库名单覆盖哪些品牌组（空串丢弃）。未收录品牌前置否决拿它当"本库认识的牌子全集"。"""
    return {label for label in (brand_group_of(name) for name in (names or [])) if label}

