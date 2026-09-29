"""
文本重排序服务工具。
utils/reranker_http_utils.py
调用外部重排序模型，为召回文档生成相关性分数。
"""

import math

import dashscope
from dotenv import load_dotenv
from config.reranker_config import reranker_config
from utils.runtime_settings import get_runtime_setting

load_dotenv()


class RerankError(RuntimeError):
    """重排服务不可用或返回不可信：调用方应回退到召回原序，而不是拿部分/异常分数继续精排。"""


def _extract_results(response: object) -> list:
    """取 output.results；字段缺失或形状不对一律抛错。

    原先这里写的是 .get("results", [])：服务返回缺字段时会变成“一份都没打分”，
    后面的补 0 逻辑把它扮成“这些文档相关性为 0”，属于最难查的那种错。
    dashscope 响应同时支持字典取键与属性取键，两种形态都兼容。
    """
    output = response.get("output") if hasattr(response, "get") else getattr(response, "output", None)
    if output is None:
        raise RerankError("DashScope rerank 响应缺少 output 字段")
    results = output.get("results") if hasattr(output, "get") else getattr(output, "results", None)
    if not isinstance(results, list):
        raise RerankError(f"DashScope rerank 响应 output.results 不是列表：{type(results).__name__}")
    return results


def rerank_documents(query: str, documents: list[str]) -> list[float]:
    """为每篇文档取一个相关性分数；返回与入参严格等长、一一对应。

    “结果不完整 / 下标越界重复 / 分数 NaN”全部抛 RerankError，不静默补 0：补 0 会把被
    服务漏掉的文档排到最后但仍留在候选里，看着像“模型认为它不相关”。
    """
    if not documents:
        return []

    # 库配置优先、env 兜底：重排是每次检索的按需调用，管理端保存即生效；
    # 密钥与聊天模型共用同一个 DashScope 账号（model.chat_api_key）。
    dashscope.api_key = get_runtime_setting("model.chat_api_key") or reranker_config.text_rerank_api_key
    try:
        response = dashscope.TextReRank.call(
            model=get_runtime_setting("rerank.text_model") or reranker_config.text_rerank_model,
            query=query,
            documents=documents,
            top_n=len(documents),
            return_documents=False,
            instruct=get_runtime_setting("rerank.text_instruct") or reranker_config.text_rerank_instruct,
        )
    except RerankError:
        raise
    except Exception as error:  # noqa: BLE001 - SDK 的网络/鉴权异常统一转成本模块异常类型
        raise RerankError(f"DashScope rerank 调用异常：{error}") from error

    status_code = response.get("status_code")
    if status_code != 200:
        message = response.get("message")
        raise RerankError(f"DashScope rerank 调用失败: {message}")

    results = _extract_results(response)
    if len(results) != len(documents):
        raise RerankError(f"DashScope rerank 返回条数与入参不符：文档 {len(documents)} 条、分数 {len(results)} 条")

    scores: list[float | None] = [None] * len(documents)
    for item in results:
        index = item.get("index") if hasattr(item, "get") else None
        raw_score = item.get("relevance_score") if hasattr(item, "get") else None
        try:
            position = int(index)
            score = float(raw_score)
        except (TypeError, ValueError) as error:
            raise RerankError(f"DashScope rerank 返回非法条目：index={index!r} score={raw_score!r}") from error
        if not 0 <= position < len(documents):
            raise RerankError(f"DashScope rerank 返回下标越界：index={index!r}，文档共 {len(documents)} 条")
        if scores[position] is not None:
            raise RerankError(f"DashScope rerank 同一篇文档被重复打分：index={index!r}")
        if math.isnan(score) or math.isinf(score):
            raise RerankError(f"DashScope rerank 返回非有限分数：index={index!r} score={score}")
        scores[position] = score

    missing = [position for position, score in enumerate(scores) if score is None]
    if missing:
        raise RerankError(f"DashScope rerank 缺少这些文档的分数：{missing}")
    return [float(score) for score in scores]
