"""实体形状识别归一（P2-8）的守卫。

分三层：
1. 公共模块本身：两档判定的边界（宽松档要能认单字母开头/无连字符型号，收紧档不能把
   windows10、wifi6 这类版本号当成"用户点名的型号"）、品牌无关的品类短语、词表不变量。
2. 消费方接线：查询侧两个文件与导入侧文件名 token 都必须取自 utils.entity_patterns，
   不得再各自 re.compile 一套型号形状（否则又回到"三套正则互不一致"的起点）。
3. 端到端：归一后实体对齐链路的两个关键取舍——中文紧邻的型号代码现在认得出（会拒答），
   形状同式的版本号认得出但不拒答（宽松档退 best-effort，严格档的单款库会直接收窄）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from graph_introspection import PROJECT_ROOT, imported_names

from processor.import_processor.nodes.node_entity_recognition_required import (
    NodeEntityRecognitionRequired,
)
from processor.query_processor.chitchat_gate import contains_entity_signal, detect_chitchat
from processor.query_processor.nodes.node_prepare_query import NodePrepareQuery
from utils import entity_patterns
from utils.entity_patterns import (
    BRAND_GROUPS,
    BRAND_TERMS,
    CATEGORY_TERMS,
    ENTITY_SIGNAL_TERMS,
    FILENAME_MODEL_TOKEN,
    MODEL_CODE_PATTERN,
    PRODUCT_MENTION_PATTERN,
    SPEC_TERMS,
    brand_group_of,
    brand_groups_in_mentions,
    find_shape_mentions,
    has_identity_token,
    is_specific_model_code,
    looks_like_model,
)

PATTERNS_PATH = PROJECT_ROOT / "utils" / "entity_patterns.py"
PREPARE_QUERY_PATH = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_prepare_query.py"
CHITCHAT_PATH = PROJECT_ROOT / "processor" / "query_processor" / "chitchat_gate.py"
ENTITY_NODE_PATH = (
    PROJECT_ROOT / "processor" / "import_processor" / "nodes" / "node_entity_recognition_required.py"
)
TOPIC_GUARD_PATH = PROJECT_ROOT / "processor" / "query_processor" / "topic_consistency.py"

# 型号形状的"指纹"：自己重写一份型号正则必然要枚举字母/数字字符类（[A-Z0-9]、[A-Za-z] 等）。
# 只查"[A-Za-z" 会被 `[A-Z][A-Z0-9]` 这种写法绕过去，所以三个字母区间开头都算。
MODEL_SHAPE_FINGERPRINTS = ("[A-", "A-Z", "a-z")


def _compiled_patterns(path: Path) -> list[str]:
    """取文件里所有 `re.compile("...")` 的模式串（含元组里的多条），用于静态形状检查。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    patterns: list[str] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "compile"
            or not node.args
        ):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            patterns.append(first.value)
    return patterns


def _call_names(path: Path) -> list[str]:
    """文件里被调用的裸函数名列表（用于统计某个公共函数被复用了几个调用点）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]


# --------------------------------------------------------------------------
# 一、宽松档：型号形状
# --------------------------------------------------------------------------

def test_loose_tier_covers_single_letter_and_hyphenless_models() -> None:
    # 方案点名的两类形状：单字母开头（X-200）、无连字符（G740 / S25），旧 MODEL_PATTERN 都不认
    for text in ("X-200", "G740", "S25", "HAK180", "FX8P", "hak-180"):
        assert looks_like_model(text), f"宽松档漏认 {text}"
    assert "G740" in find_shape_mentions("我想知道G740的价格")


def test_cjk_adjacent_model_code_is_recognized() -> None:
    r"""\b 的替代写法必须生效：Python 把中日韩也算 word char，旧
    `\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)+\b` 在中文紧邻处两侧都取不到边界，整串型号直接漏掉。"""
    assert find_shape_mentions("问下APL-005多少钱") == ["APL-005"]
    assert find_shape_mentions("型号是HAK180的") == ["HAK180"]
    assert looks_like_model("APL-005，报错") is True


def test_ascii_adjacent_run_is_taken_whole() -> None:
    # 前后析观只排除 ASCII 字母数字：左侧连着字母时不会从中间切出半个 token
    assert find_shape_mentions("XHAK-180") == ["XHAK-180"]


def test_pure_digit_or_versionless_word_is_not_a_model() -> None:
    assert looks_like_model("2026 年") is False
    assert looks_like_model("你好") is False
    assert find_shape_mentions("怎么连接电源适配器") == []


# --------------------------------------------------------------------------
# 二、收紧档：拒答判定必须比抽取保守
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["APL-005", "HAK-999", "DBY-W09", "LA2608-A", "X-200"])
def test_refuse_gate_accepts_hyphenated_model_codes(text: str) -> None:
    assert is_specific_model_code(text) is True


@pytest.mark.parametrize("text", ["G740", "S25", "wifi6", "wifi-6", "P-1", "windows10", "ios17", "C7", "E12", "一加", "", None])
def test_refuse_gate_rejects_shapes_that_could_be_versions(text: object) -> None:
    """无连字符形状与版本号同形，错拒是用户可见硬错误，漏拒只退 best-effort——宁可漏。"""
    assert is_specific_model_code(text) is False


def test_two_tiers_are_deliberately_different() -> None:
    # 两档若被合并成一套，这条就会红：宽松档认得的形状明显多于拒答档
    loose = [t for t in ("G740", "S25", "wifi6", "windows10", "C7") if looks_like_model(t)]
    refused = [t for t in loose if is_specific_model_code(t)]
    assert loose and not refused


def test_filename_token_is_stricter_than_query_code_pattern() -> None:
    # 导入侧命中就把整个文件名当主实体先验，故要求 ≥2 字母前缀；查询侧宽松档不挑
    assert MODEL_CODE_PATTERN.search("a-b")
    assert not FILENAME_MODEL_TOKEN.search("a-b")
    assert FILENAME_MODEL_TOKEN.search("HAK-180")


def test_filename_model_prior_still_works_through_shared_token() -> None:
    assert NodeEntityRecognitionRequired._filename_model("HAK-180 用户指南.pdf") == "HAK-180 用户指南"
    assert NodeEntityRecognitionRequired._filename_model("售后服务政策") == ""


# --------------------------------------------------------------------------
# 三、品牌无关的品类短语与共享词表
# --------------------------------------------------------------------------

def test_product_mention_no_longer_hardcoded_to_one_brand() -> None:
    assert "小米平板" in find_shape_mentions("小米平板怎么投屏")
    assert "华为电脑" in find_shape_mentions("华为电脑怎么进bios")  # 旧行为保持
    assert "一加手机" in find_shape_mentions("看看一加手机")


def test_bare_category_word_without_brand_is_not_an_entity_mention():
    # 去掉品牌限定不等于"任何品类词都算实体"：那样"设备"这种泛词会污染提及候选
    assert find_shape_mentions("设备怎么开机") == []
    assert find_shape_mentions("打印机卡纸") == []


def test_two_products_split_into_two_mentions() -> None:
    # 间隔量词必须是懒惰的，否则整串被吞成一个词表必然匹配不上的长提及
    mentions = find_shape_mentions("苹果手机和华为手机哪个更好")
    assert mentions == ["苹果手机", "华为手机"]


def test_brand_part_of_a_mention_matches_any_case() -> None:
    # 词表里的拉丁品牌名是按小写存的，能匹上大写全靠组合式带的 IGNORECASE
    assert "HUAWEI 平板" in find_shape_mentions("HUAWEI 平板怎么截图")
    assert "Huawei电脑" in find_shape_mentions("Huawei电脑风扇噪音大")


def test_mention_pattern_alternations_come_from_shared_tables() -> None:
    """品类短语正则只能由词表拼出。若有人回去手写字面分支，词表加品牌就又不生效了。"""
    source = PRODUCT_MENTION_PATTERN.pattern
    assert all(term in source for term in BRAND_TERMS), "有品牌词没进组合式，正则可能被另写了一份"
    assert all(term in source for term in CATEGORY_TERMS), "有品类词没进组合式"
    assert "参数" not in source and "型号" not in source  # 规格词只进信号表，拼不出实体短语


def test_signal_terms_are_already_in_matchable_form() -> None:
    """消费方是在"去标点 + 转小写"后的文本上做子串匹配的：写成 "Huawei" 会静默失配。"""
    assert ENTITY_SIGNAL_TERMS == BRAND_TERMS + CATEGORY_TERMS + SPEC_TERMS
    for term in ENTITY_SIGNAL_TERMS:
        assert term == term.strip().casefold(), f"词条 {term!r} 未归一，子串匹配永远命中不了"
    for term in ("烫金机", "型号", "苹果", "路由器"):
        assert term in ENTITY_SIGNAL_TERMS


# --------------------------------------------------------------------------
# 四、消费方接线（静态守卫）
# --------------------------------------------------------------------------

def test_shared_module_is_the_one_being_loaded() -> None:
    """AST 读的文件必须就是进程导入的那份，否则"已经删掉了"的断言读的是另一份文件。"""
    assert Path(entity_patterns.__file__).resolve() == PATTERNS_PATH.resolve()


@pytest.mark.parametrize(
    ("path", "module_name", "expected_names"),
    [
        (PREPARE_QUERY_PATH, "entity_patterns", {"find_shape_mentions", "is_specific_model_code"}),
        (CHITCHAT_PATH, "entity_patterns", {"ENTITY_SIGNAL_TERMS", "looks_like_model"}),
        (ENTITY_NODE_PATH, "entity_patterns", {"FILENAME_MODEL_TOKEN"}),
    ],
)
def test_consumers_take_shapes_from_the_shared_module(
        path: Path, module_name: str, expected_names: set[str]) -> None:
    assert expected_names <= imported_names(path, module_name), f"{path.name} 没有复用公共模块"


@pytest.mark.parametrize("path", [PREPARE_QUERY_PATH, CHITCHAT_PATH])
def test_query_side_defines_no_model_shape_of_its_own(path: Path) -> None:
    leftover = [
        p for p in _compiled_patterns(path)
        if any(fingerprint in p for fingerprint in MODEL_SHAPE_FINGERPRINTS)
    ]
    assert not leftover, f"{path.name} 又自带了一套型号正则，形状只能来自 utils.entity_patterns：{leftover}"


def test_old_local_constants_are_gone() -> None:
    """旧名字必须真的没了（AST 定义 + 运行时属性双锁），留着会让改动看起来"两套并存"。"""

    def _assigned_names(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(t.id for t in targets if isinstance(t, ast.Name))
        return names

    assigned = _assigned_names(PREPARE_QUERY_PATH)
    assert not assigned & {"MODEL_PATTERN", "PRODUCT_PATTERN"}
    assert not hasattr(NodePrepareQuery, "MODEL_PATTERN")
    gate_assigned = _assigned_names(CHITCHAT_PATH)
    assert not gate_assigned & {"_MODEL_CODE_RE", "_MODEL_SLUG_RE", "_BRAND_TERMS"}


def test_shape_extraction_is_reused_instead_of_duplicated() -> None:
    """所有“扫形状抽实体”的地方都只能调同一个公共函数；调用点数变了就得逐个核实。

    现行七处：① `_extract_mentions` 的正则通道；② `_rewrite_query_with_history` 从历史借型号；
    ③ `_inventory_gate` 的反护栏（R16：本轮提到了任何实体形状就不当清单问题拒）；
    ④ `_backfill_ellipsis_anchor` 的前置闸①（检索词里已有实体就不叠加话题锚）；
    ⑤ `_topic_entity_from_history` 从【用户】历史里取话题锚（M4）；
    ⑥ `_metadata_gate` 的反护栏（接替 R16 的那道闸：点了具体型号就不当库级元问题顶掉）；
    ⑦ `_turn_names_specific_product`（T3 的出口：写了型号的价格问句可能是真内容问题，放行去检索）。
    ④⑤ 都是“同一段文本换个用途”，口径必须与抽取/拒答那两路一致，所以同样走公共函数；
    ⑥⑦ 与 ③ 同理——同一句“用户到底有没有给出具体对象”的判据不能在新闸里再写一遍。
    required 库还多一层词表优先（`_vocab_entities_in`），那是正则抽不到的纯中文实体名的补充，
    不是另写一套形状。
    坚持等于而不是“至少”：新增调用点必须说明它复用的是同一口径，而不是在别处又写了一套正则
    （自写一套正则由上一条“文件不得自带型号正则”的守卫兜住）。
    """
    assert _call_names(PREPARE_QUERY_PATH).count("find_shape_mentions") == 7


# --------------------------------------------------------------------------
# 五、端到端：对齐链路与闲聊护栏
# --------------------------------------------------------------------------

@pytest.fixture()
def node(monkeypatch):
    """把联网的 LLM 抽取缝打桩为空，只用正则 + 确定性对齐验证分支。"""
    prepared = NodePrepareQuery()
    monkeypatch.setattr(prepared, "_call_entity_llm", lambda text, history_text: [])
    return prepared


def _required_state(query: str, vocabulary: list[dict], confirm_mode: str = "strict") -> dict:
    return {
        "original_query": query,
        "library_id": 7,
        "entity_recognition_mode": "required",
        # 显式写下确认档：以前这里不传键、隐式吃代码默认 strict，导致同一个用例名下的
        # “版本号不拒答”在两个档里其实是两种结局，改档时静默漂移。
        "entity_confirm_mode": confirm_mode,
        "entity_vocabulary": vocabulary,
    }


def test_cjk_adjacent_unknown_model_now_refuses(node) -> None:
    """归一的真正收益落点：中文紧邻点名的型号不再因为 \b 取不到边界而漏判，查无即拒答。"""
    state = node.process(_required_state("问下APL-005多少钱", [{"name": "HAK-180", "aliases": []}]))
    assert state["item_names"] == []
    assert "未收录" in (state.get("answer") or "")


def test_version_like_mention_stays_best_effort(node) -> None:
    """版本号形状的词不算“用户点了型号”：宽松档不猜、也不拒答，退回不带过滤检索。"""
    for query in ("windows10 怎么设置", "G740 报错怎么解决", "wifi6 连不上"):
        result = node.process(_required_state(query, [{"name": "HAK-180", "aliases": []}], "lenient"))
        assert result["item_names"] == [], query
        assert not result.get("answer"), f"{query} 被误拒答了"


def test_strict_single_product_library_narrows_instead_of_guessing(node) -> None:
    """严格档 + 本库仅一款：被归到“这句没写出型号”的词会收窄到那一款（阶段一确认闸第五档）。

    与上一条的旧预期相冲突——库里就一个对象时，反问比直接收窄更难受，这是阶段一定下的设计，
    本轮不改源码，只把现状钉住；“到底该不该收窄”拿 `eval/exit_probe.py` 的出口分布定夺。
    `G740` 是另一种命运：它长得像型号代码，算“提到了但没命中”（拿不到那款唯一实体），
    收窄只发生在“根本没写出型号”那一侧——两档的分工全在这个差异里，所以一并钉住。
    """
    vocabulary = [{"name": "HAK-180", "aliases": []}]

    narrowed = node.process(_required_state("windows10 怎么设置", vocabulary))
    assert narrowed["item_names"] == ["HAK-180"]
    assert narrowed["entity_align_exit"] == "single_product_narrowed"
    assert not narrowed.get("answer"), "收窄不是拒答"

    bare = node.process(_required_state("G740 报错怎么解决", vocabulary))
    assert bare["item_names"] == []
    assert bare["entity_align_exit"] == "no_brand_group"


def test_chitchat_guard_uses_the_loose_tier() -> None:
    # 护栏方向与拒答相反：多认一个型号只是多走一次检索，所以宽松档
    assert contains_entity_signal("我想知道G740的价格") is True
    assert contains_entity_signal("问下APL-005多少钱") is True
    assert contains_entity_signal("你好") is False
    # 纯型号护栏放行后，模型身份问法仍要走 model_probe 通道，不能被词表挡掉
    assert detect_chitchat("你是什么模型", l1_only=True) == "model_probe"


def test_mention_dedup_is_case_insensitive_and_keeps_first_surface() -> None:
    assert find_shape_mentions("HAK-180 hak-180 HAK-180") == ["HAK-180"]
    # 型号代码排在品类短语之前：改写补实体时更具体的形状优先进入检索文本
    assert find_shape_mentions("华为平板 C7 用户指南") == ["C7", "华为平板"]


def test_patterns_module_compiles_without_side_effects() -> None:
    # 公共模块必须能被"只想知道形状"的任何一侧安全导入（不能拉起配置/连接）
    tree = ast.parse(PATTERNS_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported <= {"re", "__future__"}, f"公共常量模块不该依赖运行时组件：{imported}"


# --------------------------------------------------------------------------
# 六、品牌组判定（R7）：只说到品牌时能不能对上本库那一款
# --------------------------------------------------------------------------

def test_brand_terms_flatten_from_a_single_group_table() -> None:
    """BRAND_TERMS 只能由品牌组摊平而来：两处各存一份，加品牌就必然漏一处。"""
    assert BRAND_TERMS == tuple(term for _, terms in BRAND_GROUPS for term in terms)
    assert len({label for label, _ in BRAND_GROUPS}) == len(BRAND_GROUPS), "组名重复会让日志分不清"
    for label, terms in BRAND_GROUPS:
        assert terms, f"品牌组 {label} 是空的"
        # 拉丁词条必须小写无标点：消费方在"去标点 + 转小写"后的文本上做子串匹配
        for term in terms:
            assert term == term.strip().casefold(), f"{label} 的词条 {term!r} 未归一"


@pytest.mark.parametrize(
    ("text", "group"),
    [
        ("苹果手机", "苹果"),
        ("苹果", "苹果"),
        ("iPhone 18 Pro", "苹果"),  # 规范名里没有"苹果"二字，全靠机型词进组
        ("iPad Air", "苹果"),
        ("HUAWEI 平板", "华为"),
        ("Redmi K80", "小米"),
        ("红米手机", "小米"),
        ("一加 Ace 6T", "一加"),
        ("HAK-180", ""),
        ("烫金机", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_brand_group_of_covers_the_model_family_names(text: object, group: str) -> None:
    assert brand_group_of(text) == group


def test_has_identity_token_is_a_shape_rule_not_a_vocabulary_lookup() -> None:
    """带数字才算"点到具体型号"：这条判据同时被 M6（要不要比正文）与 R7（要不要收窄）使用。"""
    assert has_identity_token("iPhone 18 Pro") is True
    assert has_identity_token("HAK180") is True
    assert has_identity_token("APL-005") is True
    assert has_identity_token("苹果手机") is False
    assert has_identity_token("烫金机") is False
    assert has_identity_token(None) is False


def test_brand_groups_in_mentions_dedups_and_keeps_first_order() -> None:
    assert brand_groups_in_mentions(["苹果手机", "苹果平板"]) == ["苹果"]
    assert brand_groups_in_mentions(["华为平板", "苹果手机"]) == ["华为", "苹果"]
    assert brand_groups_in_mentions(["烫金机怎么调温", ""]) == []


def test_a_numbered_mention_blocks_the_whole_brand_group() -> None:
    """用户点名了 iPhone 17 时整体不返回：本库只有 18，收窄就等于替用户换了型号。"""
    assert brand_groups_in_mentions(["苹果手机", "iPhone 17"]) == []
    assert brand_groups_in_mentions(["iPhone 17"]) == []


def test_topic_guard_reuses_the_shared_identity_rule() -> None:
    """M6 那份"带不带型号身份"的判据必须来自公共模块，不能各留一份（P2-8 同一条红线）。"""
    assert "has_identity_token" in imported_names(TOPIC_GUARD_PATH, "entity_patterns")
    tree = ast.parse(TOPIC_GUARD_PATH.read_text(encoding="utf-8"))
    defined = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_has_identity_token" not in defined, "话题校验里又定义了一份同形判据"
