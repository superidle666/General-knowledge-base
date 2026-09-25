"""P0-5：日志配置收敛到进程入口，根日志器不再被互相清空。

此前 tool/logger.py 在 import 期执行 `logging.getLogger().handlers.clear()` 挂自己的
handler，processor/import_processor/base.setup_logging 又做一遍同样的事，而
utils/embedding_utils.py 在模块导入期就调用 setup_logging()——谁最后 import 谁赢，
问答侧会看到导入侧的 handler、宿主进程挂好的 handler 会被清空。
这里锁住新的契约：
1 configure_logging 幂等：重复调用不叠加项目 handler；
2 任何配置路径都不移除别人（宿主 / pytest / 其他库）已挂的 handler；
3 导入侧 setup_logging 与问答侧 configure_logging 共用同一实现，两侧调用顺序无关；
4 库模块（含 __main__ 之外的所有模块级代码）不在导入期改动日志配置；
5 问答侧 logger 是命名空间 logger，不再是根日志器本身；
6 配置后项目 INFO 真的能输出（根日志器出厂默认 WARNING，漏设级别＝静默丢日志），
  但宿主已配过根日志器时不抢级别方向盘。
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from graph_introspection import PROJECT_ROOT
from processor.import_processor.base import setup_logging
from tool.logger import _CONSOLE_HANDLER_MARK, configure_logging, logger, reset_logging

# 这些模块会被生产代码 import，模块级不得出现任何日志配置动作。
LIBRARY_MODULES = [
    PROJECT_ROOT / "tool" / "logger.py",
    PROJECT_ROOT / "utils" / "embedding_utils.py",
    PROJECT_ROOT / "utils" / "milvus_utils.py",
    PROJECT_ROOT / "processor" / "import_processor" / "base.py",
    PROJECT_ROOT / "processor" / "query_processor" / "base.py",
    *sorted((PROJECT_ROOT / "processor" / "import_processor" / "nodes").glob("*.py")),
    *sorted((PROJECT_ROOT / "processor" / "query_processor" / "nodes").glob("*.py")),
]

# 会被调用到的日志配置函数名；模块级出现任何一个都说明副作用又回来了。
_LOGGING_SETUP_NAMES = {"setup_logging", "configure_logging"}


@pytest.fixture
def pristine_root_logging() -> Iterator[logging.Logger]:
    """用例结束后把根日志器和项目命名空间 logger 恢复原状，避免泄漏到其它测试。"""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    namespaces = [logging.getLogger(name) for name in ("kb", "import")]
    namespace_levels = [namespace.level for namespace in namespaces]
    yield root
    for handler in list(root.handlers):
        if handler not in original_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(original_level)
    for namespace, level in zip(namespaces, namespace_levels):
        namespace.setLevel(level)


@contextmanager
def fresh_process_root() -> Iterator[logging.Logger]:
    """把根日志器临时恢复成“全新进程、宿主没配过日志”的样子，退出时原样放回。

    必须在测试函数体内用 with 包住：pytest 自己在 call 阶段才往 root 上挂捕获 handler，
    放在 fixture setup 里会被后续补上的 handler 抵消。
    """
    root = logging.getLogger()
    handler_snapshot, level_snapshot = list(root.handlers), root.level
    root.handlers[:] = []
    try:
        yield root
    finally:
        root.handlers[:] = handler_snapshot
        root.setLevel(level_snapshot)


def _project_handlers(root: logging.Logger) -> list[logging.Handler]:
    return [handler for handler in root.handlers if getattr(handler, _CONSOLE_HANDLER_MARK, False)]


def _module_level_statements(tree: ast.Module) -> list[ast.stmt]:
    """取出真正的模块级语句：函数体、类体和 `if __name__` 手动运行块都不算。"""
    statements: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.If):
            # `if __name__ == "__main__":` 块属于进程入口，允许配置日志
            test = node.test
            if isinstance(test, ast.Compare) and ast.unparse(test.left) == "__name__":
                continue
            statements.extend(node.body)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        else:
            statements.append(node)
    return statements


def _logging_calls_at_module_level(path: Path) -> list[int]:
    """返回该文件模块级日志配置调用的行号，用于把回归位置直接说清楚。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[int] = []
    for statement in _module_level_statements(tree):
        for call in ast.walk(statement):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            called_name = getattr(func, "attr", None) or (ast.unparse(func) if isinstance(func, ast.Name) else "")
            if called_name in _LOGGING_SETUP_NAMES:
                offenders.append(call.lineno)
                continue
            # 形如 logging.getLogger().handlers.clear() / logger.handlers.clear()
            if isinstance(func, ast.Attribute) and func.attr == "clear":
                receiver = ast.unparse(func.value)
                if "handlers" in receiver:
                    offenders.append(call.lineno)
    return offenders


def test_configure_logging_is_idempotent(pristine_root_logging: logging.Logger) -> None:
    """重复调用只调级别，不叠加第二个项目 handler（旧实现靠 clear() 掩盖重复挂载）。"""
    configure_logging()
    first = _project_handlers(pristine_root_logging)
    configure_logging()
    configure_logging(level=logging.DEBUG)

    assert len(first) == 1
    assert len(_project_handlers(pristine_root_logging)) == 1, "重复配置后项目 handler 叠加了"
    assert _project_handlers(pristine_root_logging)[0].level == logging.DEBUG


def test_configure_logging_keeps_foreign_handlers(pristine_root_logging: logging.Logger) -> None:
    """宿主进程 / pytest 已挂的 handler 必须原样保留。"""
    foreign = logging.Handler()
    pristine_root_logging.addHandler(foreign)

    configure_logging()
    setup_logging()

    assert foreign in pristine_root_logging.handlers, "配置项目日志时把别人的 handler 清掉了"
    assert len(_project_handlers(pristine_root_logging)) == 1


def test_reset_logging_removes_only_project_handlers(pristine_root_logging: logging.Logger) -> None:
    """清理函数不能顺手拆掉别人的输出通道。"""
    foreign = logging.Handler()
    pristine_root_logging.addHandler(foreign)
    configure_logging()

    reset_logging()

    assert _project_handlers(pristine_root_logging) == []
    assert foreign in pristine_root_logging.handlers


def test_both_sides_converge_on_one_handler_regardless_of_order(
    pristine_root_logging: logging.Logger,
) -> None:
    """两侧谁先配置都一样：不再出现"最后 import 的人独占根日志器"。"""
    setup_logging()
    configure_logging()
    assert len(_project_handlers(pristine_root_logging)) == 1

    reset_logging()
    configure_logging()
    setup_logging()
    assert len(_project_handlers(pristine_root_logging)) == 1


def test_configure_logging_makes_project_info_visible() -> None:
    """配置完必须真能打出 INFO：根日志器出厂默认 WARNING，漏设级别就是静默丢日志。"""
    with fresh_process_root() as root:
        root.setLevel(logging.WARNING)

        configure_logging()

        assert root.level == logging.INFO
        assert logger.isEnabledFor(logging.INFO) is True, "配置后项目 INFO 日志仍被级别门掉"
        assert logging.getLogger("import.node_pdf_to_md").isEnabledFor(logging.INFO) is True


def test_configure_logging_respects_host_level() -> None:
    """宿主自己配过根日志器时不抢级别方向盘，但项目自己的 INFO 仍必须可见。"""
    with fresh_process_root() as root:
        host_handler = logging.Handler()
        root.handlers[:] = [host_handler]
        root.setLevel(logging.ERROR)

        configure_logging()

        assert root.level == logging.ERROR, "覆盖了宿主进程的日志级别"
        assert host_handler in root.handlers, "配置项目日志时把宿主的 handler 干掉了"
        assert len(_project_handlers(root)) == 1
        assert logger.isEnabledFor(logging.INFO) is True, "第三方库配过 root 后项目 INFO 被静默丢弃"
        assert logging.getLogger("import.node_pdf_to_md").isEnabledFor(logging.INFO) is True


def test_query_logger_is_namespaced_and_still_propagates() -> None:
    """问答侧不再直接持有根日志器，但仍冒泡到根，宿主可统一收集。"""
    assert logger is not logging.getLogger()
    assert logger.name == "kb"
    assert logger.propagate is True


@pytest.mark.parametrize("module_path", LIBRARY_MODULES, ids=lambda path: path.name)
def test_library_modules_do_not_configure_logging_at_import(module_path: Path) -> None:
    """库模块导入期不得动日志配置——这是"格式随机 / 互相清空"的病根。"""
    assert module_path.exists(), f"待检查文件不存在：{module_path}"
    offenders = _logging_calls_at_module_level(module_path)
    assert not offenders, f"{module_path.name} 第 {offenders} 行在模块级配置日志或清空 handler"
