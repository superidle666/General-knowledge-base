"""
P0-1 运行时配置快照测试：发布/读取、env 兜底、stale-while-error 与 LLM 客户端接线。

锁住四件事：
1. 保存后快照能被同步上下文读到，快照缺失/Redis 故障时 get_runtime_setting 返回 None（调用方回落 env）；
2. Redis 里不存明文（发布路径必须过加密）；
3. 拿到旧快照后 Redis 故障继续沿用旧值（stale-while-error），配置读取永不抛异常拖垮主链路；
4. get_llm_client 用库配置覆盖 env，且凭证变化会让客户端缓存失效。

conftest 的 autouse fixture 已把 _get_redis_client 换成抛错；本文件用例自行
monkeypatch _fetch_snapshot / _get_redis_client 来构造快照场景。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.core.system_settings import SETTING_SPECS
from utils import llm_utils, runtime_settings


class FakeRedis:
    """内存版 Redis：只实现快照路径用到的 set/get。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)


class FakeResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def scalars(self) -> "FakeResult":
        return self

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class FakeSession:
    """只支持 publish_settings_snapshot 那一条 select 的假会话。"""

    def __init__(self, rows: dict[str, str]) -> None:
        self._rows = [
            SimpleNamespace(setting_key=key, encrypted_value=value) for key, value in rows.items()
        ]

    async def execute(self, _stmt: Any) -> FakeResult:
        return FakeResult(self._rows)


@pytest.fixture()
def identity_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    """让加解密退化为透传，测试不依赖 SETTINGS_ENCRYPTION_KEY 是否配置。"""
    monkeypatch.setattr(runtime_settings, "encrypt_value", lambda value: value)
    monkeypatch.setattr(runtime_settings, "decrypt_value", lambda value: value)


@pytest.mark.unit
async def test_publish_then_read_roundtrip(monkeypatch: pytest.MonkeyPatch, identity_codec: None) -> None:
    fake = FakeRedis()
    monkeypatch.setattr(runtime_settings, "_get_redis_client", lambda: fake)
    await runtime_settings.publish_settings_snapshot(
        FakeSession({"model.chat_model": "qwen-max", "model.vl_model": "qwen3-vl-plus"})
    )

    assert runtime_settings.get_runtime_setting("model.chat_model") == "qwen-max"
    assert runtime_settings.get_runtime_setting("model.vl_model") == "qwen3-vl-plus"
    assert runtime_settings.get_runtime_setting("embedding.model") is None


@pytest.mark.unit
async def test_snapshot_never_stores_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    """落 Redis 的必须是加密后的 token，明文 JSON 不允许直接出现在存储里。"""
    fake = FakeRedis()
    monkeypatch.setattr(runtime_settings, "_get_redis_client", lambda: fake)
    monkeypatch.setattr(runtime_settings, "encrypt_value", lambda value: "ENC:" + value)
    monkeypatch.setattr(runtime_settings, "decrypt_value", lambda value: value[4:] if value.startswith("ENC:") else value)

    await runtime_settings.publish_settings_snapshot(FakeSession({"model.chat_api_key": "sk-secret"}))

    stored = fake.store[runtime_settings.SNAPSHOT_REDIS_KEY]
    assert stored.startswith("ENC:"), "快照未经加密就写入了 Redis"
    assert runtime_settings.get_runtime_setting("model.chat_api_key") == "sk-secret"


@pytest.mark.unit
def test_missing_snapshot_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis 可达但没有快照：返回 None，调用方回落 env。"""
    monkeypatch.setattr(runtime_settings, "_get_redis_client", lambda: FakeRedis())

    assert runtime_settings.get_runtime_setting("model.chat_model") is None


@pytest.mark.unit
def test_stale_while_error_keeps_last_good_snapshot(monkeypatch: pytest.MonkeyPatch, identity_codec: None) -> None:
    """先读成功、后 Redis 故障：继续沿用旧快照，不抛异常也不回退成 None 造成行为翻转。"""
    fetches: list[dict[str, str] | None] = [{"model.chat_model": "qwen-max"}, None]
    monkeypatch.setattr(runtime_settings, "_fetch_snapshot", lambda: fetches.pop(0))

    assert runtime_settings.get_runtime_setting("model.chat_model") == "qwen-max"
    # 强制过期触发重读；第二次 fetch 返回 None（故障）→ 旧值仍在用。
    runtime_settings.invalidate_runtime_settings_cache()
    assert runtime_settings.get_runtime_setting("model.chat_model") == "qwen-max"


@pytest.mark.unit
def test_invalidate_forces_refetch(monkeypatch: pytest.MonkeyPatch, identity_codec: None) -> None:
    """保存后本进程即时生效：invalidate 之后的下一次读取必须重新拉快照。"""
    fetches: list[dict[str, str] | None] = [{"model.chat_model": "v1"}, {"model.chat_model": "v2"}]
    monkeypatch.setattr(runtime_settings, "_fetch_snapshot", lambda: fetches.pop(0))

    assert runtime_settings.get_runtime_setting("model.chat_model") == "v1"
    assert runtime_settings.get_runtime_setting("model.chat_model") == "v1", "TTL 内不应重新拉取"
    runtime_settings.invalidate_runtime_settings_cache()
    assert runtime_settings.get_runtime_setting("model.chat_model") == "v2"


class RecordingChatModel:
    instances: list["RecordingChatModel"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        RecordingChatModel.instances.append(self)


@pytest.mark.unit
def test_llm_client_prefers_runtime_settings_and_invalidates_on_credential_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_llm_client：三项库配置覆盖 env；同配置命中缓存；改凭证后必须新建客户端。"""
    RecordingChatModel.instances = []
    monkeypatch.setattr(llm_utils, "_llm_client_cache", {})
    monkeypatch.setattr(llm_utils, "ChatOpenAI", RecordingChatModel)
    settings_map = {
        "model.chat_api_key": "sk-from-db",
        "model.chat_base_url": "https://db.example/v1",
        "model.chat_model": "qwen-db",
    }
    monkeypatch.setattr(llm_utils, "get_runtime_setting", lambda key: settings_map.get(key))

    client = llm_utils.get_llm_client()
    assert client.kwargs["model"] == "qwen-db"
    assert client.kwargs["api_key"] == "sk-from-db"
    assert client.kwargs["base_url"] == "https://db.example/v1"

    # 同配置二次获取：命中缓存，不再新建。
    assert llm_utils.get_llm_client() is client
    assert len(RecordingChatModel.instances) == 1

    # 只改密钥：模型名没变，若缓存 key 不含凭证则抓不到这里。
    settings_map["model.chat_api_key"] = "sk-rotated"
    rotated = llm_utils.get_llm_client()
    assert rotated is not client, "凭证变化后仍命中旧客户端缓存"
    assert rotated.kwargs["api_key"] == "sk-rotated"

    # 调用方显式传模型名时，覆盖仍生效（item_model 等按调用点指定）。
    explicit = llm_utils.get_llm_client(model="qwen-item")
    assert explicit.kwargs["model"] == "qwen-item"


@pytest.mark.unit
def test_llm_client_falls_back_to_env_without_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """无快照（conftest 默认已探成 Redis 不可用 → None）：行为与接线前完全一致。"""
    RecordingChatModel.instances = []
    monkeypatch.setattr(llm_utils, "_llm_client_cache", {})
    monkeypatch.setattr(llm_utils, "ChatOpenAI", RecordingChatModel)

    client = llm_utils.get_llm_client()
    from config.lm_config import lm_config

    assert client.kwargs["model"] == lm_config.llm_model
    assert client.kwargs["api_key"] == lm_config.api_key
    assert client.kwargs["base_url"] == lm_config.base_url


@pytest.mark.unit
def test_vl_model_registered_in_settings_specs() -> None:
    """视觉模型必须注册为 model 分类的非敏感项，管理端才会展示并可保存。"""
    spec = next((item for item in SETTING_SPECS if item["key"] == "model.vl_model"), None)
    assert spec is not None, "model.vl_model 未注册，设置页看不到视觉模型"
    assert spec["category"] == "model"
    assert spec["sensitive"] is False


@pytest.mark.unit
def test_registry_scope_converged_to_model_services() -> None:
    """2026-09-29 范围收敛：只保留模型服务，切片/Milvus/站点/假 embedding API 项全部下线。"""
    keys = {item["key"] for item in SETTING_SPECS}
    removed = {
        "document.chunk_size", "document.chunk_overlap", "document.max_file_size_mb",
        "milvus.url", "milvus.collection", "site.name", "site.default_library_code",
        "embedding.api_key", "embedding.base_url", "embedding.model", "embedding.dimension",
    }
    assert not (keys & removed), f"已下线的配置仍在注册表：{keys & removed}"
    assert {"model.chat_api_key", "model.chat_base_url", "model.chat_model", "model.vl_model"} <= keys
    assert {"rerank.text_model", "rerank.text_instruct"} <= keys
    assert {"embedding.model_path", "embedding.device", "embedding.fp16"} <= keys
    # 所有项统一归入 model 分组，前端只展示「模型服务」
    assert all(item["category"] == "model" for item in SETTING_SPECS)


class RecordingTextReRank:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        documents = kwargs["documents"]
        return {
            "status_code": 200,
            "output": {"results": [{"index": i, "relevance_score": 0.5} for i in range(len(documents))]},
        }


@pytest.mark.unit
def test_rerank_prefers_runtime_settings_and_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """重排每次按需调用：库配置保存即生效；无快照时行为与接线前一致。"""
    from config.reranker_config import reranker_config
    from utils import reranker_http_utils

    fake = RecordingTextReRank()
    monkeypatch.setattr(reranker_http_utils.dashscope, "TextReRank", fake)
    settings_map = {
        "model.chat_api_key": "sk-rerank-db",
        "rerank.text_model": "qwen-rerank-db",
        "rerank.text_instruct": "按型号匹配",
    }
    monkeypatch.setattr(reranker_http_utils, "get_runtime_setting", lambda key: settings_map.get(key))

    reranker_http_utils.rerank_documents("查询", ["文档A", "文档B"])

    assert fake.calls[0]["model"] == "qwen-rerank-db"
    assert fake.calls[0]["instruct"] == "按型号匹配"
    assert reranker_http_utils.dashscope.api_key == "sk-rerank-db"

    # 无快照→回落 env：重新把 get_runtime_setting 换成“永远 None”。
    monkeypatch.setattr(reranker_http_utils, "get_runtime_setting", lambda key: None)
    reranker_http_utils.rerank_documents("查询", ["文档A"])

    assert fake.calls[1]["model"] == reranker_config.text_rerank_model
    assert fake.calls[1]["instruct"] == reranker_config.text_rerank_instruct


class RecordingBGE:
    instances: list["RecordingBGE"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        RecordingBGE.instances.append(self)


@pytest.mark.unit
def test_embedding_first_load_prefers_runtime_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """BGE-M3 首次加载读库配置；fp16 的 '0' 必须被解析成 False，不能当非空字符串当真。"""
    from utils import embedding_utils

    RecordingBGE.instances = []
    monkeypatch.setattr(embedding_utils, "BGEM3EmbeddingFunction", RecordingBGE)
    monkeypatch.setattr(embedding_utils, "_bge_m3_ef", None)
    settings_map = {
        "embedding.model_path": "E:/models/bge-m3-db",
        "embedding.device": "cuda:1",
        "embedding.fp16": "0",
    }
    monkeypatch.setattr(embedding_utils, "get_runtime_setting", lambda key: settings_map.get(key))

    embedding_utils.get_bge_m3_ef()

    built = RecordingBGE.instances[0].kwargs
    assert built["model_name"] == "E:/models/bge-m3-db"
    assert built["device"] == "cuda:1"
    assert built["use_fp16"] is False


@pytest.mark.unit
async def test_sync_system_settings_deletes_unregistered_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """启动同步要删掉已下线配置的孤儿行，避免列表接口继续暴露 PATCH 必 404 的假入口。"""
    from types import SimpleNamespace

    from backend.app.core import system_settings as ss

    monkeypatch.setattr(ss, "encrypt_value", lambda value: value)
    registered = SimpleNamespace(
        setting_key="model.chat_model", display_name="", category="", is_sensitive=False,
        description="", encrypted_value="x",
    )
    orphan = SimpleNamespace(
        setting_key="document.chunk_size", display_name="", category="", is_sensitive=False,
        description="", encrypted_value="x",
    )
    deleted: list[Any] = []
    added: list[Any] = []

    class FakeSyncSession:
        async def execute(self, _stmt: Any) -> Any:
            return SimpleNamespace(scalars=lambda: [registered, orphan])

        def add(self, item: Any) -> None:
            added.append(item)

        def delete(self, item: Any) -> None:
            deleted.append(item)

        async def commit(self) -> None:
            return None

    await ss.sync_system_settings(FakeSyncSession())

    assert deleted == [orphan], "孤儿行未被清理（或误删了注册项）"
    # 其余 8 个注册项缺失 → 补种；已存在的 model.chat_model 不重复补。
    assert len(added) == len(ss.SETTING_SPECS) - 1
