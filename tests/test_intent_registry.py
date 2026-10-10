"""intent 登记表的守卫：写入的字面量必须登记，登记的取值必须真有人写。

intent 是下游（日志、离线统计、接口）按字面量比对取的字段。这类字段最坏的事故不是报错，
是同一个语义出现两种拼法——按字符串写的统计会静默漏掉一半，而代码看起来完全正常。
所以两条断言各钉一半：
1. 源码里所有 `state["intent"] = <字符串>` 的取值 ⊆ ALL_INTENTS（防漏登记）；
2. ALL_INTENTS 里每个值都能被某条真实路径产出（防注册表变成只写不读的坟场）。
"""

from __future__ import annotations

from pathlib import Path

from graph_introspection import PROJECT_ROOT, subscript_literal_writes

import processor.query_processor.intents as intents_module
from processor.query_processor.intents import (
    ALL_INTENTS,
    INTENT_CHITCHAT,
    INTENT_CONVERSATION_ECHO,
    INTENT_INVENTORY_REFUSAL,
    INTENT_META_REWRITE,
    INTENT_METADATA_CONFIG,
    INTENT_METADATA_DOCUMENT,
    INTENT_METADATA_ENTITY,
    INTENT_METADATA_STATS,
    INTENT_METADATA_UNAVAILABLE,
    INTENT_METADATA_UNSUPPORTED,
)
from processor.query_processor.metadata_gate import resolve_metadata_intent

QUERY_PROCESSOR_DIR = PROJECT_ROOT / "processor" / "query_processor"
INTENT_CONSTANTS = {
    name: value
    for name, value in vars(intents_module).items()
    if name.startswith("INTENT_") and isinstance(value, str)
}

# 一份"画像可用"的最小事实：只用来让 resolve 能按口径分派，不参与任何断言内容。
FACTS = {
    "loaded": True,
    "entity_recognition_mode": "required",
    "entity_confirm_mode": "strict",
    "use_hyde": False,
    "visibility": "private",
    "as_of": "2026-10-10 09:30",
    "total_document_count": 3,
    "ready_document_count": 3,
    "failed_document_count": 0,
    "pending_document_count": 0,
    "chunk_total": 90,
    "entity_count": 1,
    "entities": [{"name": "华为平板 C7 用户指南", "aliases": [], "source": "llm",
                  "maybe_series": False, "document_id": 1}],
    "documents": [{"id": 1, "title": "华为平板 C7 用户指南", "status": "ready", "version": 1}],
    "entities_truncated": False,
    "documents_truncated": False,
}


def _scan_paths() -> list[Path]:
    return sorted(QUERY_PROCESSOR_DIR.rglob("*.py"))


def _all_writes() -> set[str]:
    writes = subscript_literal_writes(
        _scan_paths(), "state", "intent", extra_constants=INTENT_CONSTANTS, allow_dynamic=True
    )
    assert writes, "query_processor 里一处 intent 写入都没找到，守卫空过等于没守卫"
    return writes


# --------------------------------------------------------------------------
# 一、登记：写出去的值必须都在表里
# --------------------------------------------------------------------------

def test_registry_lists_every_intent_constant_defined_in_the_module() -> None:
    """intents.py 里定义了 INTENT_* 却没列进 ALL_INTENTS → 下游按表取值会漏掉它。"""
    assert set(INTENT_CONSTANTS.values()) == set(ALL_INTENTS)


def test_written_intent_literals_are_registered() -> None:
    assert _all_writes() - {value for value in _all_writes() if value.startswith("~")} <= set(ALL_INTENTS)


def test_literal_write_points_are_exactly_the_known_short_circuits() -> None:
    """逐个钉住"哪些值是当场写字面量/常量写进去的"。

    新增一条短路却忘了登记，这里会多出一个值；登记了却从别处（比如模型输出）赋值，
    这里会少一个值——两种错位都表现为"分布表少一列"，所以宁可写得比断言更细。
    """
    assert {
        value for value in _all_writes() if not value.startswith("~")
    } == {
        INTENT_CHITCHAT,
        INTENT_INVENTORY_REFUSAL,
        INTENT_CONVERSATION_ECHO,
        INTENT_META_REWRITE,
        INTENT_METADATA_UNSUPPORTED,
        INTENT_METADATA_UNAVAILABLE,
    }


def test_only_one_dynamic_write_point_exists() -> None:
    """动态赋值只允许元数据闸那一条：值是 resolve_metadata_intent 按口径分派出来的。

    出现第二条 `state["intent"] = 变量` 就意味着有人在绕开口径分派自己造值，
    那种写法静态上看不见取值，必须把它顶出来显式登记。
    """
    assert {value for value in _all_writes() if value.startswith("~")} == {"~intent"}


# --------------------------------------------------------------------------
# 二、防死条目：表里每个值都要有真路径能产出
# --------------------------------------------------------------------------

def test_registry_has_no_dead_entries() -> None:
    reachable = {value for value in _all_writes() if not value.startswith("~")}
    # 元数据四个口径不是字面量写入（统一走 `state["intent"] = intent`），由分派函数产出
    for query in (
        "知识库里有哪些产品",  # 实体清单
        "知识库里有哪些文档",  # 文档清单
        "知识库里有多少份文档",  # 计数
        "知识库开了HyDE吗",  # 配置
    ):
        reachable.add(resolve_metadata_intent(query, FACTS))
    assert reachable == set(ALL_INTENTS)


def test_metadata_intents_are_all_reachable_from_the_gate() -> None:
    """"登记了但永远分派不到"的元数据口径＝前端与统计里那一列恒空白，比少登记更难发现。"""
    produced = {
        resolve_metadata_intent(query, FACTS)
        for query in (
            "知识库里有哪些产品",
            "知识库里有哪些文档",
            "知识库里有多少份文档",
            "知识库开了HyDE吗",
            "这个知识库的产品清单",
        )
    }
    assert {
        INTENT_METADATA_ENTITY,
        INTENT_METADATA_DOCUMENT,
        INTENT_METADATA_STATS,
        INTENT_METADATA_CONFIG,
    } <= produced
