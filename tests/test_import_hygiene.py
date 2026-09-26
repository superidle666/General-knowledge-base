"""
导入侧 hygiene 守卫（批次三 P2 组 A）。

这一组改动全是清理（删死方法、删重复配置、基类容忍空 task_id、docstring 校准），
不改行为。所以测试的任务是两件事：
1. 守住"清理成果"，别让人把第三套阈值口径或死方法又加回来；
2. 守住唯一一处真正的行为变更——没有 task_id 时节点照样能跑。
"""
import ast
from pathlib import Path

import pytest

from processor.import_processor import import_config as import_config_module
from processor.import_processor.base import BaseNode
from processor.import_processor.import_config import ImportConfig
from processor.import_processor.nodes import node_document_split as split_module
from processor.import_processor.nodes.node_document_split import NodeDocumentSplit
from utils import task_utils

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPLIT_PATH = PROJECT_ROOT / "processor" / "import_processor" / "nodes" / "node_document_split.py"
BASE_PATH = PROJECT_ROOT / "processor" / "import_processor" / "base.py"

# 连接类参数只允许存在于 config/*，ImportConfig 里重复的那一份已删（P2-3）。
FORBIDDEN_CONNECT_FIELDS = {
    "mineru_api_token", "mineru_base_url",
    "openai_api_base", "openai_api_key", "vl_model", "default_model",
    "milvus_url", "chunks_collection",
    "minio_endpoint", "minio_access_key", "minio_secret_key", "minio_bucket", "minio_secure",
    "embedding_dim",
}


def _class_method_names(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    return {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}


# ---------------------------------------------------------------- P2-1 死代码


def test_legacy_split_methods_are_gone() -> None:
    """旧版“按标题粗切 + 无标题兜底 + 遗留超长切分”三件套已删，内容块路径是唯一切分入口。"""
    # 先确认 AST 读到的这份文件就是进程里真正导入的那个模块：路径写错时，下面的
    # “方法已删”断言读的是另一份文件，永远绿。
    assert Path(split_module.__file__).resolve() == SPLIT_PATH.resolve()
    names = _class_method_names(SPLIT_PATH, "NodeDocumentSplit")
    for dead in ("_step_2_split_by_titles", "_step_3_handle_no_title", "_split_long_section_legacy"):
        assert dead not in names, f"{dead} 又回来了"
        assert not hasattr(NodeDocumentSplit, dead), f"{dead} 仍挂在运行时类上"
    # 现役入口必须还在，防止把整个切分能力一起删掉
    assert {"_step_2_split_by_blocks", "_step_3_handle_no_title_from_blocks", "_split_long_section"} <= names


def test_no_third_source_of_chunk_thresholds() -> None:
    """阈值只有 ImportConfig 与知识库 chunking_config 两处，模块级常量那份已删。"""
    tree = ast.parse(SPLIT_PATH.read_text(encoding="utf-8"))
    module_constants = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "DEFAULT_MAX_CONTENT_LENGTH" not in module_constants
    assert "MIN_CONTENT_LENGTH" not in module_constants
    source = SPLIT_PATH.read_text(encoding="utf-8")
    assert "self.config.max_content_length" in source, "兜底阈值应来自 ImportConfig"


def test_no_source_page_fallback_was_removed() -> None:
    """`setdefault("source_page", section.get("page_number"))` 兜的是一个不存在的 key，恒为 None。"""
    source = SPLIT_PATH.read_text(encoding="utf-8")
    assert 'setdefault("source_page"' not in source


# ---------------------------------------------------------------- P2-3 配置收敛


def test_import_config_has_no_duplicated_connection_fields() -> None:
    """与 config/* 重复的连接类字段全部删除，避免“改哪一处才生效”要靠记性问题。"""
    field_names = set(ImportConfig.__dataclass_fields__)
    assert not (field_names & FORBIDDEN_CONNECT_FIELDS), f"重复字段又回来了：{field_names & FORBIDDEN_CONNECT_FIELDS}"
    assert not hasattr(ImportConfig, "get_minio_base_url"), "MinIO URL 拼接只归 utils/minio_utils 与 config/minio_config"


def test_live_import_config_fields_are_still_readable() -> None:
    """留下的字段必须仍能被取到，防止删字段时顺手删掉活的（item_model 就是活的）。"""
    config = ImportConfig()
    for name in ("max_content_length", "min_content_length", "item_name_chunk_k",
                 "item_name_chunk_size", "entity_enum_char_budget", "image_extensions",
                 "item_model", "embedding_batch_size", "requests_per_minute"):
        assert getattr(config, name) is not None, f"{name} 被误删或取不到值"


def test_env_backed_fields_never_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量缺值时给空串而不是 None：字段类型标的是 str，留 None 就是类型谎言。

    必须先把变量删掉再构造：本机 .env 里 ITEM_MODEL 是有值的，不控制环境的话
    这一条在“回退成 os.getenv('ITEM_MODEL')”后依旧会绿（只能证明当前环境配了值）。
    """
    monkeypatch.delenv("ITEM_MODEL", raising=False)
    config = ImportConfig()
    assert isinstance(config.item_model, str), "ITEM_MODEL 未配置时字段变成了 None"
    assert import_config_module.get_config() is import_config_module.get_config(), "单例失效"


# ---------------------------------------------------------------- P2-2 基类容忍空 task_id


class _ProbeNode(BaseNode):
    """最小节点：只用于观察基类的跟踪行为。"""

    name = "hygiene_probe_node"

    def process(self, state):
        state["probe_ran"] = True
        return state


def test_node_runs_without_task_id() -> None:
    """脚本/单测里直接跑单个节点时 state 常常没有 task_id，不该用 KeyError 惩罚。"""
    state = _ProbeNode()({})
    assert state["probe_ran"] is True


def test_node_runs_with_empty_task_id() -> None:
    """显式空值同样容忍，并且不会把进度记到一个空 key 上污染内存态字典。"""
    state = _ProbeNode()({"task_id": ""})

    assert state["probe_ran"] is True
    assert "" not in task_utils._tasks_running_list, "空 task_id 被当成真实任务记账了"


def test_progress_is_still_tracked_with_task_id() -> None:
    """有 task_id 时跟踪必须照常，防止“容忍空值”被实现成整段跟踪都不做。"""
    task_id = "hygiene-probe-task-1"
    try:
        _ProbeNode()({"task_id": task_id})
        assert task_utils.get_done_task_list(task_id) == ["hygiene_probe_node"]
        assert task_utils.get_running_task_list(task_id) == []
    finally:
        task_utils.clear_task(task_id)


# ---------------------------------------------------------------- P2-2 重复 docstring


def test_base_module_has_a_single_docstring() -> None:
    """base.py 顶部那份复制粘贴的模块 docstring 已删：两个字符串字面量会让第二个只是条死语句。"""
    tree = ast.parse(BASE_PATH.read_text(encoding="utf-8"))
    top_strings = [
        node for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    ]
    assert len(top_strings) == 1, f"模块级字符串字面量有 {len(top_strings)} 个，应只剩一个 docstring"


@pytest.mark.parametrize("module_name", ["node_document_split", "base", "import_config"])
def test_import_side_modules_stay_importable(module_name: str) -> None:
    """清理后仍可正常导入（防止删字段时留下悬空引用）。"""
    __import__(f"processor.import_processor.{module_name}"
               if module_name != "node_document_split"
               else "processor.import_processor.nodes.node_document_split")
    assert split_module is not None
