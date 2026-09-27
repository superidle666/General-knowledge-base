"""
RAGAS 检索问答质量评估脚本。

@FilePath: eval/ragas_eval.py
@Date: 2026-09-26
@Description:
    读取 eval/qa.csv（question=问题，ground_truth=标准答案），逐题进程内直调
    查询工作流（与 backend/app/api/query.py 同一入口、同一套状态参数，library_id=2
    "网络设备资料"），拿到系统答案与生成时实际使用的检索上下文（reranked_docs）后，用 ragas 计算 5 项指标：
    Faithfulness / Answer Relevancy / Context Precision / Context Recall /
    Answer Correctness，结果写入 eval/qa_result.csv（UTF-8 with BOM）。

用法（必须使用项目虚拟环境解释器，且在工作区根目录下运行）：
    .venv\\Scripts\\python.exe eval\\ragas_eval.py
    可选参数：--library-id 2 --top-k 5 --limit 3（只跑前 3 题冒烟）
              --context-topk 3（默认；额外算"只供前 N 块 context"的 top-N 口径，0=关）

为何双口径：全量口径下 faithfulness/context_recall 常被"喂很多块"撑到接近 1.0、失去
区分度。与 top-N 口径并排对比才能分辨"真排好了"还是"堆料撑高"。

依赖的外部服务：Milvus / MySQL / LLM API（与问答后端一致）；BGE-M3 首次加载需数十秒。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
import types
from dataclasses import dataclass, field
from pathlib import Path

# 让 "python eval/ragas_eval.py" 直跑时也能 import 项目根目录下的 backend/config/processor 等包
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

# ragas 0.3.9/0.4.3 在导入期无条件 `from langchain_community.chat_models.vertexai
# import ChatVertexAI`，而新版 langchain-community 已删除该模块（上游 bug，见
# ragas issue #2745）。本项目不使用 VertexAI，该类只出现在 isinstance 检查里，
# 因此注册一个空壳模块垫片绕过，避免为它引入整套 Google Cloud 依赖。
if "langchain_community.chat_models.vertexai" not in sys.modules:
    _vertexai_shim = types.ModuleType("langchain_community.chat_models.vertexai")
    _vertexai_shim.ChatVertexAI = type("ChatVertexAI", (), {})  # type: ignore[attr-defined]
    sys.modules["langchain_community.chat_models.vertexai"] = _vertexai_shim

from ragas.dataset_schema import SingleTurnSample  # noqa: E402
from ragas.embeddings.base import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics import (  # noqa: E402
    AnswerCorrectness,
    AnswerRelevancy,
    ContextPrecision,
    ContextRecall,
    Faithfulness,
)
from ragas.run_config import RunConfig  # noqa: E402

QA_CSV_PATH = WORKSPACE_ROOT / "eval" / "qa.csv"
RESULT_CSV_PATH = WORKSPACE_ROOT / "eval" / "qa_result.csv"

# 每题评估失败的指标以空单元格落盘（区别于 0.0 这种合法最低分）。
METRIC_NAMES = [
    "faithfulness", "answer_relevancy", "context_precision", "context_recall",
    "answer_correctness",
]

# 只有依赖 context 的指标才分"全量 / top-N"两种口径；answer_relevancy（答案↔问题）
# 与 answer_correctness（答案↔参考）不看 context，两口径结果相同，不重复计算。
CONTEXT_DEPENDENT_METRICS = ["faithfulness", "context_precision", "context_recall"]
# top-N 口径的独立结果列，与全量列并排；列名 = 指标名 + "_topk"。
TOPK_METRIC_NAMES = [f"{name}_topk" for name in CONTEXT_DEPENDENT_METRICS]

# 结果 CSV 列：前 4 列问答快照，接着 5 列全量口径指标，最后 3 列 top-N 口径指标。
# 列名沿用需求原文（含 "awnser" 这一既定拼写），保证与下游对账脚本一致。
RESULT_COLUMNS = [
    "question", "awnser", "context", "ground_truth",
    *METRIC_NAMES,
    *TOPK_METRIC_NAMES,
]


def _preview(text: str, limit: int) -> str:
    """把可能很长的正文压成单行预览，供运行日志查看又不刷屏。

    :param text: 原文（检索切片 / 模型答案，可能含换行的 markdown）
    :param limit: 保留的最大字符数；<=0 表示不截断（打印全文）
    :return: 折叠空白后的预览；被截断时末尾追加省略号与剩余字符数
    """
    collapsed = " ".join(str(text or "").split())
    if limit <= 0 or len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]} …（截断，共 {len(collapsed)} 字）"


def _log_qa_detail(row: EvalRow, idx: int, total: int, preview_chars: int) -> None:
    """把单题的「问题 / 检索上下文 / 模型答案」打印到控制台，便于运行时核对链路。"""
    print(f"\n----- 第 {idx}/{total} 题 -----")
    print(f"[问题] {row.pair.question}")
    print(f"[标准答案] {row.pair.ground_truth}")
    if row.error:
        print(f"[检索上下文] 问答链路失败，无上下文：{row.error}")
        return
    print(f"[检索上下文/生成集 reranked_docs] 命中 {len(row.contexts)} 个切片：")
    if not row.contexts:
        print("  （无：重排后没有任何可用切片）")
    for i, context in enumerate(row.contexts, 1):
        print(f"  #{i} {_preview(context, preview_chars)}")
    print(f"[模型答案] {_preview(row.answer, preview_chars)}")
    dropped = getattr(row, "dropped_heading_only", 0)
    trace = getattr(row, "rerank_trace", [])
    print(f"[诊断] RRF 候选中被判'纯标题空块'、已在精排前剔除：{dropped} 个")
    if trace:
        print("[诊断] 最终喂模型的 reranked_docs 排序（rank | score | 正文字数 | 来源 | 标题）：")
        for t in trace:
            score = t.get("score")
            score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "  —  "
            print(f"    #{t['rank']:>2}  {score_s}  clen={t['clen']:>4}  [{t.get('source')}]  {t['title']!r}")


@dataclass
class QAPair:
    """一条评估样本。

    Attributes:
        question: 待评估的问题（qa.csv 的 question 列）。
        ground_truth: 人工标注的标准答案（qa.csv 的 ground_truth 列），
            作为 Context Precision/Recall 与 Answer Correctness 的参考。
    """
    question: str
    ground_truth: str


@dataclass
class EvalRow:
    """一条样本跑完整链路后的评估结果行。

    Attributes:
        pair: 原始问答对。
        answer: 系统生成的答案（工作流 state 的 answer 字段）。
        contexts: 生成答案时实际喂给模型的检索上下文正文列表（取自 reranked_docs，
            每个元素一个切片，不拼接），对应 ragas 的 retrieved_contexts。
        scores: 全量口径（context=全部 reranked_docs）下各指标名 -> 分数（0~1）。
        scores_topk: top-N 口径（context 截断到前 N 块）下，仅依赖 context 的指标名 -> 分数；
            用于与全量并排对比，判断指标是否靠"堆料"撑高。以根指标名为键。
        error: 问答链路失败时的错误摘要，成功时为 None。
    """
    pair: QAPair
    answer: str = ""
    contexts: list[str] = field(default_factory=list)
    # scores 必须是真字典而不是 None：step_6 直接 row.scores[name]=… 写回，
    # 之前默认 None 会让"指标算成功了但写回崩在 NoneType 不支持项赋值"。
    scores: dict[str, float] = field(default_factory=dict)
    scores_topk: dict[str, float] = field(default_factory=dict)
    # 排序诊断（--no-eval 单点排查用）：rrf 候选中被 2a 判为纯标题空块并剔除的数量，
    # 以及最终喂模型的 reranked_docs 逐条排序快照（rank/score/正文字数/标题）。
    dropped_heading_only: int = 0
    rerank_trace: list[dict] = field(default_factory=list)
    error: str | None = None


def step_1_read_qa_pairs(csv_path: Path = QA_CSV_PATH) -> list[QAPair]:
    """步骤 1：读取问答对数据集。

    :param csv_path: qa.csv 路径，需含 question / ground_truth 两列表头
    :return: 问答对列表（跳过空 question 的行）
    """
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        pairs = [
            QAPair(question=row["question"].strip(), ground_truth=(row.get("ground_truth") or "").strip())
            for row in reader
            if (row.get("question") or "").strip()
        ]
    print(f"[step_1] 读取 {csv_path.name}: {len(pairs)} 条问答对")
    return pairs


async def step_2_get_library_config(library_id: int) -> dict:
    """步骤 2：从 MySQL 读取知识库配置，复刻查询接口注入工作流的参数。

    与 backend/app/api/query.py 的查询入口保持一致：HyDE 开关由库配置决定、
    实体识别 required 的库要注入本库词表，否则评估链路和生产链路不等价。

    :param library_id: 知识库 ID（本项目为 2「网络设备资料」）
    :return: 含 use_hyde / entity_recognition_mode / entity_vocabulary 的字典
    """
    from sqlalchemy import select

    from backend.app.db.models import Document, Entity, Library
    from backend.app.db.session import SessionLocal

    async with SessionLocal() as session:
        library = await session.get(Library, library_id)
        if library is None or library.deleted_at is not None:
            raise RuntimeError(f"知识库 {library_id} 不存在或已删除")
        if library.status != "active":
            raise RuntimeError(f"知识库 {library_id} 已停用，无法评估")
        config = {
            "library_name": library.name,
            "use_hyde": bool(library.use_hyde),
            "entity_recognition_mode": str(library.entity_recognition_mode or "disabled"),
            "entity_vocabulary": [],
        }
        # 与接口层 _load_entity_vocabulary 同口径：只取就绪且未删除文档的实体
        if config["entity_recognition_mode"] == "required":
            rows = (
                await session.execute(
                    select(Entity.name, Entity.aliases)
                    .join(Document, Document.id == Entity.document_id)
                    .where(
                        Entity.library_id == library_id,
                        Document.status == "ready",
                        Document.deleted_at.is_(None),
                    )
                )
            ).all()
            config["entity_vocabulary"] = [
                {"name": str(n).strip(), "aliases": [str(a).strip() for a in (al or []) if str(a).strip()]}
                for n, al in rows if str(n or "").strip()
            ]
    print(
        f"[step_2] 知识库「{config['library_name']}」配置: use_hyde={config['use_hyde']}, "
        f"entity_mode={config['entity_recognition_mode']}, vocab={len(config['entity_vocabulary'])} 条"
    )
    return config


def step_3_run_query_sync(question: str, library_id: int, top_k: int, library_config: dict) -> EvalRow:
    """步骤 3（同步部分）：进程内直调查询工作流，取答案与最终来源。

    阻塞函数：内含 BGE-M3 编码、Milvus 检索与 LLM 生成（单题可达数十秒），
    在协程里必须经 asyncio.to_thread 调度，避免冻结事件循环。

    :param question: 待评估问题
    :param library_id: 知识库 ID
    :param top_k: 最终来源数量上限（与接口默认 QueryRequest.top_k 一致）
    :param library_config: step_2 读到的库配置
    :return: EvalRow（answer + contexts；链路异常时 error 字段带错误摘要）
    """
    from processor.query_processor.main_graph import get_kb_query_workflow

    row = EvalRow(pair=QAPair(question=question, ground_truth=""), contexts=[])
    try:
        result = get_kb_query_workflow().run({
            "library_id": library_id,
            "original_query": question,
            "session_id": f"ragas-eval-{time.time_ns()}",
            "history": [],  # 单轮评估：不带历史，避免上一题的助手回答污染本题检索与生成
            "top_k": top_k,
            "use_hyde": library_config["use_hyde"],
            "use_web_search": False,  # 评估只针对知识库检索链路，不引入外网变量
            "entity_recognition_mode": library_config["entity_recognition_mode"],
            "entity_vocabulary": library_config["entity_vocabulary"],
            "is_stream": False,
            "persist_history": False,
        })
    except Exception as error:
        row.error = f"query workflow failed: {error}"
        return row

    row.answer = str(result.get("answer") or "").strip()
    # context 口径：用 reranked_docs（即 node_answer_output 拼 prompt 时真正喂给模型的那份），
    # 而不是面向用户的 sources。sources 是 reranked_docs 再过一道阈值+配额筛选的子集，
    # 用它当 context 会把"模型据此答对、但被筛选丢弃的切片"漏掉，导致 Faithfulness/
    # Context Recall 出现假阴性（答案明明接地却被判 0）。Faithfulness 的定义要求
    # context == 生成时的条件集，因此这里对齐到 reranked_docs。
    row.contexts = [
        str(doc.get("content") or "").strip()
        for doc in (result.get("reranked_docs") or [])
        if str(doc.get("content") or "").strip()
    ]
    # 排序诊断：复用 node_rerank 的同一判据算“剔了几个标题空块”，并把最终进模型的块排序快照下来。
    # 目的：当 context_recall=1（答案块其实召回了）但 top-3 precision=0 时，能直接看到“是谁压住了答案块”。
    try:
        from processor.query_processor.nodes.node_rerank import NodeRerank
        rrf_chunks = result.get("rrf_chunks") or []
        row.dropped_heading_only = sum(1 for d in rrf_chunks if NodeRerank._is_heading_only_doc(d))
    except Exception:
        row.dropped_heading_only = 0
    row.rerank_trace = [
        {
            "rank": rank,
            "score": doc.get("score"),
            "clen": len(str(doc.get("content") or "").strip()),
            "title": str(doc.get("title") or "").strip()[:40],
            "source": doc.get("source"),
        }
        for rank, doc in enumerate(result.get("reranked_docs") or [], 1)
    ]
    return row


def step_4_build_wrappers() -> tuple[LangchainLLMWrapper, LangchainEmbeddingsWrapper]:
    """步骤 4：构建 ragas 所需的 LLM 与 Embeddings 包装对象。

    LLM 复用项目的 get_llm_client（json_mode=True：ragas 提示词要求 JSON 输出，
    强制 JSON 可避免部分网关下解析失败）；Embeddings 用本地 BGE-M3 适配器再套
    ragas 的 LangchainEmbeddingsWrapper（补齐 aembed_text 等异步接口）。

    :return: (llm_wrapper, embeddings_wrapper)
    """
    from langchain_core.embeddings import Embeddings

    from utils.embedding_utils import generate_embeddings
    from utils.llm_utils import get_llm_client

    class _BGEM3Embeddings(Embeddings):
        """本地 BGE-M3 的 ragas/langchain Embeddings 协议实现（仅稠密向量）。"""

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            """批量向量化：取项目封装 generate_embeddings 的 dense 部分（稀疏向量对余弦相似度无用）。"""
            return generate_embeddings(texts)["dense"]

        def embed_query(self, text: str) -> list[float]:
            """单条查询向量化：复用批量接口，行为一致。"""
            return self.embed_documents([text])[0]

    # 试编码一次：尽早暴露模型加载/维度问题（BGE-M3 单例就绪后 step_3 的检索链路复用同一份）
    assert len(_BGEM3Embeddings().embed_query("x")) > 0

    llm_wrapper = LangchainLLMWrapper(get_llm_client(json_mode=True, timeout=120, max_retries=2))
    embeddings_wrapper = LangchainEmbeddingsWrapper(_BGEM3Embeddings())
    print("[step_4] ragas LLM / Embeddings 包装就绪（BGE-M3 已完成一次试编码）")
    return llm_wrapper, embeddings_wrapper


def step_5_build_metrics(
    llm_wrapper: LangchainLLMWrapper,
    embeddings_wrapper: LangchainEmbeddingsWrapper,
    max_workers: int = 8,
) -> dict[str, object]:
    """步骤 5：实例化 5 个 ragas 指标并注入 llm / embeddings / 并发配置。

    字段含义：
    - Faithfulness: 答案中的论断被 context 支持的比例（LLM 判）。
    - Answer Relevancy: 由答案反向生成问题与原问题的向量余弦均值（用到 embeddings）。
    - Context Precision: 逐来源判定是否有用+是否支持答案，按位置加权（需 reference）。
    - Context Recall: ground_truth 拆句后被 context 覆盖的比例（需 reference）。
    - Answer Correctness: 语义相似度 + 陈述 F1 的加权综合。

    :param llm_wrapper: step_4 的 LLM 包装
    :param embeddings_wrapper: step_4 的 Embeddings 包装
    :param max_workers: 单指标内部并发评估的最大样本数（防打爆 LLM 网关限流）
    :return: 指标名 -> ragas 指标实例
    """
    metrics = {
        "faithfulness": Faithfulness(),
        "answer_relevancy": AnswerRelevancy(),
        "context_precision": ContextPrecision(),
        "context_recall": ContextRecall(),
        "answer_correctness": AnswerCorrectness(),
    }
    # 需要 embeddings 的指标：AnswerRelevancy（反向问题余弦相似度）与
    # AnswerCorrectness（内部 AnswerSimilarity 子指标做语义相似度）。
    needs_embeddings = {"answer_relevancy", "answer_correctness"}
    for name, metric in metrics.items():
        metric.llm = llm_wrapper
        if name in needs_embeddings:
            metric.embeddings = embeddings_wrapper
        run_config = RunConfig(max_workers=max_workers, timeout=180, max_retries=3)
        # init 是 ragas 标准入口：除缓存 run_config 外，AnswerCorrectness.init
        # 会据 weights 自动构建 answer_similarity 子指标（否则报 "AnswerSimilarity must be set"）。
        metric.init(run_config)
    print(f"[step_5] 指标初始化完成: {', '.join(metrics)}")
    return metrics


async def step_6_evaluate_all(rows: list[EvalRow], metrics: dict[str, object], context_topk: int = 0) -> None:
    """步骤 6：对每题构建 SingleTurnSample 并计算指标（就地写回 row.scores / scores_topk）。

    两种口径：
    - 全量口径：retrieved_contexts = 全部 reranked_docs，写回 row.scores（5 项指标）。
    - top-N 口径（context_topk>0 时）：retrieved_contexts 只取前 N 块，仅对
      CONTEXT_DEPENDENT_METRICS 追加计算并写回 row.scores_topk。切片数已 <=N 时不重跑
      （省一次 LLM 调用），gather 后直接从全量分复制。

    :param rows: step_3 产出的评估行（answer/contexts 已填）；问答链路失败的行跳过评估
    :param metrics: step_5 的指标实例；各指标并发跑，指标内样本由 ragas 限流
    :param context_topk: >0 时额外评估"只取前 N 块 context"的口径，默认 0（关闭）
    :return: None（分数写回 row.scores / row.scores_topk，失败指标记录到 row.error 附注）
    """
    sem = asyncio.Semaphore(4)  # 跨指标同时最多 4 题在评，兜住 LLM 网关 QPS
    to_eval = [r for r in rows if r.error is None]
    modes = f"全量 + top-{context_topk} 双口径" if context_topk > 0 else "全量单口径"
    print(f"[step_6] 开始计算指标（{modes}，待评估 {len(to_eval)} 题 × {len(metrics)} 指标）…")

    async def score_one(
        row: EvalRow, metric_name: str, metric: object,
        contexts: list[str], target: dict[str, float], label: str = "",
    ) -> None:
        sample = SingleTurnSample(
            user_input=row.pair.question,
            retrieved_contexts=contexts or [],
            response=row.answer,
            reference=row.pair.ground_truth,
        )
        async with sem:
            started = time.perf_counter()
            try:
                score = await metric.single_turn_ascore(sample)  # type: ignore[attr-defined]
                target[metric_name] = float(score)
                print(
                    f"  [{row.pair.question[:24]}…] {metric_name}{label}={score:.4f} "
                    f"({time.perf_counter() - started:.1f}s)"
                )
            except Exception as error:
                row.error = (row.error or "") + f" | {metric_name}{label} failed: {error}"
                print(f"  [{row.pair.question[:24]}…] {metric_name}{label} 评估失败: {error}")

    tasks = []
    for row in to_eval:
        # 全量口径：五项指标
        for name, metric in metrics.items():
            tasks.append(score_one(row, name, metric, row.contexts, row.scores))
        # top-N 口径：仅依赖 context 的指标，且只在确实发生截断（切片数>N）时重跑
        if context_topk > 0 and len(row.contexts) > context_topk:
            truncated = row.contexts[:context_topk]
            for name in CONTEXT_DEPENDENT_METRICS:
                tasks.append(score_one(row, name, metrics[name], truncated, row.scores_topk, label=f"(top-{context_topk})"))
    await asyncio.gather(*tasks)

    # 未截断的行（切片数<=N）top-N 口径与全量相同，直接复用，省一次重复 LLM 调用。
    if context_topk > 0:
        for row in to_eval:
            if len(row.contexts) <= context_topk:
                for name in CONTEXT_DEPENDENT_METRICS:
                    if name in row.scores:
                        row.scores_topk[name] = row.scores[name]
    print("[step_6] 指标计算完成")


def step_7_write_result_csv(rows: list[EvalRow], csv_path: Path = RESULT_CSV_PATH) -> None:
    """步骤 7：结果落盘为 UTF-8 BOM 的 CSV（Excel 直接打开不乱码）。

    :param rows: 已含分数的评估行（失败的指标留空单元格）
    :param csv_path: 输出路径，默认 eval/qa_result.csv；每个 context 切片
        在单元格内用空行分隔（保持"不拼接为单一正文"的可读边界）
    """
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(RESULT_COLUMNS)
        for row in rows:
            score_cells = [
                f"{row.scores[name]:.4f}" if name in row.scores else ""
                for name in METRIC_NAMES
            ]
            topk_cells = [
                f"{row.scores_topk[name]:.4f}" if name in row.scores_topk else ""
                for name in CONTEXT_DEPENDENT_METRICS
            ]
            writer.writerow([
                row.pair.question,
                row.answer,
                "\n\n".join(row.contexts or []),
                row.pair.ground_truth,
                *score_cells,
                *topk_cells,
            ])
    print(f"[step_7] 结果已写入 {csv_path}（{len(rows)} 行, UTF-8 with BOM）")


async def step_8_main() -> None:
    """步骤 8：总入口。串联 step_1~7 并输出汇总。

    评估链路的编排要点：step_3 是同步阻塞的工作流调用，逐题经 asyncio.to_thread
    执行（BGE-M3 模型为进程内单例，串行执行还能避免并发首请求重复加载模型）。
    """
    parser = argparse.ArgumentParser(description="RAGAS 评估：qa.csv -> 查询工作流 -> qa_result.csv")
    parser.add_argument("--library-id", type=int, default=2, help="知识库 ID，默认 2（网络设备资料）")
    parser.add_argument("--qa-csv", default=str(QA_CSV_PATH),
                        help="问答数据集 CSV 路径，默认 eval/qa.csv；换库评测时指向对应文件（如 eval/qa_huawei.csv）")
    parser.add_argument("--result-csv", default=str(RESULT_CSV_PATH),
                        help="结果 CSV 输出路径，默认 eval/qa_result.csv；不同数据集建议分开写避免互相覆盖")
    parser.add_argument("--top-k", type=int, default=5, help="最终来源数量，默认 5")
    parser.add_argument("--context-topk", type=int, default=3,
                        help="额外评估'只取前 N 块 context'的 top-N 口径（0=关闭，仅全量）；默认 3")
    parser.add_argument("--only", default="",
                        help="只跑问题中含这些关键字的题（逗号分隔、任一命中即选），用于单点诊断，如 --only 录制视频,打开相机")
    parser.add_argument("--no-eval", action="store_true",
                        help="只跑查询链路并打印排序诊断，跳过 ragas 指标计算（省 LLM 调用、快）；不写结果 CSV")
    parser.add_argument("--limit", type=int, default=0, help="只评估前 N 题（0=全量），用于冒烟")
    parser.add_argument("--workers", type=int, default=8, help="ragas 指标内部并发，默认 8")
    parser.add_argument("--preview-chars", type=int, default=300,
                        help="运行日志里上下文/答案预览的最大字符数，默认 300")
    parser.add_argument("--full", action="store_true",
                        help="运行日志不截断上下文与答案（等同 --preview-chars 0）")
    args = parser.parse_args()
    preview_chars = 0 if args.full else args.preview_chars

    pairs = step_1_read_qa_pairs(Path(args.qa_csv))
    if args.limit > 0:
        pairs = pairs[: args.limit]
        print(f"[step_1] --limit 生效，只评估前 {len(pairs)} 题")
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        pairs = [p for p in pairs if any(k in p.question for k in keys)]
        print(f"[step_1] --only 过滤后剩 {len(pairs)} 题（关键字 {keys}）")

    library_config = await step_2_get_library_config(args.library_id)
    llm_wrapper, embeddings_wrapper = step_4_build_wrappers()
    metrics = step_5_build_metrics(llm_wrapper, embeddings_wrapper, max_workers=args.workers)

    rows: list[EvalRow] = []
    for idx, pair in enumerate(pairs, 1):
        print(f"[step_3] ({idx}/{len(pairs)}) 正在调用查询工作流…")
        row = await asyncio.to_thread(
            step_3_run_query_sync, pair.question, args.library_id, args.top_k, library_config,
        )
        row.pair = pair
        # 把本题的问题/检索上下文/模型答案实时展示，方便边跑边核对链路质量
        _log_qa_detail(row, idx, len(pairs), preview_chars)
        if row.error:
            print(f"[step_3] 该题链路失败，跳过指标评估: {row.error}")
        rows.append(row)

    if args.no_eval:
        print("\n[no-eval] 已跳过 ragas 指标计算（未写结果 CSV），仅输出上方逐题排序诊断。")
        return
    await step_6_evaluate_all(rows, metrics, context_topk=args.context_topk)
    step_7_write_result_csv(rows, Path(args.result_csv))

    evaluated = [r for r in rows if r.scores]
    print(f"\n===== 汇总：共 {len(rows)} 题，成功评估 {len(evaluated)} 题 =====")
    for name in METRIC_NAMES:
        vals = [r.scores[name] for r in evaluated if name in r.scores]
        avg = sum(vals) / len(vals) if vals else float("nan")
        print(f"{name:>20}: mean={avg:.4f} (n={len(vals)})")

    if args.context_topk > 0:
        print(f"\n----- 全量 vs top-{args.context_topk} 口径对比（只看依赖 context 的指标）-----")
        print(f"{'metric':>20} {'全量':>10} {'top-N':>10} {'差(全量-topN)':>15}")
        for name in CONTEXT_DEPENDENT_METRICS:
            full_vals = [r.scores[name] for r in evaluated if name in r.scores]
            topk_vals = [r.scores_topk[name] for r in evaluated if name in r.scores_topk]
            full_avg = sum(full_vals) / len(full_vals) if full_vals else float("nan")
            topk_avg = sum(topk_vals) / len(topk_vals) if topk_vals else float("nan")
            print(f"{name:>20} {full_avg:>10.4f} {topk_avg:>10.4f} {full_avg - topk_avg:>+15.4f}")
        print("  解读：top-N 与全量越接近→答案块本就排在前列（排序好、非堆料）；"
              "top-N 明显偏低→有用块埋在后面、收窄即掉分，排序仍需治理。")


if __name__ == "__main__":
    # Windows 事件循环策略保持默认（Proactor）：工作流内部如需 asyncio.run
    # （联网检索节点）在无运行循环的线程里执行，不受本入口影响。
    asyncio.run(step_8_main())
