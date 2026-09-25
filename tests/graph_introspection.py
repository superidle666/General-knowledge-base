"""测试用的源码自省工具：不导入生产模块，只用 AST 读源码做跨文件一致性校验。

检索与导入链路的节点在实例化时会拉起 Milvus / Redis / LLM / Mongo 连接，直接 import 做单测
既不稳定也很慢。而这些一致性风险（节点改名、状态词表漂移、进度表漏项）本质上是"源码之间是否
自洽"的问题，用 AST 静态解析就能确定性地覆盖。
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

IMPORT_MAIN_GRAPH = PROJECT_ROOT / "processor" / "import_processor" / "main_graph.py"
QUERY_MAIN_GRAPH = PROJECT_ROOT / "processor" / "query_processor" / "main_graph.py"
IMPORT_WORKER = PROJECT_ROOT / "processor" / "import_processor" / "worker.py"
PERSIST_CHUNKS = PROJECT_ROOT / "processor" / "import_processor" / "nodes" / "node_persist_chunks.py"
API_MYSQL = PROJECT_ROOT / "backend" / "app" / "api" / "mysql.py"


def registered_node_names(main_graph: Path) -> set[str]:
    """提取 `xxx.add_node("name", ...)` 注册的节点名集合。"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(main_graph.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        first = node.args[0]
        if isinstance(func, ast.Attribute) and func.attr == "add_node":
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
    return names


def _dml_values_model(node: ast.Call) -> str | None:
    """从 `update(Model).where(...).values(...)` / `insert(Model).values(...)` 取出被写的模型名。

    只认得确定是 DML 语句的链式调用（链上必须有 update/insert），否则 `some_dict.values()`
    这类普通调用也会被当成写入点。
    """
    current: ast.AST | None = node.func.value if isinstance(node.func, ast.Attribute) else None
    while isinstance(current, ast.Call):
        func = current.func
        if isinstance(func, ast.Name) and func.id in {"update", "insert"}:
            first = current.args[0] if current.args else None
            return first.id if isinstance(first, ast.Name) else None
        func = func if isinstance(func, ast.Attribute) else None
        current = func.value if func is not None else None
    return None


def literal_assignments(paths: list[Path], attribute: str, owners: set[str]) -> dict[str, list[str]]:
    """收集某属性被写成字符串字面量的位置，返回 值 -> 出处(文件:行号) 列表。

    覆盖两种写入形态：
    - ORM 属性赋值：`task.status = "processing"`
    - SQL 侧写入：`update(ImportTask).values(status="processing")`（条件更新，归属看模型名）
    第二种必须一起认，否则把写入从 Python 侧挪进 SQL 会让守卫误报“某状态没人写”。
    """
    found: dict[str, list[str]] = {}
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            values: list[tuple[str, int]] = []
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                value = node.value.value
                if isinstance(value, str):
                    values = [
                        (value, node.lineno)
                        for target in node.targets
                        if isinstance(target, ast.Attribute)
                        and target.attr == attribute
                        and isinstance(target.value, ast.Name)
                        and target.value.id in owners
                    ]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "values"
                and _dml_values_model(node) in owners
            ):
                values = [
                    (keyword.value.value, node.lineno)
                    for keyword in node.keywords
                    if keyword.arg == attribute
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ]
            for value, lineno in values:
                rel = f"{path.relative_to(PROJECT_ROOT).as_posix()}:{lineno}"
                found.setdefault(value, []).append(rel)
    return found


def collection_literal(path: Path, variable: str) -> set[str]:
    """读取模块级字面量集合：支持 `X = {...}`、`X = frozenset({...})`、`X = [...]`。"""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Assign):
            continue
        matched = any(
            isinstance(target, ast.Name) and target.id == variable for target in node.targets
        )
        if not matched:
            continue
        container = node.value
        if isinstance(container, ast.Call) and container.args:
            container = container.args[0]
        if isinstance(container, ast.Dict):
            elements = container.keys
        elif isinstance(container, (ast.Set, ast.List, ast.Tuple)):
            elements = container.elts
        else:
            raise AssertionError(f"{path.name} 中的 {variable} 不是字面量集合，无法静态校验")
        return {
            element.value
            for element in elements
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    raise AssertionError(f"{path.as_posix()} 中找不到模块级变量 {variable}，可能已被重构，请同步更新用例")


def typed_dict_keys(path: Path, class_name: str) -> set[str]:
    """读取 TypedDict 声明的字段名。

    LangGraph 只会把已声明的键写回状态，节点返回未声明的键会被静默丢弃（不报错），
    所以“节点返回键 ⊆ 状态声明键”必须能被静态校验。
    """
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            keys: set[str] = set()
            for body in node.body:
                if isinstance(body, ast.AnnAssign) and isinstance(body.target, ast.Name):
                    keys.add(body.target.id)
            return keys
    raise AssertionError(f"{path.as_posix()} 中找不到类 {class_name}，请同步更新用例")


def returned_dict_keys(path: Path, class_name: str, method_name: str) -> set[str]:
    """收集某个节点方法内所有 `return {字面量键...}` 的键名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        keys: set[str] = set()
        found_method = False
        for member in ast.walk(node):
            if not (isinstance(member, ast.FunctionDef) and member.name == method_name):
                continue
            found_method = True
            for ret in ast.walk(member):
                if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Dict):
                    for key in ret.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            keys.add(key.value)
        if not found_method:
            raise AssertionError(f"{class_name} 中没有 {method_name} 方法，请同步更新用例")
        return keys
    raise AssertionError(f"{path.as_posix()} 中找不到类 {class_name}，请同步更新用例")


def imported_names(path: Path, module: str) -> set[str]:
    """读取文件从指定模块导入了哪些名字（用于“不得再引用旧集合”这类防回归断言）。"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith(module):
            names.update(alias.name for alias in node.names)
    return names
