"""检索出口的话题一致性校验（M6）：只问"本轮那个对象到底在不在切片里"。

processor/query_processor/topic_consistency.py

@Description: 送进生成的切片窗口里，可能坐着几条"交叉编码器打得很高、但整篇没提本轮话题实体"
    的近邻块（线上实测：话题是 iPhone，窗口里却是平板手册的存储章节，答案于是写成"该设备
    UFS4.1"）。本模块拿已经带出来的标题/文件名/正文与话题实体做一次纯字符串包含校验，
    把断崖砍在窗口外、但确实提到该实体的切片等量换进来。
@BusinessRule: 只做等量替换、绝不减少条数（"读几条 = 展示几条 = top_k"的承诺不能破，
    而剔除到空还会误触发 R5 的"库里没有"话术——把兜底实现成新的错误结论）；
    窗口内一条都不命中时整体不动（那更可能是话题实体取错了或本库真没有，硬凑等于把猜测
    写进答案，与宁查勿否同向）；联网结果不参与换出（它的标题来自搜索引擎页面，与本库实体名
    本来就对不上）；换入换出后必须重新按分数降序（断崖检测与来源面板的相关度分档都假定降序）；
    不带型号身份的话题词（"苹果手机"这类品牌+品类短语）**只比标题/文件名，不比正文**
    （拿它比正文会把"对标苹果手机"这种竞品文案判成相关、把本型号那些短到没有身份字的
    参数切片判成跑题，方向刚好是反的）。
@WhyNotMilvusFilter: disabled 库没有词表、也就没有 `item_name` 可过滤，这一道出口校验是本库
    唯一还能用的型号隔离手段；required 库带实体过滤零召回而放宽重查时（`entity_filter_relaxed`）
    窗口里的切片同样与用户点名的实体无关，故本模块对两种模式都只用"名字在不在文本里"这一个
    通用信号，不依赖任何 Milvus 新列。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from processor.query_processor.chitchat_gate import normalize_for_match
from tool.logger import logger
from utils.entity_patterns import find_shape_mentions, has_identity_token

# 关掉本道校验的环境变量开关：设成 1/true/yes 时只做日志、不改结果，方便线上做基线对比
# （与 node_rerank 的 RERANK_A_*_DISABLE 同一手法，判定也复用节点那个 _env_disabled）。
GUARD_DISABLE_ENV = "DRIFT_GUARD_DISABLE"

# 判定"切片是否提到该实体"时看取的标识字段：都在检索输出字段白名单里
# （见 retrieval_common.RETRIEVAL_OUTPUT_FIELDS），不引入任何新列。
DOC_IDENTITY_FIELDS: Tuple[str, ...] = ("title", "file_title", "parent_title", "file_name", "item_name")

# 归一化后短于此长度的实体名不参与判断：否则 "Pro"、"1" 这类碎片会把无关块判成"相关"，
# 校验永远不动作（与实体对齐侧 MIN_SUBSTRING_MATCH_LEN、词表探测 MIN_VOCAB_PROBE_KEY_LEN 同源）。
MIN_TOPIC_KEY_LEN = 3
# 话题实体上限：超过这个数量说明这句本来在比好几个对象，"任一命中即相关"已足够，
# 继续收只会让窗口里每一条都算相关而白跑一趟。
MAX_TOPIC_ENTITIES = 3
# 一次最多换出几条：校验是兜底层，不该大幅改写交叉编码器的排序结论。
MAX_TOPIC_GUARD_SWAPS = 3


def topic_entities(state: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    """本轮检索"关于哪个对象"，返回 [(展示名, [归一化匹配键, ...])]；拿不准就返回空列表。

    实体对齐确认的规范名优先（那已经是本库真实存在、且能直接对上千切片的 `item_name`），
    并按本库词表把别名一并展开成匹配键——切片标题常写别名而不写规范名。
    disabled 库（或词表为空、对齐未命中）没有 `item_names`，此时退回从检索词现取实体形状：
    M4 回填与指代回填都会把话题实体拼到检索词头上，那正是本道校验需要的锚。
    口径复用同一个 `find_shape_mentions`，不另写一套正则，否则抽取/拒答/校验三处会各自漂移。
    """
    vocabulary = state.get("entity_vocabulary")
    vocabulary = vocabulary if isinstance(vocabulary, list) else []

    confirmed = _unique_names([str(name) for name in (state.get("item_names") or [])])
    if confirmed:
        return [
            (name, _match_keys(name, vocabulary))
            for name in confirmed[:MAX_TOPIC_ENTITIES]
            if _match_keys(name, vocabulary)
        ]

    text = str(state.get("rewritten_query") or state.get("original_query") or "")
    anchors: List[Tuple[str, List[str]]] = []
    generic: List[str] = []
    for mention in find_shape_mentions(text)[:MAX_TOPIC_ENTITIES]:
        name = mention.strip()
        keys = _match_keys(name, vocabulary)
        if not keys:
            continue
        if not has_identity_token(name):
            generic.append(name)
        anchors.append((name, keys))
    if generic and anchors:
        # 提醒现场：“苹果手机/华为平板”这类只由品牌+品类拼出来的短语不携带型号身份，
        # 它只能在 `_mentions_topic` 里比标题/文件名，不参与比正文（理由见那个函数）。
        logger.info("话题锚仅品牌+品类（%s）：只比标题与文件名，不比正文", "、".join(generic))
    return anchors


def align_window_to_topic(
    state: Dict[str, Any],
    window_docs: List[Dict[str, Any]],
    pool_docs: List[Dict[str, Any]],
    *,
    apply_changes: bool = True,
) -> List[Dict[str, Any]]:
    """把窗口里未提及话题实体的本地切片，换成窗口外提及了它的切片；任何拿不准的情形原样返回。

    :param window_docs: 断崖截断后的最终窗口（送生成与展示来源的就是这一批）
    :param pool_docs: 窗口之外、同一批送精排的候选（按分数降序，越靠前越可靠）
    :param apply_changes: False 时走完整判断但返回原窗口，只留下"本会怎么改"的日志
                          （`DRIFT_GUARD_DISABLE=1` 的对比模式）
    """
    entities = topic_entities(state)
    if not entities or len(window_docs) < 2:
        return list(window_docs)

    on_topic: List[Dict[str, Any]] = []
    off_topic: List[Dict[str, Any]] = []
    for doc in window_docs:
        if _is_off_topic_local_doc(doc) and not _mentions_topic(doc, entities):
            off_topic.append(doc)
        else:
            on_topic.append(doc)

    topic_label = "、".join(name for name, _ in entities)
    if not off_topic:
        # 全部命中：本道校验无事可做，也不能顺手"再收窄一次"（分数序才是精排的结论）
        return list(window_docs)
    if not on_topic:
        # 窗口里一条都没提到话题实体：多半是锚取错了，或本库确实没有这个型号的资料。
        # 宁可不改——把窗口外的低分块整体换进来，等于用一次猜测覆盖掉精排的全部判断。
        logger.info("话题一致性校验未动作：窗口内 %s 条切片均未提及「%s」", len(window_docs), topic_label)
        return list(window_docs)

    candidates = _pick_candidates(window_docs, pool_docs, entities)
    if not candidates:
        logger.info(
            "话题一致性校验未动作：「%s」在窗口外也没有可换入的切片（换出 %s 条只会少证据）",
            topic_label, len(off_topic),
        )
        return list(window_docs)

    swaps = min(len(off_topic), len(candidates), MAX_TOPIC_GUARD_SWAPS)
    dropped = off_topic[-swaps:]
    logger.info(
        "话题一致性校验：%s，%s 条未提及「%s」的切片（分数 %s），换入窗口外命中切片 %s 条",
        "换出" if apply_changes else "本可换出（开关关闭，仅记录）",
        len(dropped), topic_label,
        [round(_score_or_min(doc), 4) for doc in dropped], len(candidates[:swaps]),
    )
    if not apply_changes:
        return list(window_docs)

    dropped_keys = {_doc_key(doc) for doc in dropped}
    merged = [doc for doc in window_docs if _doc_key(doc) not in dropped_keys] + candidates[:swaps]
    # 重新降序：换入的是窗口外的低分块、换出的是窗口内的低分块，混完不再天然单调，
    # 而断崖之后的所有下游（含来源面板的分档展示）都假定"分数从高到低"。
    merged.sort(key=_score_or_min, reverse=True)
    return merged


def _is_off_topic_local_doc(doc: Dict[str, Any]) -> bool:
    """只有本地切片才可能被判定为"跑题"：联网结果的标题与本库实体名对不上是常态。"""
    return str(doc.get("source") or "local") == "local"


def _pick_candidates(
    window_docs: List[Dict[str, Any]],
    pool_docs: List[Dict[str, Any]],
    entities: List[Tuple[str, List[str]]],
) -> List[Dict[str, Any]]:
    """窗口外"确实提到话题实体、且正文可用"的本地候选，保持入参的降序。"""
    in_window = {_doc_key(doc) for doc in window_docs}
    picked: List[Dict[str, Any]] = []
    for doc in pool_docs:
        if not isinstance(doc, dict) or _doc_key(doc) in in_window:
            continue
        if not _is_off_topic_local_doc(doc):
            continue
        if not str(doc.get("content") or "").strip():
            # 空正文换进来对模型没有信息量（`_select_context_docs` 会直接跳过它），
            # 结果是窗口凭空少一条——那就等于本校验做了一次"剔除"，违背等量替换的前提。
            continue
        if _mentions_topic(doc, entities):
            picked.append(doc)
    return picked


def _mentions_topic(doc: Dict[str, Any], entities: List[Tuple[str, List[str]]]) -> bool:
    """切片的任一标识字段或正文（归一化后）含话题实体键即算相关。

    不带型号身份的话题词（“苹果手机”“华为平板”）只比标识字段、**不比正文**：线上实测
    （库 33“手机商品介绍”）那份 iPhone 文档写的是“苹果智能手机”，字面包含关系不成立，
    而它真正该留下的参数切片短到只剩“## 256GB”一个身份字都没有；反倒另一型号的对比文案
    会写“对标苹果手机”。拿这种短语去比正文，结果就是把对的判成跑题、把错的留下。
    品牌词能生效的地方是文件名/标题（“华为平板 C7 用户指南”），那就只看那里。
    """
    fields = _normalized_fields(doc)
    if not fields:
        # 一条可比文本都没有（旧集合、手工构造的裸切片）：不能因此判它"跑题"而换出，
        # 缺证据就放过——与 fail-open 同向。
        return True
    identity_fields = _normalized_fields(doc, with_content=False)
    for name, keys in entities:
        comparable = fields if has_identity_token(name) else identity_fields
        if not comparable:
            # 这一路只允许比身份字段，而这条切片连身份字段都没有：不能判它跑题。
            return True
        if any(key in text for key in keys for text in comparable):
            return True
    return False


def _normalized_fields(doc: Dict[str, Any], *, with_content: bool = True) -> List[str]:
    """逐个字段归一化（而不是拼成一整串再归一）：避免跨字段边界黏出虚假词形。"""
    values = [doc.get(field) for field in DOC_IDENTITY_FIELDS]
    if with_content:
        values.append(doc.get("content"))
    normalized = [normalize_for_match(value) for value in values]
    return [text for text in normalized if text]


def _match_keys(name: object, vocabulary: List[Any]) -> List[str]:
    """实体名的归一化匹配键：自身 + 本库词表里对得上的别名，短于闸门的键一律丢弃。"""
    normalized = normalize_for_match(name)
    keys = {normalized} if len(normalized) >= MIN_TOPIC_KEY_LEN else set()
    for entry in vocabulary:
        if not isinstance(entry, dict):
            continue
        canonical = str(entry.get("name") or "").strip()
        aliases = entry.get("aliases") if isinstance(entry.get("aliases"), list) else []
        spellings = [canonical] + [str(alias) for alias in aliases]
        if normalized not in {normalize_for_match(spelling) for spelling in spellings}:
            continue
        # 命中的是同一个实体：把它的规范名与全部别名都收进来当匹配键
        for spelling in spellings:
            key = normalize_for_match(spelling)
            if len(key) >= MIN_TOPIC_KEY_LEN:
                keys.add(key)
    return sorted(keys)


def _unique_names(raw_names: List[str]) -> List[str]:
    """按归一化去重、保留首次出现的表面形式（HAK180 与 HAK-180 不该被当成两个对象）。"""
    ordered: Dict[str, str] = {}
    for raw in raw_names:
        name = str(raw or "").strip()
        if not name:
            continue
        ordered.setdefault(normalize_for_match(name), name)
    return list(ordered.values())


def _doc_key(doc: Dict[str, Any]) -> tuple:
    """窗口内外去重的标识：优先 chunk_id，拿不到才退回对象身份（同一批 dict 内唯一）。"""
    chunk_id = doc.get("chunk_id")
    return ("chunk", chunk_id) if chunk_id is not None else ("obj", id(doc))


def _score_or_min(doc: Dict[str, Any]) -> float:
    """重排用的分数键：拿不到分数的沉底（None 不能参与比较，会直接抛 TypeError）。"""
    value = doc.get("score")
    return float(value) if isinstance(value, (int, float)) else float("-inf")
