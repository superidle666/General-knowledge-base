"""同章节过切碎片合并（R11）与来源溯源接线的行为回归。

原实现把合并放在精排**之后**：合并体沿用的是某一张单片（还被截到 1000 字）的分，正文却
是几倍长，断崖阈值与来源面板的相关度分档都建立在不再代表当前内容的分数上；而合并体只有
代表片一个 chunk_id 能回源，其余几段"进了 prompt 却无法溯源"。

本批把合并前移到送精排之前（分数由交叉编码器直接打在合并内容上），并把 part_chunk_ids /
merged_parts 一路带到接口层。用例只锁这几件事：合并时机、探测文本、代表片选择、候选名额、
不该合并的一律不并、开关可回退、降级路径口径，以及来源结构的向后兼容。
"""

from __future__ import annotations

import ast
from types import SimpleNamespace
from typing import Any

import pytest
from graph_introspection import PROJECT_ROOT

import processor.query_processor.nodes.node_rerank as rerank_node_module
from backend.app.api.query import _hydrate_sources, _local_source_out
from processor.query_processor.nodes.node_answer_output import NodeAnswerOutput
from processor.query_processor.nodes.node_rerank import (
    RERANK_MAX_CONTENT_CHARS,
    RERANK_PROBE_MIN_PER_PART,
    NodeRerank,
)
from utils.reranker_http_utils import RerankError

RERANK_NODE_PATH = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_rerank.py"

BASE = "加热板保养"


def _frag(part: int, rrf_score: float, *, token: str | None = None) -> dict:
    """构造一个 _split_long_section 产出的过切碎片（标题带 “-<序号>” 后缀）。"""
    marker = token or f"P{part}"
    return {
        "chunk_id": f"c{part}",
        "title": f"{BASE}-{part}",
        # 单片正文 403 字：三片合并后 1200+ 字，一定超过送精排的上限，才测得出均摊预算
        "content": f"{marker}：" + "清洁加热板前先断电。" * 40,
        "rrf_score": rrf_score,
        "document_id": 7,
        "chunk_index": part,
        "file_name": "加热板保养.pdf",
    }


def _plain(index: int, *, rrf_score: float = 0.02) -> dict:
    """一个正常章节块（无片段后缀），正文以 “独立块N” 开头便于在送精排的文本里认出它。"""
    return {
        "chunk_id": f"n{index}",
        "title": f"独立章节{index}",
        "content": f"独立块{index}：" + "维护记录。" * 60,
        "rrf_score": rrf_score,
        "document_id": 8,
        "chunk_index": index,
    }


@pytest.fixture
def recorded_rerank(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """拦住精排外部调用：记录送给它的文本，返回值由用例塞进 box（绝不联网）。"""
    box: dict[str, Any] = {"contents": [], "scores": None, "error": None}

    def fake_rerank(_query: str, contents: list[str]) -> list[float]:
        box["contents"] = contents
        if box["error"] is not None:
            raise box["error"]
        return box["scores"] or [0.9 - index * 0.01 for index in range(len(contents))]

    monkeypatch.setattr(rerank_node_module, "rerank_documents", fake_rerank)
    return box


def _merged_of(docs: list[dict]) -> dict:
    """从重排结果里取出那条合并体（标题已被还原成基标题）。"""
    return next(doc for doc in docs if doc.get("title") == BASE)


# --------------------------------------------------------------------------
# 一、合并时机：必须发生在送精排之前
# --------------------------------------------------------------------------

def test_fragments_are_merged_before_the_rerank_request(recorded_rerank) -> None:
    node = NodeRerank()
    state = {
        "original_query": "加热板怎么清洁",
        "top_k": 5,
        "rrf_chunks": [_frag(1, 0.02), _frag(2, 0.03), _frag(3, 0.01), _plain(0)],
        "web_search_docs": [],
    }
    result = node.process(state)

    # 三片 + 一个独立块 → 只送 2 条：精排看到的是合并内容，不是某一片
    assert len(recorded_rerank["contents"]) == 2
    merged = _merged_of(result["reranked_docs"])
    assert merged["rerank_probe"] in recorded_rerank["contents"]
    # 送出去的绝不可能是合并正文本体（那会有 1200+ 字），也只可能不超过上限
    assert merged["content"] not in recorded_rerank["contents"]
    assert all(len(text) <= RERANK_MAX_CONTENT_CHARS for text in recorded_rerank["contents"])


def test_merge_frees_candidate_slots_before_the_cap(recorded_rerank) -> None:
    """合并的另一个动机是"去重占坑"：先合再截，名额才按"几个不同来源"分配。"""
    node = NodeRerank()
    # top_k=1 时本地候选名额是 10 条：三片占满 3 个名额就会挤掉最后一个独立块
    chunks = [_frag(1, 0.03), _frag(2, 0.02), _frag(3, 0.01)] + [_plain(index) for index in range(8)]
    state = {"original_query": "q", "top_k": 1, "rrf_chunks": chunks, "web_search_docs": []}
    node.process(state)

    contents = recorded_rerank["contents"]
    assert len(contents) == 9  # 1 条合并体 + 8 个独立块
    assert sum(1 for text in contents if text.startswith("独立块")) == 8


def test_merged_probe_keeps_every_fragment_head_within_one_budget(recorded_rerank) -> None:
    """均摊预算的证据：只截合并正文头部会完全看不到第 3 片，而第 3 片可能正是最强命中。"""
    node = NodeRerank()
    chunks = [_frag(1, 0.01, token="头一片"), _frag(2, 0.03, token="第二片"), _frag(3, 0.02, token="第三片")]
    state = {"original_query": "q", "top_k": 5, "rrf_chunks": chunks, "web_search_docs": []}
    result = node.process(state)

    probe = _merged_of(result["reranked_docs"])["rerank_probe"]
    for token in ("头一片", "第二片", "第三片"):
        assert token in probe, f"探测文本丢了 {token} 的信号，等于没让精排看到那一片"
    assert len(probe) <= RERANK_MAX_CONTENT_CHARS
    # 每片预算 = max(1000 // 3, 200) = 333，尾部被均摊砍掉：三片原文合计 1200+ 字
    assert probe.count("清洁加热板前先断电") < 120
    assert RERANK_MAX_CONTENT_CHARS // 3 >= RERANK_PROBE_MIN_PER_PART


def test_merged_doc_uses_strongest_fragment_as_representative(recorded_rerank) -> None:
    node = NodeRerank()
    # 故意打乱出现顺序、把最强命中放在中间那片：代表片看 rrf_score，位置看首成员
    chunks = [_frag(3, 0.01), _frag(1, 0.02), _frag(2, 0.05)]
    state = {"original_query": "q", "top_k": 5, "rrf_chunks": chunks, "web_search_docs": []}
    result = node.process(state)

    merged = _merged_of(result["reranked_docs"])
    assert merged["chunk_id"] == "c2"
    assert merged["part_chunk_ids"] == ["c1", "c2", "c3"]  # 按片段序号，不是出现顺序
    assert merged["merged_parts"] == 3
    # 库内字段全部继承代表片，否则接口层无法回源
    assert merged["document_id"] == 7 and merged["chunk_index"] == 2
    assert merged["file_name"] == "加热板保养.pdf"
    assert merged["content"].startswith(f"{BASE}\n\n")


# --------------------------------------------------------------------------
# 二、不该合并的一律不并
# --------------------------------------------------------------------------

def test_isolated_fragment_and_plain_duplicate_titles_are_left_alone(recorded_rerank) -> None:
    node = NodeRerank()
    chunks = [
        _frag(1, 0.03),                                    # 孤立碎片：组里只有它自己
        _plain(0, rrf_score=0.02),                         # 无后缀同名不同章节：两条也不能并
        {**_plain(1), "title": "独立章节0"},
    ]
    state = {"original_query": "q", "top_k": 5, "rrf_chunks": chunks, "web_search_docs": []}
    result = node.process(state)

    assert len(recorded_rerank["contents"]) == 3
    assert all("merged_parts" not in doc for doc in result["reranked_docs"])
    assert all("rerank_probe" not in doc for doc in result["reranked_docs"])


def test_fragments_from_different_documents_are_not_merged(recorded_rerank) -> None:
    """同一份手册重复导入时两份的章节标题一模一样：跨文档拼接会得到一个归属错误的来源。"""
    node = NodeRerank()
    chunks = [
        _frag(1, 0.03),
        _frag(2, 0.02),
        {**_frag(1, 0.015, token="重复导入的那份"), "document_id": 9, "chunk_id": "d1"},
        {**_frag(2, 0.01, token="重复导入的那份"), "document_id": 9, "chunk_id": "d2"},
    ]
    state = {"original_query": "q", "top_k": 5, "rrf_chunks": chunks, "web_search_docs": []}
    result = node.process(state)

    assert len(recorded_rerank["contents"]) == 2  # 每个文档各自合并一条，不拼成一条
    assert {tuple(sorted(doc["part_chunk_ids"])) for doc in result["reranked_docs"]} == {
        ("c1", "c2"), ("d1", "d2")
    }
    assert all(doc["merged_parts"] == 2 for doc in result["reranked_docs"])


def test_web_results_are_never_merged(recorded_rerank) -> None:
    """联网结果的标题来自页面，恰好以 “-<数字>” 结尾（版本号、页码）时不能和本地碎片拼接。"""
    node = NodeRerank()
    state = {
        "original_query": "q",
        "top_k": 3,
        "rrf_chunks": [_plain(0)],
        "web_search_docs": [
            {"snippet": "第 1 页摘要", "title": f"{BASE}-1", "url": "http://a/1"},
            {"snippet": "第 2 页摘要", "title": f"{BASE}-2", "url": "http://a/2"},
        ],
    }
    node.process(state)

    assert len(recorded_rerank["contents"]) == 3
    assert "第 1 页摘要" in recorded_rerank["contents"]
    assert "第 2 页摘要" in recorded_rerank["contents"]


def test_merge_disable_env_restores_the_old_behaviour(recorded_rerank, monkeypatch: pytest.MonkeyPatch) -> None:
    """A/B 对照开关：关掉后逐字回到"不合并、不设探测文本"，评测能拿它取旧基线。"""
    monkeypatch.setenv("RERANK_A_MERGE_DISABLE", "1")
    node = NodeRerank()
    chunks = [_frag(1, 0.02), _frag(2, 0.03), _frag(3, 0.01)]
    state = {"original_query": "q", "top_k": 5, "rrf_chunks": chunks, "web_search_docs": []}
    result = node.process(state)

    assert len(recorded_rerank["contents"]) == 3  # 三片各占一个候选名额（旧行为）
    assert sorted(recorded_rerank["contents"]) == sorted(
        doc["content"][:RERANK_MAX_CONTENT_CHARS] for doc in result["reranked_docs"]
    )
    assert all("rerank_probe" not in doc and "merged_parts" not in doc for doc in result["reranked_docs"])


# --------------------------------------------------------------------------
# 三、分数口径：正常路径由精排给，降级路径取组内最强那片
# --------------------------------------------------------------------------

def test_merged_score_comes_from_the_reranker(recorded_rerank) -> None:
    node = NodeRerank()
    recorded_rerank["scores"] = [0.77]
    state = {
        "original_query": "q",
        "top_k": 5,
        "rrf_chunks": [_frag(1, 0.02), _frag(2, 0.03), _frag(3, 0.01)],
        "web_search_docs": [],
    }
    result = node.process(state)

    merged = _merged_of(result["reranked_docs"])
    assert merged["score"] == 0.77
    # 关键：这个分不再是任何一片的单片分（旧行为直接把 best 片的分顶给三倍长的正文）
    assert merged["score"] not in {0.01, 0.02, 0.03}
    assert result["rerank_failed"] is False


def test_fallback_path_seeds_with_group_max_and_skips_the_cliff(recorded_rerank) -> None:
    node = NodeRerank()
    recorded_rerank["error"] = RerankError("模拟精排服务故障")
    chunks = [
        _frag(1, 0.01), _frag(2, 0.03), _frag(3, 0.02),
        _plain(0, rrf_score=0.02), _plain(1, rrf_score=0.015), _plain(2, rrf_score=0.001),
    ]
    state = {"original_query": "q", "top_k": 5, "rrf_chunks": chunks, "web_search_docs": []}
    result = node.process(state)

    assert result["rerank_failed"] is True
    ranked = result["reranked_docs"]
    merged = _merged_of(ranked)
    # 合并体的降级种子 = 组内最高 rrf_score（0.03），归一后就是满分
    assert merged["score"] == pytest.approx(1.0)
    # 第 3 条与第 4 条之间 rel_gap 远超 0.25，正常路径会被断崖切掉；降级路径必须跳过断崖
    assert len(ranked) == 4
    assert ranked[-1]["chunk_id"] == "n2"


# --------------------------------------------------------------------------
# 四、静态守卫：别再有人把合并加回精排之后
# --------------------------------------------------------------------------

def _function_def(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_post_rerank_merge_step_is_gone_forever() -> None:
    """后置合并这一步已被前移取代；留着调用点会让两条路径同时生效（重复拼接正文）。"""
    tree = ast.parse(RERANK_NODE_PATH.read_text(encoding="utf-8"))
    assert _function_def(tree, "_step_2c_merge_same_section_fragments") is None
    referenced = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "_step_2c_merge_same_section_fragments"
    ]
    assert referenced == []
    assert _function_def(tree, "_merge_local_fragments") is not None


def test_merge_happens_before_the_candidate_slice() -> None:
    """合并必须在候选截断之前，否则碎片仍占坑——这正是原后置合并没达成的目的。"""
    tree = ast.parse(RERANK_NODE_PATH.read_text(encoding="utf-8"))
    step = _function_def(tree, "_step_1_merge_multi_source_docs")
    assert step is not None, "_step_1_merge_multi_source_docs 被改名了，这条守卫会静默失效"

    merge_lines = [
        node.lineno
        for node in ast.walk(step)
        if isinstance(node, ast.Attribute) and node.attr == "_merge_local_fragments"
    ]
    slice_lines = [
        node.lineno
        for node in ast.walk(step)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Slice)
        and isinstance(node.slice.upper, ast.Name)
        and node.slice.upper.id == "local_limit"
    ]
    assert merge_lines and slice_lines
    assert max(merge_lines) < min(slice_lines)


# --------------------------------------------------------------------------
# 五、来源接线：合并痕迹要一路带到来源面板，探测文本不能外泄
# --------------------------------------------------------------------------

def _merged_source_doc() -> dict:
    return {
        "chunk_id": "c2",
        "title": BASE,
        "content": "合并后的完整正文",
        "score": 0.77,
        "source": "local",
        "document_id": 7,
        "chunk_index": 2,
        "merged_parts": 3,
        "part_chunk_ids": ["c1", "c2", "c3"],
        "rerank_probe": "这是送精排的探测副本，不该出现在任何对外结构里",
    }


def test_sources_carry_part_ids_without_the_probe() -> None:
    """探测文本会随 state 进日志并写进 Mongo 历史；分片 ID 则必须留着才能回源。"""
    item = NodeAnswerOutput._step_2b_generation_sources({"rerank_failed": False}, [_merged_source_doc()])[0]
    assert item["merged_parts"] == 3
    assert item["part_chunk_ids"] == ["c1", "c2", "c3"]
    assert "rerank_probe" not in item
    assert item["score_kind"] == "rerank"


def test_single_part_ids_do_not_add_merge_fields() -> None:
    """只剩一片可溯源时不加合并标注：徽标说的是“读到几段”，一片就没什么好标的。"""
    doc = {key: value for key, value in _merged_source_doc().items() if key != "merged_parts"}
    doc["part_chunk_ids"] = ["c2"]
    item = NodeAnswerOutput._step_2b_generation_sources({}, [doc])[0]
    assert "merged_parts" not in item and "part_chunk_ids" not in item


def test_non_merged_source_structure_is_byte_identical() -> None:
    """零回归保证：没发生合并时，来源条目结构与改前逐字相等。"""
    original = {
        "content": "普通章节正文",
        "source": "local",
        "chunk_id": 42,
        "title": "章节",
        "score": 0.5,
        "document_id": 7,
        "chunk_index": 3,
    }
    item = NodeAnswerOutput._step_2b_generation_sources({}, [dict(original)])[0]
    assert item == {**original, "score_kind": "rerank"}


def _chunk(vector_id: str = "c2", content: str = "代表片自己的单段正文", chunk_index: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        vector_id=vector_id,
        content=content,
        chunk_index=chunk_index,
        page_number=11,
        section_title=BASE,
        extra_metadata={"parent_title": "第3章 保养"},
    )


def _document(doc_id: int = 7, title: str = "加热板保养手册") -> SimpleNamespace:
    return SimpleNamespace(id=doc_id, title=title)


def test_local_source_out_counts_only_traceable_parts() -> None:
    source = {
        "chunk_id": "c2",
        "source": "local",
        "part_chunk_ids": ["c1", "c2", "c3"],
        "content": "合并后的完整正文",
        "merged_parts": 3,
        "score": 0.77,
        "score_kind": "rerank",
    }
    # c3 所在文档已被删除：展示 3 段会给出一个无法溯源的段数
    out = _local_source_out(_chunk(), _document(), source, {"c1", "c2"})
    assert out.merged_parts == 2
    # 合并来源的正文改用检索链路带出的合并内容：代表片的单段正文已不代表模型读到的东西
    assert out.content == "合并后的完整正文"
    assert out.chunk_id == "c2" and out.document_id == 7
    assert out.parent_title == "第3章 保养"


def test_local_source_out_keeps_mysql_content_for_single_source() -> None:
    """非合并来源仍以 MySQL 为准，避免文档重导入后展示陈旧文本。"""
    out = _local_source_out(
        _chunk(content="MySQL 权威正文"),
        _document(),
        {"chunk_id": "c2", "source": "local", "content": "检索链路带出的旧正文", "score": 0.5},
        {"c2"},
    )
    assert out.merged_parts is None
    assert out.content == "MySQL 权威正文"
    assert out.score_kind == "rerank"


class _FakeSession:
    """只支持 _hydrate_sources 那一条 select 的假会话，同时记录被执行的语句。"""

    def __init__(self, rows: list[tuple[SimpleNamespace, SimpleNamespace]]) -> None:
        self._rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> list[Any]:
        self.statements.append(statement)
        return self._rows


async def test_hydrate_drops_source_whose_representative_is_gone() -> None:
    rows = [(_chunk("c1"), _document())]
    sources = [{
        "chunk_id": "c2",
        "source": "local",
        "part_chunk_ids": ["c1", "c2", "c3"],
        "content": "合并后的完整正文",
        "score": 0.77,
    }]
    assert await _hydrate_sources(_FakeSession(rows), 1, sources) == []


async def test_hydrate_checks_every_part_in_a_single_query() -> None:
    """全部候选分片共用同一次 IN 查询：不因为合并而多一次 RPC。"""
    rows = [(_chunk("c1"), _document()), (_chunk("c2"), _document())]
    session = _FakeSession(rows)
    sources = [{
        "chunk_id": "c2",
        "source": "local",
        "part_chunk_ids": ["c1", "c2", "c3"],
        "content": "合并后的完整正文",
        "score": 0.77,
    }]
    out = await _hydrate_sources(session, 1, sources)

    assert len(session.statements) == 1
    in_values: set[str] = set()
    for value in session.statements[0].compile().params.values():
        if isinstance(value, (list, tuple, set, frozenset)):
            in_values.update(str(item) for item in value)
    assert in_values == {"c1", "c2", "c3"}
    assert len(out) == 1
    assert out[0].merged_parts == 2
    assert out[0].content == "合并后的完整正文"
