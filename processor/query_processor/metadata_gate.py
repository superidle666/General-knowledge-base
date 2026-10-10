"""库级元数据问题闸：命中就用元数据画像拼出确定答案并短路（R16 的接替者）。

覆盖的是**问知识库本身**而不是问文档内容的那一类句子：有哪些产品、有多少份文档、
本库开了 HyDE 没。这些事实全在 MySQL 的库/文档/实体表里，切片内容里一条都没有，
所以让内容检索去答必然落在手册前言与免责声明上（那次线上复现记在 inventory_gate）。

形态上这是一道**纯规则闸**，不是 agent 也不是工具调用：没有模型决策环节，不写 SQL，
不读库。取数在异步接口层做完（理由见 `backend/app/api/library_facts.py` 的循环绑定死结），
本模块只吃两样东西——本轮原文 + 一个 dict 画像，输出 `(intent, answer)`。

四个槽位靠句式判出来，不靠模型自觉：
- **粒度**：required 且有实体 → 实体清单；否则退到文档清单（disabled 库整段不生效，见下）
- **范围**：会话已绑死 library_id，永远是"本库"，没有跨库问题
- **品牌过滤**：句子里的纯品牌词当成过滤槽，在内存清单上筛（"有哪些华为的产品"能真答出那几款）
- **时间窗/指标缺失**：**不反问**，一次把几项计数都给出来并声明"未限定时间范围"，让用户自取

生效范围与旧 R16 逐字重合（`_entity_alignment_available`：required 且词表非空）。
disabled 库本阶段整段不启用——它没有实体可列，能答的只有文档清单，而那是独立一轮设计；
沿用同一条判据的收益是老闸不生效的地方新闸也不生效，零回归可证。

@FilePath: processor/query_processor/metadata_gate.py
"""

from __future__ import annotations

import re

from processor.query_processor.chitchat_gate import normalize_for_match
from processor.query_processor.intents import (
    INTENT_METADATA_CONFIG,
    INTENT_METADATA_DOCUMENT,
    INTENT_METADATA_ENTITY,
    INTENT_METADATA_STATS,
    INTENT_METADATA_UNSUPPORTED,
)
from processor.query_processor.inventory_gate import (
    INVENTORY_OBJECT,
    LIBRARY_SCOPE,
    is_library_inventory_question,
)
from utils.entity_patterns import brand_group_of

# 一次最多列出几项。与 `DEFAULT_ENTITY_CLARIFY_CANDIDATE_CAP` 同值但是**独立常量**：
# 那个是"反问候选"的上界、可以按 env 调，这个是"清单答案"的排版上界，两者改动的理由不同，
# 复用一个 env 会让调反问宽度时顺带改了答案排版（没人会想到去看的那一侧）。
MAX_LISTED_ITEMS = 8
# 品牌没命中时顺带列几项本库实有的，让"未收录"这句不至于把天聊死。
BRAND_MISS_LISTED_HINT = 3

# ---- 话术模板（答案就是拼出来的，不经大模型，所以措辞在这里定死）----
METADATA_ENTITY_INVENTORY_TEMPLATE = (
    "截至 {as_of}，本库共收录 {count} 个产品实体：{names}。{overflow}需要哪一款的资料，直接报出型号或产品名。"
)
METADATA_BRAND_ENTITY_INVENTORY_TEMPLATE = (
    "截至 {as_of}，本库收录的 {brand} 产品共 {count} 个：{names}。{overflow}需要哪一款的资料，直接报出型号或产品名。"
)
METADATA_DOCUMENT_INVENTORY_TEMPLATE = (
    "截至 {as_of}，本库共有 {count} 份文档（已就绪 {ready} 份）：{names}。{overflow}需要哪一份的内容，直接报文档名或型号。"
)
METADATA_STATS_TEMPLATE = (
    "截至 {as_of}，本库共有 {total} 份文档（已就绪 {ready} 份、处理中或待确认 {pending} 份、导入失败 {failed} 份），"
    "就绪文档共 {chunks} 个切片{entity_part}。以上为未限定时间范围的全库统计。"
)
METADATA_CONFIG_TEMPLATE = (
    "截至 {as_of}，本库「{name}」当前配置：实体识别={entity_mode}，实体确认={confirm_mode}，"
    "增强检索（HyDE）={hyde_mode}，可见范围={visibility}；共 {total} 份文档（已就绪 {ready} 份）。"
)
METADATA_BRAND_MISS_TEMPLATE = (
    "截至 {as_of}，本库未收录 {brand} 品牌的产品。当前收录了：{names}。需要哪一款，直接报出型号或产品名。"
)
METADATA_EMPTY_LIBRARY_REPLY = "截至 {as_of}，本库还没有可列出的已就绪文档，请稍后再问或先在管理端导入资料。"
# 画像装了但清单被上界截断时补的一句：宁可说"只是前 N 项"，也不能把列出数说成总数。
METADATA_TRUNCATED_SUFFIX = "（清单已达本次统计上界，完整清单请到管理端文档列表查看）"
METADATA_OVERFLOW_TEMPLATE = "（另有 {rest} 项未列出，请直接报名字）"
# T3：问的是库外商业事实。一句顶死、绝不回落内容检索——回落的后果是模型从手册前言里
# 编出"请访问官网/联系经销商"这类零来源承诺，那正是本闸要治的形态。
METADATA_UNSUPPORTED_REPLY = "价格、库存、发货与客户信息不在本知识库的收录范围内，我无法查询，也不根据文档内容推测。"
# 画像读失败时的降级句。**绝不能退成"库里没有"**：那是把系统故障说成事实否定，
# 与"故障≠零命中"（R5 之后立的这条）是同一个语义槽位要求。
METADATA_UNAVAILABLE_REPLY = "本次没能读到知识库的统计信息（数据库暂时不可用），请稍后重试；这不是“库里没有这些内容”。"

# 枚举型疑问词。与 inventory_gate 的 INVENTORY_ASK 差一个子集：那里的"多少种/多少款"
# 算清单，这里只认"有哪些/列出/清单"——计数问法要落到 stats 口径，回答里给数字而不报名字。
_ENUMERATE_ASK = r"(?:都?有哪些|都?有什么|都有什么|都有些|收录了?哪些|收录了?什么|列出|列举|清单|目录)"
# 计数型疑问骨架：量词表刻意窄，只收文档级量词，避免把"这个电池有多少个电芯"吃进来。
_COUNT_ASK = r"(?:多少|有几)(?:份|个|条|种|款|页|篇|张|字)"
# 只可能属于"库"的宾语。刻意不含 产品/机型/型号：那些词在文档内容里同样出现
# （"这款机型有几个摄像头"），只有库级名词出现才能确定问的是库本身。
_LIBRARY_ONLY_OBJECT = r"(?:文档|资料|文件|切片|分段|实体|篇数|规模|体量)"
# 不带量词的写法（线上实测："库里有多少文档"）。量词缺席时只能靠宾语表态，所以这条骨架
# 必须与范围词绑在一起用（见 _STATS_RES 最后一条）：单独拿它判句，"这块电池有多少电芯"
# 这类内容问句就会被顶掉——那是误拒答，代价比漏判高。
_COUNT_BARE_ASK = rf"(?:多少|有几)(?:{_LIBRARY_ONLY_OBJECT}|{INVENTORY_OBJECT})"
# 配置类疑问词。
_CONFIG_ASK = r"(?:配置|设置|模式|开没开|有没有开|开了吗|是否开启|启用了?|关了吗|可见范围|可见性|实体识别|确认模式|hyde)"

_ENUMERATE_RE = re.compile(_ENUMERATE_ASK)
_COUNT_RE = re.compile(_COUNT_ASK)
_COUNT_BARE_RE = re.compile(_COUNT_BARE_ASK)
_DOC_OBJECT_RE = re.compile(r"(?:文档|资料|文件|篇)")

# 计数/配置型的两种语序都要覆盖：范围词在前（"库里有多少文档"）与在后（"有多少份文档在库里"）。
# 范围词是硬前提——没有它，"有多少个"这类骨架与内容问句无法区分，误判的代价是答非所问。
_STATS_RES = (
    re.compile(rf"{LIBRARY_SCOPE}.{{0,12}}?{_COUNT_ASK}"),
    re.compile(rf"{_COUNT_ASK}.{{0,6}}?(?:{_LIBRARY_ONLY_OBJECT}|{INVENTORY_OBJECT})"),
    re.compile(rf"{LIBRARY_SCOPE}.{{0,12}}?(?:统计|规模|体量)"),
    # 省掉量词的写法："库里有多少文档"、"知识库里有多少切片"。范围词在这里同样是硬前提。
    re.compile(rf"{LIBRARY_SCOPE}.{{0,12}}?{_COUNT_BARE_ASK}"),
)
_CONFIG_RES = (
    re.compile(rf"{LIBRARY_SCOPE}.{{0,12}}?{_CONFIG_ASK}", re.IGNORECASE),
    re.compile(rf"{_CONFIG_ASK}.{{0,12}}?{LIBRARY_SCOPE}", re.IGNORECASE),
)

# 库外商业事实（T3）。词表刻意取"商业交易"这一族而不是宽泛的"多少钱/保修"：
# 产品文档里**真有**保修期、售价表、服务网点，那些是该走检索的内容问题；
# 只有句子明确在问一单交易（库存/发货/订单/客户名单/这台机器还在不在保）才顶死。
# “售价/价格”必须带上"多少/几"才算在问价：光“售价表”三个字是文档里的一张表，
# 问的是内容位置（线上实测："这份资料的售价表在哪一页"），顶死它就是误拒答。
_UNSUPPORTED_RES = (
    re.compile(r"(?:多少钱|什么价|几个钱|怎么卖|贵不贵|报价|优惠|打折|降价|涨价|付款方式)"),
    re.compile(r"(?:售价|价格|价钱)(?:是|为)?(?:多少|几|怎么|如何)"),
    re.compile(r"(?:库存|现货|有没有货|有货吗|补货|缺货|备货)"),
    re.compile(r"(?:发货|寄到|物流|快递|几天能到|什么时候到货)"),
    re.compile(r"(?:下单|购买链接|订单|退货|退款|发票)"),
    re.compile(r"(?:客户(?:名单|信息|电话|资料)|联系人|联系方式|销售电话|业务员)"),
    re.compile(r"(?:保修状态|在(?:鲜|保)吗|过保(?:了吗|没)|这台(?:的)?保修)"),
    re.compile(r"(?:序列号|sn\s*码?(?:查询|状态)|串号查询)"),
)

# 库里统计口径的可读名：直接把 required/disabled 抛给用户是工程师视角，
# 一句"强制（问题必须对上具体产品才检索）"才是他能拿去决策的信息。
_ENTITY_MODE_LABEL = {"required": "强制（问题必须对上具体产品才检索）", "disabled": "关闭"}
_CONFIRM_MODE_LABEL = {"strict": "严格（认不出具体型号先问一句）", "lenient": "宽松（只筛选不反问）"}
_VISIBILITY_LABEL = {"private": "私有", "team": "团队", "public": "公开"}


def is_metadata_question(text: object) -> bool:
    """这句是在问"库本身"的事实（清单/计数/配置）吗？只看句式，不含实体护栏。

    接口层预探测与图内判定**共用这一个函数**：两处各写一套判据的话，会出现
    "接口没装载画像、图里却判成元问题"的组合，那正好落进降级话术——用户看见的是
    一句本可以不出现的错误。护栏（点名了具体型号/本库实体名就不算元问题）留给调用方
    结合词表判断，本函数保持纯文本。
    """
    normalized = normalize_for_match(text)
    if not normalized:
        return False
    return (
        is_library_inventory_question(normalized)
        or _is_stats_question(normalized)
        or _is_config_question(normalized)
    )


def is_unsupported_metadata_question(text: object) -> bool:
    """这句在要库外的商业事实（价格/库存/发货/客户信息）吗？命中就一句顶死，不进检索。"""
    normalized = normalize_for_match(text)
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _UNSUPPORTED_RES)


def _is_stats_question(normalized: str) -> bool:
    return any(pattern.search(normalized) for pattern in _STATS_RES)


def _is_config_question(normalized: str) -> bool:
    return bool(normalized) and any(pattern.search(normalized) for pattern in _CONFIG_RES)


def brand_filter_of(text: object) -> str:
    """句子里的**纯品牌词**（当作过滤槽时用），没有品牌或带了数字就返回空串。

    有数字说明用户在点具体型号（"华为 mate60 有哪些"），那时候该走实体对齐而不是品牌过滤；
    与 `brand_groups_in_mentions` 里"带数字就不收窄"是同一条规矩，只是这里直接看整句。
    """
    normalized = normalize_for_match(text)
    if not normalized or any(char.isdigit() for char in normalized):
        return ""
    return brand_group_of(normalized)


def resolve_metadata_intent(text: object, facts: dict | None) -> str:
    """判出这句该用哪个元数据口径回答（返回 intent 常量），判不出返回空串。

    顺序是 配置 → 枚举清单 → 计数 → 兜底清单：配置词的信号最强（"开了吗"不会与内容问句
    同形），其次"有哪些"要报名字，最后"多少"只报数字。实体口径在画像里没有可用实体时
    自动退到文档口径，不另设分支——退了之后答案仍然真实，只是粒度变粗。
    """
    normalized = normalize_for_match(text)
    if not normalized or not is_metadata_question(normalized):
        return ""
    if _is_config_question(normalized):
        return INTENT_METADATA_CONFIG
    if _ENUMERATE_RE.search(normalized):
        return _inventory_intent(normalized, facts)
    if _COUNT_RE.search(normalized):
        return INTENT_METADATA_STATS
    if _COUNT_BARE_RE.search(normalized):
        # 不带量词的计数（"库里有多少文档"）：问的是数字而不是名字，不能落到清单口径。
        return INTENT_METADATA_STATS
    return _inventory_intent(normalized, facts)


def _inventory_intent(normalized: str, facts: dict | None) -> str:
    """清单型按宾语分流：明说"文档/资料"就走文档口径，其余走实体口径。

    实体口径在画像里真的没实体时自动退到文档口径：退了之后答案仍然真实，只是粒度变粗，
    比回一句"本库没有产品"好（本阶段只开 required 库，空壳库本来就进不到这里）。
    """
    if _DOC_OBJECT_RE.search(normalized):
        return INTENT_METADATA_DOCUMENT
    profile = facts if isinstance(facts, dict) else {}
    if int(profile.get("entity_count") or 0) <= 0:
        return INTENT_METADATA_DOCUMENT
    return INTENT_METADATA_ENTITY


def _listed(names: list[str], total: int) -> tuple[str, str]:
    """把清单切成"前若干项"与"还剩多少"两段，供模板填空。"""
    head = names[:MAX_LISTED_ITEMS]
    joined = "、".join(head)
    rest = max(0, total - len(head))
    overflow = METADATA_OVERFLOW_TEMPLATE.format(rest=rest) if rest > 0 else ""
    return joined, overflow


def _entity_names(facts: dict, brand: str = "") -> list[str]:
    names: list[str] = []
    for entry in facts.get("entities") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        if brand and brand_group_of(name) != brand:
            continue
        names.append(name)
    return names


def _document_titles(facts: dict, brand: str = "") -> list[str]:
    titles: list[str] = []
    for entry in facts.get("documents") or []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        if brand and brand_group_of(title) != brand:
            continue
        titles.append(title)
    return titles


def _as_of(facts: dict) -> str:
    return str(facts.get("as_of") or "本次统计")


def render_metadata_answer(intent: str, text: object, facts: dict) -> str:
    """按 intent 把画像填成答案句子。只读 dict，不查库、不调模型。

    每个渲染分支都必须落在**画像里真有的数**上：总数用 SQL 聚合值（`entity_count`），
    列出数受 `MAX_LISTED_ITEMS` 截断、受 `MAX_FACTS_ENTITIES` 截断，两处截断各有一句
    补话，绝不让"列了 8 项"被读成"一共 8 项"。
    """
    if intent == INTENT_METADATA_UNSUPPORTED:
        return METADATA_UNSUPPORTED_REPLY
    if not isinstance(facts, dict) or not facts.get("loaded"):
        return METADATA_UNAVAILABLE_REPLY

    brand = brand_filter_of(text)
    if intent == INTENT_METADATA_CONFIG:
        return _render_config(facts)
    if intent == INTENT_METADATA_STATS:
        return _render_stats(facts)
    if intent == INTENT_METADATA_DOCUMENT:
        return _render_documents(facts, brand)
    return _render_entities(facts, brand)


def _render_entities(facts: dict, brand: str) -> str:
    names = _entity_names(facts, brand)
    if not names:
        # 品牌过滤后为空：直说未收录这个牌子，并顺带列几项本库实有的——
        # 与确认闸的 UNLISTED_BRAND 同一口径，不把天聊死，也不把用户写的牌子换成另一个。
        if brand:
            known = _entity_names(facts)
            if known:
                return METADATA_BRAND_MISS_TEMPLATE.format(
                    as_of=_as_of(facts), brand=brand, names="、".join(known[:BRAND_MISS_LISTED_HINT])
                )
        return _render_documents(facts, brand)
    total = len(names) if brand else int(facts.get("entity_count") or len(names))
    joined, overflow = _listed(names, total)
    truncated = METADATA_TRUNCATED_SUFFIX if facts.get("entities_truncated") and not brand else ""
    template = METADATA_BRAND_ENTITY_INVENTORY_TEMPLATE if brand else METADATA_ENTITY_INVENTORY_TEMPLATE
    return template.format(as_of=_as_of(facts), brand=brand or "产品", count=total, names=joined,
                           overflow=f"{overflow}{truncated}")


def _render_documents(facts: dict, brand: str) -> str:
    titles = _document_titles(facts, brand)
    if not titles:
        return METADATA_EMPTY_LIBRARY_REPLY.format(as_of=_as_of(facts))
    total = len(titles) if brand else int(facts.get("total_document_count") or len(titles))
    joined, overflow = _listed(titles, total)
    truncated = METADATA_TRUNCATED_SUFFIX if facts.get("documents_truncated") and not brand else ""
    return METADATA_DOCUMENT_INVENTORY_TEMPLATE.format(
        as_of=_as_of(facts), count=total, ready=int(facts.get("ready_document_count") or 0),
        names=joined, overflow=f"{overflow}{truncated}",
    )


def _render_stats(facts: dict) -> str:
    entity_part = ""
    entity_count = int(facts.get("entity_count") or 0)
    if entity_count > 0:
        entity_part = f"、{entity_count} 个产品实体"
    return METADATA_STATS_TEMPLATE.format(
        as_of=_as_of(facts),
        total=int(facts.get("total_document_count") or 0),
        ready=int(facts.get("ready_document_count") or 0),
        pending=int(facts.get("pending_document_count") or 0),
        failed=int(facts.get("failed_document_count") or 0),
        chunks=int(facts.get("chunk_total") or 0),
        entity_part=entity_part,
    )


def _render_config(facts: dict) -> str:
    mode = str(facts.get("entity_recognition_mode") or "disabled")
    confirm = str(facts.get("entity_confirm_mode") or "strict")
    visibility = str(facts.get("visibility") or "")
    return METADATA_CONFIG_TEMPLATE.format(
        as_of=_as_of(facts),
        name=str(facts.get("library_name") or "当前") or "当前",
        entity_mode=_ENTITY_MODE_LABEL.get(mode, mode),
        # disabled 库根本没有确认闸，报"严格"会被当成一句假话；这一档只在 required 下有意义。
        confirm_mode=(_CONFIRM_MODE_LABEL.get(confirm, confirm) if mode == "required" else "不适用"),
        hyde_mode="已开启" if facts.get("use_hyde") else "未开启",
        visibility=_VISIBILITY_LABEL.get(visibility, visibility or "未设置"),
        total=int(facts.get("total_document_count") or 0),
        ready=int(facts.get("ready_document_count") or 0),
    )
