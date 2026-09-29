"""
大语言模型客户端工具。
utils/llm_utils.py
按模型和输出模式缓存 ChatOpenAI 客户端，避免重复初始化连接。
"""
import hashlib

from langchain_openai import ChatOpenAI

from config.lm_config import lm_config
from utils.runtime_settings import get_runtime_setting

_llm_client_cache = {}


def get_llm_client(
    model: str | None = None,
    json_mode: bool = False,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> ChatOpenAI:
    """
    获取 LangChain ChatOpenAI 客户端实例
    - model: 允许不同节点使用不同模型
    - json_mode: True 时要求输出 JSON
    - timeout: 单次请求超时秒数；同步检索链路用它兜住外部调用，避免卡住整个问答
    - max_retries: 失败重试次数；与 timeout 配合限定最坏情况耗时
    """
    # 库配置优先、env 兜底（P0-1）：管理端保存的聊天模型三项在此生效，
    # 读不到快照（未配置/Redis 故障）时 get_runtime_setting 返回 None，行为与接线前一致。
    api_key = get_runtime_setting("model.chat_api_key") or lm_config.api_key
    base_url = get_runtime_setting("model.chat_base_url") or lm_config.base_url
    m = model or get_runtime_setting("model.chat_model") or lm_config.llm_model
    # 缓存 key 必须纳入实际生效的凭证：只含模型名的话，改了地址/密钥后旧 client 永远命中缓存。
    credential_digest = hashlib.sha1(f"{api_key}|{base_url}".encode("utf-8")).hexdigest()
    key = (m, json_mode, timeout, max_retries, credential_digest)
    if key in _llm_client_cache:
        return _llm_client_cache[key]

    extra_body = {"enable_thinking": False}

    model_kwargs: dict = {}
    if json_mode:
        model_kwargs["response_format"] = {"type": "json_object"}

    client_kwargs: dict = {
        "model": m,
        "temperature": lm_config.llm_temperature,
        "api_key": api_key,
        "base_url": base_url,
        "extra_body": extra_body,
        "model_kwargs": model_kwargs,
    }
    if timeout is not None:
        client_kwargs["timeout"] = timeout
    if max_retries is not None:
        client_kwargs["max_retries"] = max_retries

    client = ChatOpenAI(**client_kwargs)
    _llm_client_cache[key] = client
    return client
