"""S1 检索链路接通的回归守卫。

这一轮修的是"增强检索链路写了但没接上"的问题：HyDE 还在查已废弃的 v1 集合、联网搜索
失败会拖垮整个问答、联网结果进了 prompt 却进不了来源。防护分两层：

1. 行为测试：纯函数（表达式转义、结果归一化、MCP 解析、来源筛选）直接断言输入输出。
2. 静态守卫：跨文件一致性（两路检索共用同一份字段与集合、节点返回键必须在状态里声明）
   用 AST 读源码校验，避免为了断言而去实例化会拉起外部连接的生产对象。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

from graph_introspection import (
    PROJECT_ROOT,
    imported_names,
    returned_dict_keys,
    typed_dict_keys,
)
from processor.query_processor.nodes.node_source_collection import NodeSourceCollection
from processor.query_processor.nodes.node_web_search_mcp import NodeWebSearchMcp
from processor.query_processor.retrieval_common import (
    ITEM_NAME_FILTER_FIELD,
    LIBRARY_FILTER_FIELD,
    MAX_FILTER_VALUES,
    RETRIEVAL_OUTPUT_FIELDS,
    build_item_name_filter,
    build_library_filter,
    collection_field_names,
    combine_filters,
    normalize_hits,
    resolve_output_fields,
)

HYDE_NODE = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_search_embedding_hyde.py"
LOCAL_NODE = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_search_embedding.py"
WEB_NODE = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_web_search_mcp.py"
QUERY_STATE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"
API_QUERY = PROJECT_ROOT / "backend" / "app" / "api" / "query.py"
API_SCHEMAS = PROJECT_ROOT / "backend" / "app" / "api" / "schemas.py"

# Milvus 布尔表达式里的字符串字面量语法：双引号包裹、内部引号需转义
_MILVUS_QUOTED = '"{}"'


# --------------------------------------------------------------------------
# 一、过滤表达式：多路检索的库隔离与注入防护
# --------------------------------------------------------------------------

def test_library_filter_is_numeric_comparison() -> None:
    assert build_library_filter(7) == f"{LIBRARY_FILTER_FIELD} == 7"
    # 传入数字字符串也按数值拼接，避免生成 knowledge_base_id == "7" 这种类型不匹配的表达式
    assert build_library_filter("7") == f"{LIBRARY_FILTER_FIELD} == 7"


def test_library_filter_rejects_non_numeric_value() -> None:
    """非法 library_id 必须抛错，而不是拼出一个永远匹配不到数据的表达式。"""
    try:
        build_library_filter("7; or 1 == 1")
    except ValueError:
        return
    raise AssertionError("非数值的 library_id 应当被拒绝")


def test_item_name_filter_escapes_quotes_and_collapses_newlines() -> None:
    """实体名可能来自用户输入或大模型抽取，必须转义后才能进入表达式。"""
    expression = build_item_name_filter(['烫金机"Pro"', "设备\n说明书", "带\\反斜杠"])
    assert expression is not None
    assert expression.startswith(f"{ITEM_NAME_FILTER_FIELD} in [")
    # 换行会被折叠，避免表达式跨行导致解析失败
    assert "\n" not in expression
    assert _MILVUS_QUOTED.format('烫金机\\"Pro\\"') in expression
    assert "设备 说明书" in expression


def test_item_name_filter_deduplicates_and_caps_values() -> None:
    expression = build_item_name_filter(["A", "A", " ", "B"] + [f"型号{i}" for i in range(50)])
    assert expression is not None
    # 空串被丢弃、重复值只保留一次，总量被封顶，防止表达式无限膨胀
    assert expression.count('"A"') == 1
    assert len(expression.split(", ")) == MAX_FILTER_VALUES


def test_item_name_filter_is_skipped_when_column_absent() -> None:
    """集合里没有 item_name 列时放弃过滤，宁可多召回也不能让整次检索报错。"""
    assert build_item_name_filter(["A"], {"content", "chunk_id"}) is None
    assert build_item_name_filter(["A"], {"content", "item_name"}) == f'{ITEM_NAME_FILTER_FIELD} in ["A"]'
    assert build_item_name_filter(None) is None
    assert build_item_name_filter(["", "  "]) is None


def test_combine_filters_ignores_empty_conditions() -> None:
    assert combine_filters("a == 1", None) == "a == 1"
    assert combine_filters(None, None) is None
    assert combine_filters("a == 1", 'b in ["x"]') == 'a == 1 and b in ["x"]'


# --------------------------------------------------------------------------
# 二、集合结构与结果归一化
# --------------------------------------------------------------------------

class _FakeMilvusClient:
    """只实现 describe_collection 的最小替身，避免测试连真实 Milvus。"""

    def __init__(self, fields: list[object]) -> None:
        self._fields = fields

    def describe_collection(self, collection_name: str) -> dict:
        return {"schema": {"fields": self._fields}}


def test_collection_field_names_reads_both_schema_shapes() -> None:
    dict_style = _FakeMilvusClient([{"name": "chunk_id"}, {"name": "content"}])
    assert collection_field_names(dict_style, "kb") == {"chunk_id", "content"}

    object_style = _FakeMilvusClient([SimpleNamespace(name="chunk_id")])
    assert collection_field_names(object_style, "kb") == {"chunk_id"}


def test_resolve_output_fields_only_keeps_existing_columns() -> None:
    """请求不存在的字段会让整次检索失败，因此可选输出字段必须按集合结构裁剪。"""
    available = {"chunk_id", "content", "knowledge_base_id", "item_name"}
    resolved = resolve_output_fields(available)
    assert set(resolved) == available
    assert set(RETRIEVAL_OUTPUT_FIELDS) - available and resolved[0] == "chunk_id"


def test_normalize_results_keep_vector_score_and_drop_unaddressable_hits() -> None:
    """没有 chunk_id 的命中无法回填来源，必须丢弃；其余补齐统一结构。"""
    hits = [
        {"id": 11, "distance": 0.87, "entity": {"content": "嵌套结果"}},
        {"chunk_id": "12", "content": "扁平结果", "source": "local"},
        {"content": "没有 ID，应该被丢掉"},
        "不是字典，忽略",
    ]
    normalized = normalize_hits([hits])
    assert [item["chunk_id"] for item in normalized] == [11, "12"]
    assert normalized[0]["content"] == "嵌套结果"
    assert normalized[0]["vector_score"] == 0.87
    assert all(item["source"] == "local" for item in normalized)
    assert normalize_hits(None) == []
    assert normalize_hits([]) == []


# --------------------------------------------------------------------------
# 三、静态守卫：两路检索不得再各写一份
# --------------------------------------------------------------------------

def test_hyde_node_targets_v2_collection_only() -> None:
    """HyDE 曾经查的是导入流程早已不再写入的 v1 集合：开 HyDE 等于白烧一次大模型调用。"""
    text = HYDE_NODE.read_text(encoding="utf-8")
    assert "document_chunks_v2_collection" in text
    assert "chunks_collection" not in text.replace("document_chunks_v2_collection", "")
    # 库隔离条件由共享构造器产生，不可以在节点里再拼一遍字段名
    # （字段硬校验已收到 resolve_output_fields，节点不该自己判 LIBRARY_FILTER_FIELD）
    assert "build_library_filter" in text and "resolve_output_fields" in text
    assert '"knowledge_base_id ==' not in text


def test_both_retrieval_nodes_share_one_field_and_weight_source() -> None:
    """字段白名单与混合检索权重只能有一份来源，否则调一路忘一路就是下一次的静默漂移。"""
    for path in (LOCAL_NODE, HYDE_NODE):
        imported = imported_names(path, "retrieval_common")
        assert "resolve_output_fields" in imported, f"{path.name} 未复用共享输出字段"
        assert "HYBRID_RANKER_WEIGHTS" in imported, f"{path.name} 未复用共享权重"
        assert "normalize_hits" in imported, f"{path.name} 未复用共享结果归一化"
        assert "OUTPUT_FIELDS = (" not in path.read_text(encoding="utf-8")


def test_enhanced_nodes_declare_state_keys_in_schema() -> None:
    """LangGraph 会静默丢弃未在状态里声明的返回键——失败标记一旦丢不到，前端就永远看不到降级。"""
    declared = typed_dict_keys(QUERY_STATE, "QueryGraphState")
    for path, class_name in (
        (HYDE_NODE, "NodeSearchEmbeddingHyde"),
        (WEB_NODE, "NodeWebSearchMcp"),
    ):
        returned = returned_dict_keys(path, class_name, "process")
        assert returned, f"{class_name}.process 没有返回任何字面量键"
        undeclared = returned - declared
        assert not undeclared, f"{class_name}.process 返回了未声明的状态键：{sorted(undeclared)}"


def test_failure_markers_are_declared_in_state() -> None:
    declared = typed_dict_keys(QUERY_STATE, "QueryGraphState")
    assert {"hyde_failed", "web_search_failed", "rerank_failed", "hyde_doc"} <= declared


# --------------------------------------------------------------------------
# 四、MCP 联网检索：解析与降级
# --------------------------------------------------------------------------

def _mcp_result(payload: object) -> SimpleNamespace:
    """构造与 openai-agents SDK 一致的返回形状：content 是一个带 text 字段的段列表。"""
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))])


def test_parse_pages_maps_bailian_shape_into_unified_doc() -> None:
    """字段名对齐百炼真实返回：pages[].{title,url,snippet,hostname}。"""
    payload = {
        "status": 0,
        "pages": [
            {"title": "北京今日天气", "url": "https://e.weather.com.cn", "snippet": "今天白天晴，最高气温29℃", "hostname": "网易"},
            {"title": "无摘要条目", "url": "https://x.test", "hostname": "示例站"},
            "脏数据",
        ],
    }
    docs = NodeWebSearchMcp._parse_pages(_mcp_result(payload))
    assert len(docs) == 1
    assert docs[0] == {
        "title": "北京今日天气",
        "url": "https://e.weather.com.cn",
        "snippet": "今天白天晴，最高气温29℃",
        "site_name": "网易",
    }


def test_parse_pages_tolerates_empty_shapes() -> None:
    assert NodeWebSearchMcp._parse_pages(None) == []
    assert NodeWebSearchMcp._parse_pages(SimpleNamespace(content=[])) == []
    assert NodeWebSearchMcp._parse_pages(SimpleNamespace(content=[SimpleNamespace(text="")])) == []
    assert NodeWebSearchMcp._parse_pages(_mcp_result({"status": 1})) == []


def test_search_count_follows_top_k_with_cap() -> None:
    """搜索条数跟着用户可配的 top_k 走，并留出重排余量、封顶防失控。"""
    resolve = NodeWebSearchMcp._resolve_count
    assert resolve({"top_k": 3}) == 6
    assert resolve({}) == 10
    assert resolve({"top_k": 50}) == 10


def test_web_search_degrades_to_empty_docs_on_failure(monkeypatch) -> None:
    """联网失败必须降级，不能让整条问答链路一起失败。"""
    node = NodeWebSearchMcp()

    async def broken_call(query: str, count: int = 5):
        raise TimeoutError("MCP 连接超时")

    monkeypatch.setattr(node, "_mcp_call", broken_call)
    result = node.process({"rewritten_query": "HAK180 报错 E12 怎么处理"})
    assert result == {"web_search_docs": [], "web_search_failed": True}


def test_web_search_reports_failure_flag_on_success(monkeypatch) -> None:
    node = NodeWebSearchMcp()
    payload = {"pages": [{"title": "T", "url": "https://a.test", "snippet": "S" * 40, "hostname": "站点"}]}

    async def working_call(query: str, count: int = 5):
        return _mcp_result(payload)

    monkeypatch.setattr(node, "_mcp_call", working_call)
    result = node.process({"rewritten_query": "问题", "top_k": 2})
    assert result["web_search_failed"] is False
    assert result["web_search_docs"][0]["site_name"] == "站点"


def test_web_search_is_skipped_without_question() -> None:
    result = NodeWebSearchMcp().process({"rewritten_query": "   ", "original_query": ""})
    assert result == {"web_search_docs": [], "web_search_failed": False}


# --------------------------------------------------------------------------
# 五、来源整理：本地与联网两套阈值、统一 top_k
# --------------------------------------------------------------------------

def _rerank_doc(content: str, source: str, score: float) -> dict:
    return {"content": content, "source": source, "score": score, "chunk_id": None, "url": None, "title": "T"}


def test_short_web_snippets_are_not_dropped_by_local_threshold() -> None:
    """60 字阈值是为本地碎片设计的；联网摘要普遍更短，共用会把外部来源全静默丢掉。"""
    web_doc = _rerank_doc("联网摘要" * 6, "web", 0.9)
    local_doc = _rerank_doc("本地碎片太短", "local", 0.9)
    state = {"reranked_docs": [web_doc, local_doc], "top_k": 5}
    sources = NodeSourceCollection().process(state)["sources"]
    assert [item["source"] for item in sources] == ["web"]


def test_sources_respect_top_k_and_rerank_threshold() -> None:
    docs = [_rerank_doc("足够长的本地知识库正文" * 6, "local", 0.9 - index * 0.01) for index in range(5)]
    docs.append(_rerank_doc("低于阈值的正文内容" * 10, "local", 0.2))
    state = {"reranked_docs": docs, "top_k": 3}
    sources = NodeSourceCollection().process(state)["sources"]
    assert len(sources) == 3
    assert all(item["score"] >= 0.5 for item in sources)


def test_rerank_failure_falls_back_to_recall_order() -> None:
    """精排服务挂了时用向量分数继续出来源，比空结果更有用，但不能再去卡相关性阈值。"""
    docs = [{"content": "本地知识库正文内容" * 10, "source": "local", "chunk_id": 1, "vector_score": 0.31}]
    state = {"reranked_docs": docs, "rerank_failed": True, "top_k": 5}
    sources = NodeSourceCollection().process(state)["sources"]
    assert len(sources) == 1
    assert sources[0]["score"] == 0.31


def _long_doc(source: str, score: float, index: int) -> dict:
    return _rerank_doc(f"{source} 正文内容片段" * 8, source, score - index * 0.01)


def test_local_sources_keep_reserved_slots_when_web_outranks_them() -> None:
    """联网摘要句子完整，Cross-Encoder 打分普遍高于本地碎片；本地不保底就会“开了联网看不到自己资料”。"""
    docs = [_long_doc("web", 0.95, index) for index in range(5)]
    docs += [_long_doc("local", 0.70, index) for index in range(5)]
    sources = NodeSourceCollection().process({"reranked_docs": docs, "top_k": 5})["sources"]

    assert len(sources) == 5, "来源总数不得超出 top_k"
    local_count = sum(1 for item in sources if item["source"] == "local")
    assert local_count == 3, "ceil(5 * 0.6) = 3 个本地保底槽位未生效"
    # 保底只影响谁入选，不影响展示顺序：仍然按分数降序
    assert [item["score"] for item in sources] == sorted((item["score"] for item in sources), reverse=True)


def test_local_quota_never_pads_with_unqualified_local_sources() -> None:
    """保底是“有合格结果时不被挤出”，不是硬性比例；本地全低于阈值时不得为了凑数把它塞进来。"""
    docs = [_long_doc("web", 0.80, index) for index in range(5)]
    docs.append(_long_doc("local", 0.20, 0))
    sources = NodeSourceCollection().process({"reranked_docs": docs, "top_k": 5})["sources"]

    assert len(sources) == 5
    assert {item["source"] for item in sources} == {"web"}


def test_local_sources_can_fill_slots_left_by_web() -> None:
    """联网结果不足时本地继续补位：配额不能反过来减少总来源数。"""
    docs = [_long_doc("local", 0.70, index) for index in range(5)]
    docs.append(_long_doc("web", 0.90, 0))
    sources = NodeSourceCollection().process({"reranked_docs": docs, "top_k": 5})["sources"]

    assert len(sources) == 5
    assert sum(1 for item in sources if item["source"] == "local") == 4


def test_relevant_local_doc_survives_a_lower_absolute_score() -> None:
    """实测场景：本地“## 调节色彩”对“打印机怎么调整对比度”只拿 0.44，但它是真答案。

    只要本地还沿用联网那把 0.5 绝对尺子，这类“内容对但篇幅短”的切片会在源头就被判死；
    所以本地阈值必须按自己组内高低算，而不是跟网页摘要拼绝对分。
    """
    docs = [_long_doc("web", 0.75, index) for index in range(5)]
    docs.append(_long_doc("local", 0.44, 0))
    sources = NodeSourceCollection().process({"reranked_docs": docs, "top_k": 5})["sources"]

    assert [item["source"] for item in sources].count("local") == 1
    assert min(item["score"] for item in sources) < 0.5, "本地唯一相关切片被绝对阈值误杀了"


def test_local_group_is_cut_by_its_own_relative_score() -> None:
    """本地不能为了凑数把无关碎片也带进来：远低于本组最高分的切片仍要被剔除。"""
    docs = [_long_doc("web", 0.80, index) for index in range(5)]
    docs.append(_long_doc("local", 0.46, 0))   # 本组最高分 → 相对线 0.46*0.8=0.368
    docs.append(_long_doc("local", 0.39, 5))   # 得分 0.34：高于地板分 0.3，但低于本组相对线 0.368
    sources = NodeSourceCollection().process({"reranked_docs": docs, "top_k": 5})["sources"]

    local_scores = [item["score"] for item in sources if item["source"] == "local"]
    assert len(local_scores) == 1
    assert round(local_scores[0], 2) == 0.46


# --------------------------------------------------------------------------
# 六、接口层：联网来源必须能被透传出去
# --------------------------------------------------------------------------

def test_query_source_schema_carries_web_fields() -> None:
    """来源结构没有 url/site_name 时，联网结果就只能"悄悄被引用"，用户无法核验。"""
    fields = _annotated_fields(API_SCHEMAS, "QuerySourceOut")
    assert {"source", "url", "site_name"} <= fields


def test_hydrate_sources_has_web_branch() -> None:
    text = API_QUERY.read_text(encoding="utf-8")
    assert "_web_source_out" in text
    assert 'source="web"' in text
    # 旧的"只保留本地来源"过滤条件不应再存在
    assert 'if source.get("source", "local") == "local" and source.get("chunk_id")' not in text


def test_hyde_switch_belongs_to_library_not_query_request() -> None:
    """HyDE 多一次大模型调用、抬高时延与成本，而且用户看不懂这个缩写。

    因此开关收归知识库规则（管理端配置），请求体不得再携带；
    两边任一侧回退（接口重新读 payload，或库模型丢字段）都会让策略开关静默失效。
    """
    request_fields = _annotated_fields(API_SCHEMAS, "QueryRequest")
    assert "use_hyde" not in request_fields, "HyDE 开关不应回到用户请求体"
    assert "use_web_search" in request_fields
    for model_name in ("LibraryCreate", "LibraryUpdate", "LibraryOut"):
        assert "use_hyde" in _annotated_fields(API_SCHEMAS, model_name), f"{model_name} 缺少 use_hyde"
    text = API_QUERY.read_text(encoding="utf-8")
    assert "payload.use_hyde" not in text
    # 同步与流式两条链路都要按知识库字段注入，只改一条会使两个入口行为不一致
    assert text.count("bool(library.use_hyde)") == 2


def test_new_library_column_has_matching_migration() -> None:
    """模型新增列必须配 Alembic 迁移，否则服务一启动就是 Unknown column。"""
    models_text = (PROJECT_ROOT / "backend" / "app" / "db" / "models.py").read_text(encoding="utf-8")
    assert "use_hyde: Mapped[bool]" in models_text
    versions_dir = PROJECT_ROOT / "backend" / "alembic" / "versions"
    migrated = any(
        "use_hyde" in path.read_text(encoding="utf-8") and "kb_library" in path.read_text(encoding="utf-8")
        for path in versions_dir.glob("*.py")
    )
    assert migrated, "kb_library.use_hyde 缺迁移脚本"


def _annotated_fields(path: Path, class_name: str) -> set[str]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                body.target.id
                for body in node.body
                if isinstance(body, ast.AnnAssign) and isinstance(body.target, ast.Name)
            }
    raise AssertionError(f"{path.as_posix()} 中找不到类 {class_name}")
