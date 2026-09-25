"""
BGE-M3 懒加载单例的并发保护测试（批次二 P1-9）。

原来 `get_bge_m3_ef()` 是裸的 check-then-act：只判空、不加锁。查询侧（FastAPI 请求线程）
与导入侧（Worker 线程池）共用这一个入口，并发首查时每个线程都看得到 None，于是各自
加载一份 BGE-M3（约 2GB）——峰值内存按并发数翻倍，最后只有一份被留下，其余变垃圾。

这里锁的是三件事：只加载一次、热路径不被锁串行化、加载失败不留半成品。
所有用例都必须替换掉真模型构造函数，绝不能在本机真的加载权重。
"""
from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from utils import embedding_utils

THREAD_COUNT = 8
MODULE_PATH = Path(embedding_utils.__file__)


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例都从"模型未加载"开始，避免用例间通过模块全局互相污染。"""
    monkeypatch.setattr(embedding_utils, "_bge_m3_ef", None)


# ---------------------------------------------------------------- 并发行为


def test_concurrent_first_calls_load_the_model_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """8 个线程同时首查，只能加载一份模型。"""
    built: list[object] = []
    record_lock = threading.Lock()

    class SlowModel:
        def __init__(self, **kwargs: object) -> None:
            time.sleep(0.05)  # 拉长临界区，把竞态窗口真的打开
            with record_lock:
                built.append(self)

    monkeypatch.setattr(embedding_utils, "BGEM3EmbeddingFunction", SlowModel)

    barrier = threading.Barrier(THREAD_COUNT)
    results: list[object] = []

    def worker() -> None:
        barrier.wait()
        results.append(embedding_utils.get_bge_m3_ef())

    threads = [threading.Thread(target=worker) for _ in range(THREAD_COUNT)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads), "有线程卡死在锁上"
    assert len(built) == 1, f"并发首查加载了 {len(built)} 份模型，每份都是 GB 级"
    assert len(results) == THREAD_COUNT
    assert all(result is built[0] for result in results), "有线程拿到了别的模型实例"


def test_hot_path_does_not_take_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型已就绪时不得去抢锁，否则高并发下所有请求会在这把锁上串行。"""
    acquires: list[int] = []

    class CountingLock:
        def __enter__(self) -> "CountingLock":
            acquires.append(1)
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    ready = object()
    monkeypatch.setattr(embedding_utils, "_bge_m3_ef", ready)
    monkeypatch.setattr(embedding_utils, "_bge_m3_ef_lock", CountingLock())

    assert embedding_utils.get_bge_m3_ef() is ready
    assert acquires == [], "已就绪仍进锁：加载完了还把并发压成串行"


def test_failed_load_is_not_cached_and_can_be_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """加载抛错不能被缓存成“半个单例”，否则进程活着但向量功能永久失效。"""
    attempts: list[dict] = []

    class FlakyModel:
        def __init__(self, **kwargs: object) -> None:
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise RuntimeError("显存不足")

    monkeypatch.setattr(embedding_utils, "BGEM3EmbeddingFunction", FlakyModel)

    with pytest.raises(RuntimeError):
        embedding_utils.get_bge_m3_ef()
    assert embedding_utils._bge_m3_ef is None, "加载失败仍写进了单例"

    model = embedding_utils.get_bge_m3_ef()
    assert len(attempts) == 2, "失败后无法重试"
    assert model is not None
    assert embedding_utils.get_bge_m3_ef() is model
    assert len(attempts) == 2, "第三次调用又加载了一遍模型"


def test_generate_embeddings_shares_the_same_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """向量生成入口必须走工厂，不能自己 new 一个模型绕过缓存。"""
    built: list[object] = []

    class _Array(list):
        """切片后仍得返回自己这种类型：真实代码是 `indices[a:b].tolist()`，普通 list 切片会挂。"""

        def __getitem__(self, item):
            value = super().__getitem__(item)
            return _Array(value) if isinstance(item, slice) else value

        def tolist(self) -> list:
            return list(self)

    class FakeModel:
        def __init__(self, **kwargs: object) -> None:
            built.append(self)

        def encode_documents(self, texts: list[str]) -> dict:
            count = len(texts)

            class _Sparse:
                """照搬 scipy csr 的访问方式：代码读的是 .indices / .indptr / .data 属性。"""

                def __init__(self) -> None:
                    self.indices = _Array([1, 2] * count)
                    self.indptr = _Array(list(range(0, 2 * count + 1, 2)))
                    self.data = _Array([0.5, 0.6] * count)

            return {
                "dense": [_Array([0.1, 0.2]) for _ in texts],
                "sparse": _Sparse(),
            }

    monkeypatch.setattr(embedding_utils, "BGEM3EmbeddingFunction", FakeModel)

    first = embedding_utils.generate_embeddings(["甲"])
    second = embedding_utils.generate_embeddings(["乙"])

    assert len(first["dense"]) == 1 and len(second["sparse"]) == 1
    assert len(built) == 1, "generate_embeddings 绕过了单例工厂"


# ---------------------------------------------------------------- 结构静态锁
# 动态用例能抓住“没加锁”和“没快路径”，但抓不住下面两种“看着对”的写法：
# 在函数里新建锁（每次调用一把新锁 = 完全没有互斥），以及锁内漏掉二次检查。


def _function_tree(name: str) -> ast.FunctionDef:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"模块里没有函数 {name}")


def _is_none_guard(node: ast.AST, target: str) -> bool:
    """识别 `if target is None:` / `if target is not None:` 两种判空守卫。"""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    comparator = test.comparators[0]
    return (
        isinstance(test.left, ast.Name)
        and test.left.id == target
        and isinstance(comparator, ast.Constant)
        and comparator.value is None
        and isinstance(test.ops[0], (ast.Is, ast.IsNot))
    )


def test_lock_is_created_once_at_module_level() -> None:
    """锁必须在模块级建一次；在函数里建锁等于每调用一次换一把新锁。"""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    scopes: list[str] = []

    def walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "Lock"
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "threading"
            ):
                scopes.append(scope)
            walk(child, scope)

    walk(tree, "module")

    assert scopes == ["module"], f"threading.Lock() 出现在 {scopes}，必须只在模块级建一次"


def test_double_check_happens_inside_the_lock() -> None:
    """锁内必须有二次检查：等锁期间别人可能已经加载完了。"""
    function = _function_tree("get_bge_m3_ef")
    with_blocks = [node for node in ast.walk(function) if isinstance(node, ast.With)]
    assert with_blocks, "get_bge_m3_ef 里没有任何 with 锁块"

    checked = [
        child
        for with_node in with_blocks
        for child in with_node.body
        if _is_none_guard(child, "_bge_m3_ef")
    ]
    assert checked, "锁内缺二次检查：排队等锁的线程会各自再加载一份模型"


def test_fast_path_check_precedes_the_lock() -> None:
    """进锁前要先判空，否则每次取模型都要抢锁。"""
    function = _function_tree("get_bge_m3_ef")
    first_with = next((child for child in function.body if isinstance(child, ast.With)), None)
    assert first_with is not None, "锁块不在函数顶层，可能包住的范围不对"

    before = [node for node in function.body if _is_none_guard(node, "_bge_m3_ef")]
    assert before, "缺快路径判空：模型就绪后每次调用仍要过锁"
    assert function.body.index(before[0]) < function.body.index(first_with)
