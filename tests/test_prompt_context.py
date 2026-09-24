"""提示词组装的预算与可读性回归用例。

_answer_output 节点在拼 prompt 时有两条独立预算：历史对话与参考文档。历史上出过两类事故：
一是预算写成一维，长文档把多轮上下文整段挤掉；二是文档字段名对不上，拼接时直接 TypeError
导致答案为空。这里只锁"预算受控 + 关键字段进 prompt + 不因缺字段崩溃"，不锁定具体文案。
"""

from __future__ import annotations

from processor.query_processor.nodes.node_answer_output import (
    MAX_CONTEXT_CHARS,
    MAX_HISTORY_CHARS,
    NodeAnswerOutput,
)


def _docs(count: int, *, size: int = 800, **overrides: object) -> list[dict]:
    docs = []
    for index in range(count):
        doc = {
            "content": f"第{index}条参考内容：" + ("检修流程说明。" * (size // 7 + 1)),
            "source": "local",
            "chunk_id": index,
            "title": f"章节{index}",
            "score": 0.9 - index * 0.01,
        }
        doc.update(overrides)
        docs.append(doc)
    return docs


def _state(**overrides: object) -> dict:
    state = {
        "original_query": "HAK 180 烫金机怎么清洁加热板？",
        "rewritten_query": "HAK 180 烫金机加热板清洁方法",
        "item_names": ["HAK 180 烫金机"],
        "history": [],
        "reranked_docs": [],
        "top_k": 5,
    }
    state.update(overrides)
    return state


def test_doc_budget_and_history_budget_are_within_total_limit() -> None:
    """两个预算相加不能超过总上限，否则长历史会挤掉参考内容或反之。"""
    assert MAX_HISTORY_CHARS < MAX_CONTEXT_CHARS
    assert MAX_CONTEXT_CHARS - MAX_HISTORY_CHARS > 0


def test_reference_budget_stops_adding_docs_once_exhausted() -> None:
    """参考内容超预算时应停止追加，而不是无限拼长 prompt。"""
    node = NodeAnswerOutput()
    oversized = _docs(15, size=2000)
    context_str, remaining = node._format_reranked_docs(oversized, MAX_CONTEXT_CHARS - MAX_HISTORY_CHARS)
    budget = MAX_CONTEXT_CHARS - MAX_HISTORY_CHARS
    assert len(context_str) <= budget, f"参考内容长度 {len(context_str)} 超出预算 {budget}"
    assert context_str.count("[source=local]") < len(oversized), "预算耗尽后仍在追加全部文档"
    assert remaining < budget


def test_history_budget_keeps_recent_turns_and_respects_limit() -> None:
    """多轮历史同样要受控，长回答不能把预算吃满。"""
    node = NodeAnswerOutput()
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "text": f"第{index}轮：" + "设备维护记录。" * 120}
        for index in range(20)
    ]
    history_str, _ = node._format_chat_history(history, MAX_HISTORY_CHARS)
    assert len(history_str) <= MAX_HISTORY_CHARS, f"历史对话长度 {len(history_str)} 超出预算 {MAX_HISTORY_CHARS}"


def test_prompt_contains_question_item_names_and_reference_marker() -> None:
    """问题、商品名与参考内容必须出现在最终 prompt 里，否则模型无从依据。"""
    node = NodeAnswerOutput()
    prompt = node._step_2_construct_prompt(_state(reranked_docs=_docs(3)))
    assert "HAK 180 烫金机加热板清洁方法" in prompt
    assert "HAK 180 烫金机" in prompt
    assert "检修流程说明" in prompt
    assert len(prompt) <= MAX_CONTEXT_CHARS + 2000, f"prompt 长度 {len(prompt)} 失控"


def test_prompt_survives_docs_missing_optional_fields() -> None:
    """缺 title/url 等可选字段不能抛异常：节点异常会被基类吞掉，最终表现为"答案为空"。"""
    node = NodeAnswerOutput()
    sparse_docs = [{"content": "仅有正文，没有任何元数据。"}]
    prompt = node._step_2_construct_prompt(_state(reranked_docs=sparse_docs))
    assert "仅有正文，没有任何元数据。" in prompt


def test_prompt_never_carries_literal_none_for_missing_fields() -> None:
    """缺失的 url/chunk_id 必须整个标记消失，不能变成 [url=None]。

    真实检索里本地切片永远没有 url，联网来源又没有 chunk_id；
    一旦以 "None" 字面量入 prompt，模型会把它当真实链接写进回答，来源表观上“正常”但完全不可溯源。
    """
    node = NodeAnswerOutput()
    docs = [
        {"content": "本地知识库正文内容，长度足够不被阈值过滤。" * 3, "source": "local", "chunk_id": None, "url": None, "title": None},
        {"content": "联网摘要内容，长度足够不被阈值过滤。" * 3, "source": "web", "chunk_id": None, "url": "https://a.test", "title": "页面标题"},
    ]
    context_str, _ = node._format_reranked_docs(docs, MAX_CONTEXT_CHARS - MAX_HISTORY_CHARS)
    assert "=None" not in context_str, f"prompt 里泄漏了 None 字面量：{context_str}"
    # 有值的字段仍然要完整透传，否则修污染的同时把真实来源也修掉了
    assert "[source=local]" in context_str and "[source=web]" in context_str
    assert "[url=https://a.test]" in context_str and "[title=页面标题]" in context_str


def test_docs_without_content_are_skipped_instead_of_crashing() -> None:
    """content 缺失不能抛 TypeError：该异常会被基类吞掉，外部只表现为“答案为空”。"""
    node = NodeAnswerOutput()
    docs = [
        {"content": None, "source": "local", "chunk_id": 1},
        {"content": "   ", "source": "local", "chunk_id": 2},
        {"content": "唯一有正文的参考内容。" * 10, "source": "local", "chunk_id": 3},
    ]
    context_str, _ = node._format_reranked_docs(docs, MAX_CONTEXT_CHARS - MAX_HISTORY_CHARS)
    assert context_str.count("[source=local]") == 1
    # 序号按入参位置计算，跳过空正文不能把后续来源的引用号前移（否则与来源面板对不上）
    assert context_str.startswith("[3] [source=local] [chunk_id=3]")
    assert "唯一有正文的参考内容。" in context_str


def test_prompt_without_reference_content_still_builds() -> None:
    """零命中时也要能出 prompt（走"无参考内容"分支），不能拼出空洞模板。"""
    node = NodeAnswerOutput()
    prompt = node._step_2_construct_prompt(_state(reranked_docs=[]))
    assert "无参考内容" in prompt
    assert "HAK 180 烫金机加热板清洁方法" in prompt
