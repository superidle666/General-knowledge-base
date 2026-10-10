"""提示词组装的预算与可读性回归用例。

_answer_output 节点在拼 prompt 时有两条独立预算：历史对话与参考文档。历史上出过两类事故：
一是预算写成一维，长文档把多轮上下文整段挤掉；二是文档字段名对不上，拼接时直接 TypeError
导致答案为空。这里只锁"预算受控 + 关键字段进 prompt + 不因缺字段崩溃"，不锁定具体文案。
"""

from __future__ import annotations

import ast
import logging
import re

from processor.query_processor.nodes.node_answer_output import (
    IMAGE_ALT_VETO_DISABLE_ENV,
    MAX_CONTEXT_CHARS,
    MAX_HISTORY_CHARS,
    STATIC_REPLY_DELAY_SECONDS,
    NodeAnswerOutput,
)
from tests.graph_introspection import PROJECT_ROOT

ANSWER_NODE_PATH = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_answer_output.py"


def _answer_node_ast() -> ast.Module:
    """答案节点的语法树（只读源码，不执行它）：静态守卫专用。"""
    return ast.parse(ANSWER_NODE_PATH.read_text(encoding="utf-8"))


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
    """多轮历史同样要受控，长回答不能把预算吃满，且留下的必须是靠后的轮次。"""
    node = NodeAnswerOutput()
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "text": f"第{index}轮：" + "设备维护记录。" * 120}
        for index in range(20)
    ]
    history_str, _ = node._format_chat_history(history, MAX_HISTORY_CHARS)
    assert len(history_str) <= MAX_HISTORY_CHARS, f"历史对话长度 {len(history_str)} 超出预算 {MAX_HISTORY_CHARS}"
    assert "第19轮" in history_str, "预算截断后丢失了最新一轮"
    assert "第0轮" not in history_str, "预算截断后保留了最早一轮——截断方向又反了"


def test_history_truncation_keeps_most_recent_in_chronological_order() -> None:
    """截断方向是追问体验的生命线：必须丢旧留新，且输出仍按时间正序。"""
    node = NodeAnswerOutput()
    # 8 条、单条约 580 字，总预算 2400 只装得下靠后的 4 条
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "text": f"第{index}问：" + "内容。" * 190}
        for index in range(8)
    ]
    history_str, _ = node._format_chat_history(history, 2400)
    assert len(history_str) <= 2400
    assert "第7问" in history_str and "第6问" in history_str, "最新的轮次必须保留"
    assert "第0问" not in history_str and "第1问" not in history_str, "最早的轮次应被丢弃"
    # 收集时倒序、输出时必须翻转回时间正序，否则模型读到的是倒放的对话
    assert history_str.index("第4问") < history_str.index("第5问") < history_str.index("第6问") < history_str.index("第7问")


def test_single_oversized_latest_message_is_truncated_not_dropped() -> None:
    """最近一条就超预算时截断保留，不能空切——丢了最新一轮比溢出更伤追问。"""
    node = NodeAnswerOutput()
    history = [
        {"role": "user", "text": "旧问题"},
        {"role": "assistant", "text": "最新回答：" + "很长" * 2000},
    ]
    history_str, _ = node._format_chat_history(history, 300)
    assert history_str.startswith("助手: 最新回答："), f"最新一条被整条丢弃了：{history_str!r}"
    assert len(history_str) <= 300
    assert "旧问题" not in history_str


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


# ---------------------------------------------------------------------------
# R10：文档预算装不下时按条跳过，而不是整段收尾
# 碎片合并（R11）恰好会造出“一条特别长”的块，旧 break 写法能让它把后面本可装下的
# 短答案块整体挡掉，而 sources 与 prompt 同源，被挡掉的块连引用都不展示。
# ---------------------------------------------------------------------------

def _budget() -> int:
    return MAX_CONTEXT_CHARS - MAX_HISTORY_CHARS


def _huge(*, chunk_id: int = 1, title: str = "合并章节", extra: int = 500) -> dict:
    """一条本身就超过整段文档预算的块（合并三片后的典型形状）。"""
    return {
        "content": f"{title}的合并正文：" + "啊" * (_budget() + extra),
        "source": "local",
        "chunk_id": chunk_id,
        "title": title,
        "score": 0.9,
    }


def _small(index: int, *, score: float = 0.5) -> dict:
    return {
        "content": f"短答案{index}：断电后静置十分钟再拆护板。",
        "source": "local",
        "chunk_id": 100 + index,
        "title": f"短章节{index}",
        "score": score,
    }


def test_oversized_doc_does_not_block_later_short_docs() -> None:
    node = NodeAnswerOutput()
    huge = _huge()
    assert len(huge["content"]) > _budget(), "用例本身得先保证这条真的整段装不下"
    context_str, used_docs, _ = node._select_context_docs([huge, _small(0), _small(1)], _budget())
    assert "短答案0" in context_str and "短答案1" in context_str
    # 超长块被跳过而不是带着半截正文进 prompt：截断会把 ![](url) 从中间切断（前端裂图）
    assert [doc["chunk_id"] for doc in used_docs] == [100, 101]


def test_budget_skipped_docs_never_reach_the_source_list() -> None:
    """同源口径不能因为改成跳过而回退：模型没读过的块，就不能出现在用户面前。"""
    node = NodeAnswerOutput()
    state = _state(reranked_docs=[_huge(), _small(0)])
    node._step_2_construct_prompt(state)
    assert [source["chunk_id"] for source in state["sources"]] == [100]


def test_skipping_keeps_the_total_context_within_budget() -> None:
    """把既有"停止追加"用例的语义升级为"总长度受控"：交错尺寸也不许溢出。"""
    node = NodeAnswerOutput()
    docs: list[dict] = []
    for index in range(4):
        docs.append(_huge(chunk_id=index, title=f"合并章节{index}"))
        docs.append(_small(index))
    context_str, used_docs, remaining = node._select_context_docs(docs, _budget())
    assert len(context_str) <= _budget(), f"参考内容长度 {len(context_str)} 超出预算 {_budget()}"
    assert remaining >= 0
    # 旧的 break 在这里一条都装不下（第一条就是超长块），跳过策略才能留下这 4 条
    assert [doc["chunk_id"] for doc in used_docs] == [100, 101, 102, 103]


def test_skipped_docs_are_logged_with_index_and_size(caplog) -> None:
    """这类丢弃过去完全不可见，只能从"答案少了点东西"反推；现在必须能从日志里查到是谁。"""
    node = NodeAnswerOutput()
    huge = _huge()
    with caplog.at_level(logging.DEBUG, logger="kb"):
        node._select_context_docs([huge, _small(0)], _budget())
    text = "\n".join(record.getMessage() for record in caplog.records if record.name == "kb")
    assert "跳过 1 条" in text
    pairs = re.findall(r"\((\d+), (\d+)\)", text)
    assert pairs, f"日志没给出被跳过条目的序号与长度：{text}"
    index, entry_len = (int(value) for value in pairs[0])
    assert index == 1, "记录的必须是它在候选里的序号，引用号才不会前移"
    assert entry_len > len(huge["content"]), "记录的长度要含元数据头，否则与预算口径不一致"


def test_all_small_docs_match_the_previous_output_exactly() -> None:
    """反向回归：一条都不超预算时，拼出的上下文与改动前逐字相等。"""
    node = NodeAnswerOutput()
    docs = _docs(6, size=400)
    context_str, used_docs, remaining = node._select_context_docs(docs, _budget())
    assert [doc["chunk_id"] for doc in used_docs] == [index for index in range(6)]
    assert remaining > 0
    expected = "\n\n".join(
        f"[{index + 1}] [source=local] [chunk_id={index}] [title=章节{index}]"
        f" [score={0.9 - index * 0.01:.4f}]\n{docs[index]['content']}"
        for index in range(len(docs))
    )
    assert context_str == expected


# ---------------------------------------------------------------------------
# 臆造图片剔除：模型会自编 ![外观图](http://...) 写进正文，前端 markdown 渲染就是裂图；
# 白名单（来自召回切片）之外的图片引用要整行删掉，白名单内的不能误删。
# ---------------------------------------------------------------------------

_VERIFY_DOC = {
    "content": "产品外观见下图。\n![真图](http://minio.test/real/ap.png)",
    "source": "local",
    "chunk_id": 9,
}


def test_sanitize_drops_fabricated_image_but_keeps_verified_one() -> None:
    node = NodeAnswerOutput()
    answer = (
        "正面直屏。\n![真图](http://minio.test/real/ap.png)\n"
        "外观图\n![一加 Ace 6T 外观图](http://minio.test/fake/hallucinated.jpg)\n"
        "图片说明：一加 Ace 6T 正面外观展示\n完毕。"
    )
    image_urls = node._extract_images_from_answer(answer, [_VERIFY_DOC])
    assert image_urls == ["http://minio.test/real/ap.png"]  # 臆造 URL 不在召回里，进不了白名单
    cleaned = node._sanitize_unverified_images(answer, set(image_urls))
    assert "http://minio.test/real/ap.png" in cleaned  # 白名单内误删会把真图也屏蔽掉
    assert "hallucinated.jpg" not in cleaned
    assert "图片说明：" not in cleaned  # 用户要求：只贴图不写说明
    assert "完毕。" in cleaned  # 只删图片相关行，正文保留


def test_sanitize_strips_all_images_when_recall_has_none() -> None:
    # 本实体在库里根本没图（线上案例）：模型引用的任何图片都是臆造，全部剥除。
    node = NodeAnswerOutput()
    answer = "参数如下。\n![图](https://evil.test/x.png)"
    image_urls = node._extract_images_from_answer(answer, [{"content": "纯文字，无图。"}])
    assert image_urls == []
    cleaned = node._sanitize_unverified_images(answer, set(image_urls))
    assert "![" not in cleaned and "参数如下。" in cleaned


# ---------------------------------------------------------------------------
# 图片出口的语义否决（线上实测：HAK 180 烫金机的回答底部把手册封面条形码当外观图贴了出来）
# 出处白名单只回答“这张图在本库真的存在吗”，不回答“它是不是在讲这个产品”；
# alt 是导入期多模态模型写的文档事实，拿它做一道零模型调用的确定性否决。
# ---------------------------------------------------------------------------

_BARCODE_URL = "http://minio.test/real/barcode.jpg"
_BARCODE_DOC = {
    "content": (
        "![```markdownHAK 180 烫金机产品安全手册封面条形码（含型号 D01WD7001-00 及 SCHN 标识）```]"
        f"({_BARCODE_URL})\nD01WD7001-00"
    ),
    "source": "local",
    "chunk_id": 11,
}


def test_barcode_image_is_vetoed_even_when_the_model_calls_it_appearance() -> None:
    """判据取切片里那份 alt，不取模型自己写的描述：错配时模型完全可能把 alt 改成“外观图”。"""
    node = NodeAnswerOutput()
    answer = f"是的，有烫金机。\n![HAK 180 烫金机外观图]({_BARCODE_URL})"

    assert node._extract_images_from_answer(answer, [_BARCODE_DOC], "有烫金机吗") == []


def test_vetoed_image_is_also_stripped_from_the_answer_body() -> None:
    """否决要同时作用到前端可见的正文：只进不了 image_urls 不够，正文里的 markdown 仍会被渲染。"""
    node = NodeAnswerOutput()
    answer = f"是的，有烫金机。\n![外观图]({_BARCODE_URL})\n如需详情请访问官网。"
    image_urls = node._extract_images_from_answer(answer, [_BARCODE_DOC], "有烫金机吗")
    cleaned = node._sanitize_unverified_images(answer, set(image_urls))

    assert image_urls == []
    assert "![" not in cleaned and _BARCODE_URL not in cleaned
    assert "是的，有烫金机。" in cleaned and "如需详情请访问官网。" in cleaned


def test_the_veto_yields_when_the_user_is_asking_about_that_very_thing() -> None:
    """本轮就在问条码/铭牌时，这张图正好就是答案：不能一律地屏蔽标识图。"""
    node = NodeAnswerOutput()
    answer = f"铭牌位置见下图。\n![封面条形码]({_BARCODE_URL})"

    assert node._extract_images_from_answer(answer, [_BARCODE_DOC], "设备条形码在哪里看") == [_BARCODE_URL]


def test_image_without_any_alt_is_kept() -> None:
    """fail-open：旧集合/无摘要图拿不到语义依据，不能因此屏蔽真图（与宁查勿否同向）。"""
    node = NodeAnswerOutput()
    doc = {"content": "面板图。\n![](http://minio.test/real/panel.png)", "source": "local", "chunk_id": 12}
    answer = "面板如下图。\n![面板图](http://minio.test/real/panel.png)"

    assert node._extract_images_from_answer(answer, [doc], "面板上有哪些按键") == [
        "http://minio.test/real/panel.png"
    ]


def test_the_veto_can_be_switched_back_to_the_old_behaviour(monkeypatch) -> None:
    """与 DRIFT_GUARD_DISABLE 同族：设 IMAGE_ALT_VETO_DISABLE=1 退回“只看 URL 出处”。"""
    monkeypatch.setenv(IMAGE_ALT_VETO_DISABLE_ENV, "1")
    node = NodeAnswerOutput()
    answer = f"是的，有烫金机。\n![外观图]({_BARCODE_URL})"

    assert node._extract_images_from_answer(answer, [_BARCODE_DOC], "有烫金机吗") == [_BARCODE_URL]


def test_the_answer_node_feeds_the_current_turn_text_to_the_veto() -> None:
    """接线守卫：判定“本轮在问什么”只吃本轮原文（R2），不能拿回填过的检索词去豁免。"""
    call = next(
        node for node in ast.walk(_answer_node_ast())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_extract_images_from_answer"
    )
    assert len(call.args) == 3, "必须显式传入本轮原文，否则“用户在问条码”这道豁免拿不到输入"
    assert "query_entity_text" in ast.unparse(call.args[2])


def test_sanitize_leaves_plain_links_untouched() -> None:
    # 只处理图片语法（![..]）；普通 markdown 链接/正文不能被误删。
    node = NodeAnswerOutput()
    answer = "详见[官网](https://site.test/manual)，价格 1999 元。"
    assert node._sanitize_unverified_images(answer, set()) == answer


def test_preset_static_answer_pushes_after_delay(monkeypatch) -> None:
    # 闲聊话术等预置短答：流式推送前先 sleep，避免瞬时送达像占位符。
    import types

    node = NodeAnswerOutput()
    slept: list[float] = []
    pushed: list[tuple[str, dict]] = []
    # 替换节点模块命名空间里的 time 引用（teardown 自动还原），不碰全局 time 模块。
    fake_time = types.SimpleNamespace(sleep=slept.append)
    monkeypatch.setattr(
        "processor.query_processor.nodes.node_answer_output.time", fake_time
    )
    monkeypatch.setattr(
        "processor.query_processor.nodes.node_answer_output.push_to_session",
        lambda session_id, event, data: pushed.append((event, data)),
    )
    monkeypatch.setattr(
        "processor.query_processor.nodes.node_answer_output.save_chat_message", lambda **kwargs: None
    )
    state = _state(session_id="s-1", answer="你好！我是本知识库的智能助手。", is_stream=True, persist_history=False)
    result = node.process(state)
    assert slept == [STATIC_REPLY_DELAY_SECONDS]
    assert pushed and pushed[0][0] == "delta"
    assert result["image_urls"] == []
    # 非流式路径不拖慢接口：不经过 sleep 分支（上面只验流式，这里补验预置答案不被图片清洗破坏）
    assert result["answer"].startswith("你好")
