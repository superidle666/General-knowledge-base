r"""实体识别模式「出口探针」跑批脚本（第一件事：先量，不治）。

eval/exit_probe.py
读 tmp/entity_exit_probe_kb/<库>/cases.md 的每一行问法，进程内直调查询工作流（与
backend/app/api/query.py 同一入口、同一套状态参数，取词表与 eval/ragas_eval.py 同口径），
记录每轮**实际走的实体对齐出口**（state.entity_align_exit），并把三个下游坑的现场证据一起
落到 jsonl，最后汇总成出口分布表 + 待人工标注清单。

用法（项目虚拟环境解释器，在工作区根目录下运行）：
    # 第一步必跑：只解析用例 + 读库看词表实际形状，不调模型、不检索（秒级）
    .venv\Scripts\python.exe eval\exit_probe.py --library-id 12 --inspect
    # 第二步冒烟：只跑前 2 条，确认链路通、出口有值
    .venv\Scripts\python.exe eval\exit_probe.py --library-id 12 --limit 2
    # 第三步全量（cases.md 路径可省，默认按库文件夹名在 kb 根目录下找）
    .venv\Scripts\python.exe eval\exit_probe.py --library-id 12 --cases tmp\entity_exit_probe_kb\L3_apple_three_models\cases.md

为何绕开 HTTP 直调工作流：`workflow.run()` 就是 `app.invoke()`，返回**完整 final state**；
埋点键住在 state 里，而接口层只回 answer/sources/image_urls/stop_reason，走 HTTP 拿不到。
代价是脚本要自己拼 state——下面拼的十个键与接口层逐一对齐，少一个就退化成另一条链路。

依赖的外部服务：Milvus / MySQL / LLM API。MongoDB 不需要：history 由脚本自带，
且 persist_history=False（与接口层一致，历史写库由接口层自己做，脚本不碰真实会话）。
BGE-M3 首次加载需数十秒，所以 --limit 1 的耗时不等于线性外推。

输出（--out-dir 默认 eval/exit_probe_out）：
    <库文件夹名>.jsonl      逐条现场，重跑覆盖
    <库文件夹名>_report.md  出口分布 + 三个坑计数 + 疑似串款清单 + 待标注表
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# 让 "python eval/exit_probe.py" 直跑时也能 import 项目根目录下的 backend/config/processor 等包
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

# Windows 控制台默认 cp936，中文表格与箭头字符会抛 UnicodeEncodeError 打断跑批；
# 统一把标准流切到 UTF-8，配合无法编码时的替换，保证"打印"这件事本身绝不再是失败源。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 重定向到某些设备时 reconfigure 可能不可用，不影响跑批
        pass

KB_ROOT = WORKSPACE_ROOT / "tmp" / "entity_exit_probe_kb"
DEFAULT_OUT_DIR = WORKSPACE_ROOT / "eval" / "exit_probe_out"

# 出口取值与 node_prepare_query.ENTITY_ALIGN_EXITS 一一对应；这里的中文只服务报表，
# 真正的枚举由 tests/test_entity_align_exit_probe.py 的静态守卫钉住，两边不会各说各话。
EXIT_LABELS: dict[str, str] = {
    "hit_exact": "①逐字/别名命中 → 带实体过滤",
    "single_candidate": "②唯一候选 → 直接收窄",
    "clarify": "③多个候选 → 反问用户",
    "refuse": "④点名查无此型 → 拒答",
    "brand_narrowed": "⑤品牌组内唯一款 → 收窄",
    "brand_giveup": "⑤品牌组内 0 款或 ≥2 款 → 放弃收窄",
    "no_brand_group": "提到实体但既不对齐也非品牌词（错字/别称主信号）",
    "no_mention": "没抽出任何提及 → 不带过滤裸搜",
    "empty_vocab": "本库词表为空 → 不带过滤裸搜",
    "not_required": "非 required 库（对照组）",
    "pre_gated": "前置闸或参数校验短路",
}

# 多轮意图的识别词：命中却没有可机读的"上一轮"时，绝不把该条当单轮跑（那样出口会假性
# 落到 no_mention，把最该看的一类样本污染掉），而是单独成组、不参与分布判读。
MULTI_TURN_TERMS: tuple[str, ...] = ("多轮", "省略", "追问", "回指", "上文", "第二轮")
# cases.md 里"上一轮"以自然语言写在预期要点内（生成语料时的约定），抓到就回显供核对。
# 冒号可省：L0 写的是「上一轮是 iphone15pro 多重」（无冒号），要求必须有冒号会让对照组库的
# 多轮用例全部落到 unseeded，等于自愿放弃最需要比的一组样本。
PREFIX_PATTERN = re.compile(r"上一轮(?:是|问|提)?\s*[：:]?\s*([^；;。\n]+)")
PREFIX_CELL_SEPARATORS = re.compile(r"\|\||→|；")

# 指纹只取电池容量：模板保证每篇的「常见问题」第一节就是"问：<全名> 的电池容量是多少？
# 答：<数字> 毫安时"，且同库内互斥——这是唯一"一句话一个值、每篇都有、库内不重复"的参数。
# 其余参数（快充/重量）在同一段里成串出现，取值与归属容易错位，做机检反而制造误报。
BATTERY_FAQ_PATTERN = re.compile(r"问：\s*(.+?)\s*的电池容量是多少[？?]\s*答：\s*(\d{3,4})\s*(?:毫安时|mAh)")
ANSWER_MAH_PATTERN = re.compile(r"(\d{3,4})\s*(?:毫安时|mAh)")
MIN_MATCH_LEN = 3  # 与 node_prepare_query.MIN_SUBSTRING_MATCH_LEN 同口径的包含匹配下限


@dataclass
class Case:
    """一行用例。"""

    row_no: int
    question: str
    intent: str = ""
    expected: str = ""
    note: str = ""
    prefix_questions: list[str] = field(default_factory=list)
    prefix_source: str = "none"  # column / text / none


# ----------------------------------------------------------------------
# 一、cases.md 解析：按表头名取列，不认列序（语料由另一个 agent 生成，列序不作数）
# ----------------------------------------------------------------------

def _split_pipe_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells)


def _pick_column(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    for index, header in enumerate(headers):
        if any(header == candidate or header.startswith(candidate) for candidate in candidates):
            return index
    return None


def parse_cases(cases_path: Path) -> list[Case]:
    """把 cases.md 的 markdown 表读成用例列表。"""
    lines = cases_path.read_text(encoding="utf-8").splitlines()
    header_index = next((i for i, line in enumerate(lines) if line.strip().startswith("|")), None)
    if header_index is None:
        raise ValueError(f"{cases_path} 里找不到 markdown 表格（首列需以「问题」为表头）")
    headers = _split_pipe_row(lines[header_index])
    question_col = _pick_column(headers, ("问题",))
    if question_col is None:
        raise ValueError(f"{cases_path} 表格缺「问题」列，实际表头：{headers}")
    columns = {
        "intent": _pick_column(headers, ("意图类型", "类型")),
        "expected": _pick_column(headers, ("预期正确答案要点", "预期答案", "预期")),
        "note": _pick_column(headers, ("备注",)),
        "prefix": _pick_column(headers, ("前置问题",)),
    }

    cases: list[Case] = []
    for line in lines[header_index + 1:]:
        if not line.strip().startswith("|"):
            continue  # 表尾之后的正文（下一节标题等）不是数据行
        cells = _split_pipe_row(line)
        if _is_separator_row(cells) or len(cells) <= question_col:
            continue
        question = cells[question_col].strip().strip("`")
        if not question:
            continue
        case = Case(
            row_no=len(cases) + 1,
            question=question,
            intent=_cell(cells, columns["intent"]),
            expected=_cell(cells, columns["expected"]),
            note=_cell(cells, columns["note"]),
        )
        _attach_prefix(case, cells, columns["prefix"])
        cases.append(case)
    if not cases:
        raise ValueError(f"{cases_path} 的表格里没有有效数据行")
    return cases


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index].strip().strip("`")


def _attach_prefix(case: Case, cells: list[str], prefix_col: int | None) -> None:
    """确定该用例的"上一轮"。列优先，其次从预期要点里抓，两条来源都要回显，绝不静默猜。"""
    cell_text = _cell(cells, prefix_col)
    if cell_text:
        case.prefix_questions = [part.strip() for part in PREFIX_CELL_SEPARATORS.split(cell_text) if part.strip()]
        case.prefix_source = "column"
        return
    if any(term in case.intent for term in MULTI_TURN_TERMS):
        for source_text in (case.expected, case.note):
            matched = PREFIX_PATTERN.search(source_text)
            if matched:
                # 回显时压掉多余空白：语料里出现过「iphone15pro  多重」双空格，直接拿去提问会影
                # 响的是一轮真实提问的形状，而不是脚本可读性
                case.prefix_questions = [re.sub(r"\s+", " ", matched.group(1)).strip()]
                case.prefix_source = "text"
                return
        case.prefix_source = "unseeded"  # 明知是多轮却拿不到上一轮：单独成组，不冒充单轮样本


# ----------------------------------------------------------------------
# 二、库内参数指纹：从同目录的 md 正文建"数值 → 归属机型"表（只做机检提示，不替代人判）
# ----------------------------------------------------------------------

def build_param_owners(folder: Path) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for doc_path in sorted(folder.glob("*.md")):
        if doc_path.name.lower() in {"cases.md", "readme.md"}:
            continue
        text = doc_path.read_text(encoding="utf-8")
        for entity_name, value in BATTERY_FAQ_PATTERN.findall(text):
            owners.setdefault(value, set()).add(entity_name.strip())
    return owners


def _cited_params(answer: str, owners: dict[str, set[str]]) -> list[str]:
    """答案里引用的电池数值及其归属（"3349→iPhone 15"，查不到归属记 "?"）。"""
    cited: list[str] = []
    for value in ANSWER_MAH_PATTERN.findall(answer):
        holders = owners.get(value)
        label = "、".join(sorted(holders)) if holders else "?"
        item = f"{value}→{label}"
        if item not in cited:
            cited.append(item)
    return cited


def _matchable(surface: str) -> bool:
    """包含匹配的长度下限：ASCII 沿用被测系统的 3，中文放到 2。

    MIN_MATCH_LEN=3 是从 node_prepare_query 抄来的（那里防的是 "15" ⊂ "iphone15" 这种噪声），
    但品牌名“苹果/华为/一加”就是 2 个汉字，照抄 3 会把它们当噪声丢掉，
    机检于是把“只报品牌 + 该品牌机型的专属数值”一律误报成疑似串款（实跑 3 条假阳性全在这一形）。
    """
    if any("\u4e00" <= char <= "\u9fff" for char in surface):
        return len(surface) >= 2
    return len(surface) >= MIN_MATCH_LEN


def _suspect_cross_model(answer: str, mentions: list[str], owners: dict[str, set[str]]) -> bool:
    """疑似串款：答案引用的专属数值，归属机型与用户点名的机型对不上。

    只在“用户点名了具体型号”时判：mentions 拆出的 token 里必须有一个含数字
    （iphone15 / mtepad11 / ace6t）。品牌级问法（“有苹果手机吗”、“一加的手机”）不判：
    它引用多款数值是期望行为（就该给清单），而中文品牌“苹果”与实体名“iPhone 15”
    根本没有共同子串，拿包含匹配去套它永远得“疑似”——那是别名词表的缺口，不是串款。

    拆 token 而不是整串比：“华为 mtepad11” 归一化后是“华为mtepad11”，与持有者
    “华为matepad11”因内部缺字对不上，但单拿“华为”这个 token 就能对上。
    代价是同族包含会漏报（iphone15 ⊂ iphone15pro 算对齐），宁漏报不误报：
    误报会把人眼引到本来答对了的条上。这只是定位提示，不是结论。
    """
    if not mentions:
        return False
    from processor.query_processor.chitchat_gate import normalize_for_match

    named: set[str] = set()
    for mention in mentions:
        for raw_token in str(mention).split():
            token = normalize_for_match(raw_token)
            if token:
                named.add(token)
    if not any(any(char.isdigit() for char in token) for token in named):
        return False
    holders: set[str] = set()
    for value in ANSWER_MAH_PATTERN.findall(answer):
        holders.update(normalize_for_match(name) for name in (owners.get(value) or set()))
    if not holders:
        return False
    for holder in holders:
        if any(token in holder or holder in token
               for token in named if _matchable(token)):
            return False
    return True


# ----------------------------------------------------------------------
# 三、日志抓取：话题一致性校验（M6）不写 state，只能从日志里取
# ----------------------------------------------------------------------

class _LogCapture(logging.Handler):
    """只留 INFO 以上的原文行，供跑完后按前缀筛；不做任何解析，避免与日志文案耦合过紧。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001 - 采集器绝不能把跑批弄挂
            pass


def _topic_guard_state(lines: list[str]) -> str:
    """aligned / not_touched / recorded_only / absent 四态，对应"这次到底动没动窗口"。"""
    for line in reversed(lines):
        if line.startswith("话题一致性校验未动作"):
            return "not_touched"
        if line.startswith("话题一致性校验："):
            return "recorded_only" if "本可换出" in line else "aligned"
    return "absent"


# ----------------------------------------------------------------------
# 四、跑批
# ----------------------------------------------------------------------

async def load_library_config(library_id: int) -> dict:
    """读库配置与实体词表。

    词表这段查询与接口层 `_load_entity_vocabulary` 同口径（只取就绪且未删除文档的实体、
    同样只在 required 时载入）：出口判读的全部意义是"线上会怎么走"，脚本自己拼一套 SQL
    就会与线上不一致，得出的分布也就不可信。
    """
    from sqlalchemy import func, select

    from backend.app.db.models import Document, Entity, Library
    from backend.app.db.session import SessionLocal

    async with SessionLocal() as session:
        library = await session.get(Library, library_id)
        if library is None or library.deleted_at is not None:
            raise RuntimeError(f"知识库 {library_id} 不存在或已删除（请核对管理端里的库 ID）")
        if library.status != "active":
            raise RuntimeError(f"知识库 {library_id}「{library.name}」已停用，无法跑探针")
        mode = str(library.entity_recognition_mode or "disabled")
        vocabulary: list[dict] = []
        if mode == "required":
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
            vocabulary = [
                {"name": str(name).strip(), "aliases": [str(alias).strip() for alias in (aliases or []) if str(alias).strip()]}
                for name, aliases in rows
                if str(name or "").strip()
            ]
        # 就绪度只有导入期有、事后拿不到，所以一并读出来：
        # · Document.status 分布——停在 needs_review/uploaded 的篇数会直接让“同品牌 3 款”变成 2 款；
        # · Entity.source——manual 占比高 = 词表是人工兜出来的，“名单认不出”这一项真实情比数字差；
        # · maybe_series 条数——当前检索侧不读它，它是第二、三件事排期的直接依据。
        status_rows = (
            await session.execute(
                select(Document.status, func.count())
                .where(Document.library_id == library_id, Document.deleted_at.is_(None))
                .group_by(Document.status)
            )
        ).all()
        entity_rows = (
            await session.execute(
                select(Entity.source, Entity.maybe_series).where(Entity.library_id == library_id)
            )
        ).all()
        # 属性在会话里一次性读干净再返回：不留“会话已关、再去摸 ORM 对象”这种依赖细节的写法
        config = {
            "library_name": str(library.name),
            "use_hyde": bool(library.use_hyde),
            "entity_recognition_mode": mode,
            "entity_vocabulary": vocabulary,
            "document_status": {str(status): int(count) for status, count in status_rows},
            "entity_source": {
                "llm": sum(1 for source, _ in entity_rows if str(source) != "manual"),
                "manual": sum(1 for source, _ in entity_rows if str(source) == "manual"),
                "maybe_series": sum(1 for _, series in entity_rows if bool(series)),
            },
        }
    return config


def run_single_turn(
    question: str,
    library_id: int,
    top_k: int,
    library_config: dict,
    history: list[dict],
) -> dict:
    """直调工作流一轮，返回 final state（阻塞，单轮可达数十秒）。"""
    from processor.query_processor.main_graph import get_kb_query_workflow

    return get_kb_query_workflow().run({
        "library_id": library_id,
        "original_query": question,
        "session_id": f"exit-probe-{time.time_ns()}",
        "history": history,
        "top_k": top_k,
        "use_hyde": library_config["use_hyde"],
        "use_web_search": False,  # 探针只看本库实体链路，不放外网变量进来
        "entity_recognition_mode": library_config["entity_recognition_mode"],
        "entity_vocabulary": library_config["entity_vocabulary"],
        "is_stream": False,
        "persist_history": False,
    })


def _fixed_reply_kind(answer: str) -> str:
    """命中哪句固定话术（坑2：零证据 ≠ 认错款，两句在报表里必须分开数）。"""
    from processor.query_processor.nodes.node_answer_output import (
        GENERATION_FAILED_REPLY,
        INSUFFICIENT_EVIDENCE_REPLY,
        RELAXED_FILTER_NOTICE_TEMPLATE,
        RETRIEVAL_INCOMPLETE_REPLY,
    )

    if answer == INSUFFICIENT_EVIDENCE_REPLY:
        return "insufficient_evidence"
    if answer == RETRIEVAL_INCOMPLETE_REPLY:
        return "retrieval_incomplete"
    if answer == GENERATION_FAILED_REPLY:
        return "generation_failed"
    # M7：被放宽过的答案前面会拼一句补充说明，它既不是固定话术也不是正常回答，单独算一态
    if answer.startswith(RELAXED_FILTER_NOTICE_TEMPLATE.split("{entity}")[0]):
        return "relaxed_notice"
    return ""


def probe_case(
    case: Case,
    library_id: int,
    top_k: int,
    library_config: dict,
    owners: dict[str, set[str]],
) -> dict:
    """跑一条用例（含它的前置轮），组装成一行现场记录。"""
    capture = _LogCapture()
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(capture)

    record: dict = {
        "row_no": case.row_no,
        "question": case.question,
        "intent": case.intent,
        "expected": case.expected,
        "note": case.note,
        "prefix_questions": case.prefix_questions,
        "prefix_source": case.prefix_source,
    }
    try:
        history: list[dict] = []
        prefix_exits: list[dict] = []
        for prefix_question in case.prefix_questions:
            capture.lines.clear()
            warm = run_single_turn(prefix_question, library_id, top_k, library_config, list(history))
            warm_answer = str(warm.get("answer") or "").strip()
            prefix_exits.append({
                "question": prefix_question,
                "exit": str(warm.get("entity_align_exit") or ""),
                "fixed_reply": _fixed_reply_kind(warm_answer),
                # 前置轮的换入也要记账：它改掉的那段答案文本会原样接进 history，目标轮就是靠这个历史做锚定
                "topic_guard": _topic_guard_state(capture.lines),
                "topic_align_log": [line for line in capture.lines if "话题一致性" in line],
            })
            # 真实连续对话的形状：把刚发生的问答原样接回历史，不做任何美化
            history = history + [
                {"role": "user", "text": prefix_question},
                {"role": "assistant", "text": warm_answer},
            ]
        capture.lines.clear()  # 目标轮的日志只留自己那一段
        started = time.perf_counter()
        state = run_single_turn(case.question, library_id, top_k, library_config, list(history))
        elapsed_ms = int((time.perf_counter() - started) * 1000)
    except Exception as error:  # noqa: BLE001 - 单条失败要留下一行，不能带走整批
        record.update({"exit": "", "error": f"{type(error).__name__}: {error}"})
        return record
    finally:
        root.removeHandler(capture)
        root.setLevel(previous_level)

    answer = str(state.get("answer") or "").strip()
    signals = state.get("entity_align_signals") or {}
    mentions = list(signals.get("mentions") or [])
    cited = _cited_params(answer, owners)
    record.update({
        "exit": str(state.get("entity_align_exit") or ""),
        "signals": signals,
        "mentions": mentions,
        "item_names": list(state.get("item_names") or []),
        "prefix_exits": prefix_exits,
        "elapsed_ms": elapsed_ms,
        "stop_reason": str(state.get("stop_reason") or ""),
        # 坑1：出口标着 hit_exact，但过滤其实是放宽后才召回到内容的 —— 必须由这两列拆穿
        "entity_filter_relaxed": bool(state.get("entity_filter_relaxed")),
        "fixed_reply": _fixed_reply_kind(answer),
        # 坑3：话题一致性校验换过窗口（救回一次漂移）时，出口数字与最终答案不同源
        "topic_guard": _topic_guard_state(capture.lines),
        # 三态判读全靠文案匹配，原始行不留档就永远说不清“没日志”是捕获问题还是分类问题
        "topic_align_log": [line for line in capture.lines if "话题一致性" in line],
        "sources_count": len(state.get("sources") or []),
        "reranked_count": len(state.get("reranked_docs") or []),
        "answer_head": answer[:160],
        "answer": answer,
        "cited_params": cited,
        "suspect_cross_model": _suspect_cross_model(answer, mentions, owners),
    })
    return record


# ----------------------------------------------------------------------
# 五、报表
# ----------------------------------------------------------------------

def write_report(out_path: Path, records: list[dict], folder_name: str, library_id: int, library_config: dict,
                 owners: dict[str, set[str]]) -> None:
    total = len(records)
    counted = [record for record in records if record.get("exit")]
    skipped = [record for record in records if record.get("prefix_source") == "unseeded"]
    failed = [record for record in records if record.get("error")]
    # 未播种的多轮用例确实跑了（拿得到一个出口），但那个出口是"把它当单轮问"的结果，
    # 会假性落入 no_mention。只把它列进单独那节、不参与任何占比，否则最需要看的 T2
    # 分组会一边污染分布、一边在报表里写着"不参与判读"——自相矛盾。
    judged = [record for record in counted if record.get("prefix_source") != "unseeded"]
    # 机检列按当前判据重算，不拿 jsonl 里的旧值：判据会改（中文品牌名长度下限就是一个），
    # 只改判据就要求重跑几十分钟模型不合理，--report-only 必须能吃到修复。
    for record in judged:
        record["suspect_cross_model"] = _suspect_cross_model(
            str(record.get("answer") or ""), record.get("mentions") or [], owners)

    distribution: dict[str, int] = {}
    for record in judged:
        distribution[str(record["exit"])] = distribution.get(str(record["exit"]), 0) + 1
    # 多轮用例的目标轮与前置轮分开统计：前置轮的出口是"上一轮怎么走的"，混进来会抬高裸搜占比
    prefix_rows = [item for record in counted for item in (record.get("prefix_exits") or [])]

    lines: list[str] = [
        f"# 实体对齐出口分布 · {folder_name}",
        "",
        f"- 知识库：`{library_config['library_name']}`（library_id={library_id}，"
        f"entity_mode={library_config['entity_recognition_mode']}，"
        f"vocab={len(library_config['entity_vocabulary'])} 条，use_hyde={library_config['use_hyde']}）",
        f"- 用例：{total} 条，其中参与判读 {len(judged)} 条（出口有值，且不是拿不到上一轮的多轮用例）、"
        f"跑批失败 {len(failed)} 条、多轮未播种不计入分布 {len(skipped)} 条",
        "",
        "## 出口分布（目标轮）",
        "",
        "| 出口 | 条数 | 占比 | 含义 |",
        "| --- | --- | --- | --- |",
    ]
    for exit_tag, count in sorted(distribution.items(), key=lambda item: -item[1]):
        label = EXIT_LABELS.get(exit_tag, "（未在标签表里，检查枚举是否漂移）")
        lines.append(f"| `{exit_tag}` | {count} | {count / max(1, len(judged)):.0%} | {label} |")
    lines += [
        "",
        "## 三个下游坑",
        "",
        f"- 坑1 放宽重查（`entity_filter_relaxed=True`）：**{sum(1 for r in judged if r.get('entity_filter_relaxed'))} 条**"
        " —— 这些条的出口看着像正常命中，实质靠拆掉实体过滤才召回到内容，判读时按裸搜算",
        f"- 坑2 固定话术：零证据 {sum(1 for r in judged if r.get('fixed_reply') == 'insufficient_evidence')} 条 / "
        f"检索未完成 {sum(1 for r in judged if r.get('fixed_reply') == 'retrieval_incomplete')} 条 / "
        f"生成失败 {sum(1 for r in judged if r.get('fixed_reply') == 'generation_failed')} 条 / "
        f"带放宽说明 {sum(1 for r in judged if r.get('fixed_reply') == 'relaxed_notice')} 条"
        " —— 零证据≠认错款，两类修法不同，不能合并",
        f"- 坑3 话题一致性校验（目标轮）：换入 {sum(1 for r in judged if r.get('topic_guard') == 'aligned')} 条 / "
        f"未动作 {sum(1 for r in judged if r.get('topic_guard') == 'not_touched')} 条 / "
        f"仅记录 {sum(1 for r in judged if r.get('topic_guard') == 'recorded_only')} 条 / "
        f"无日志 {sum(1 for r in judged if r.get('topic_guard') == 'absent')} 条"
        " —— 换入过的条，最终答案与出口标记不同源",
        f"  前置轮另记：换入/仅记录共 "
        f"{sum(1 for p in prefix_rows if p.get('topic_guard') in ('aligned', 'recorded_only'))} 次"
        " —— 前置轮动过窗口就意味着喂给目标轮的历史文本已被改过，不能当噪音扔",
        "  「无日志」不等于没跑校验：四条文案都在「有专名可对齐」时才写，判据不足以外走不到分支就没行",
        "",
        "## 疑似串款（机检提示，供人眼快速定位，不是结论）",
        "",
    ]
    suspects = [record for record in judged if record.get("suspect_cross_model")]
    # 品牌级点名（mentions 里没有含数字的型号形状）机检一律不判，但要透明列出来：
    # 这类条“有没有随机挑一款当唯一答案”靠出口判读和人眼，不靠子串匹配。
    brand_level = [
        record for record in judged
        if record.get("cited_params")
        and not any(any(char.isdigit() for char in str(mention)) for mention in (record.get("mentions") or []))
    ]
    if suspects:
        lines += ["| # | 问题 | 出口 | 点名 | 答案引用数值 |", "| --- | --- | --- | --- | --- |"]
        lines += [
            f"| {record['row_no']} | {record['question']} | `{record['exit']}` "
            f"| {'、'.join(record.get('mentions') or []) or '-'} | {'；'.join(record.get('cited_params') or []) or '-'} |"
            for record in suspects
        ]
    else:
        lines.append("（无：凡点名了具体型号、且答案引用到库内专属电池数值的条，归属都与点名机型一致）")
    if brand_level:
        lines.append(
            f"　另有 {len(brand_level)} 条只报了品牌/品类（#"
            f"{'、'.join(str(record['row_no']) for record in brand_level)}）：机检不判这类——"
            "中文品牌名与实体名没有共同子串（苹果 vs iPhone 15），而它们引用多款数值本就是期望行为；"
            "要查的是“有没有随机挑一款当唯一答案”，到下面标注表看"
        )
    lines += [
        "",
        "## 待人工标注",
        "",
        "只填最后一列，四选一：`对` / `错` / `拒答对` / `拒答错`。机检已经把可疑的挑到上面那张表了，"
        "这里主要扫的是「出口看着正常、答案却答非所问」的那批。",
        "",
        "| # | 问题 | 意图 | 出口 | 答案摘要 | 标注 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for record in judged:
        answer_head = str(record.get("answer_head") or "").replace("|", "\\|").replace("\n", " ")
        intent = str(record.get("intent") or "").replace("|", "\\|")
        lines.append(
            f"| {record['row_no']} | {record['question']} | {intent} | `{record['exit']}` | {answer_head} |  |"
        )
    if skipped:
        lines += [
            "",
            "## 多轮用例未播种（本轮不参与分布判读）",
            "",
            "这些行的意图是多轮追问，但 cases.md 里没给出可机读的「上一轮」。把它们当单轮跑会得到假的"
            "`no_mention`，污染最需要看的 T2 分组，所以只列不跑判读。补法：给表格加一列 `前置问题`。",
            "",
        ]
        lines += [f"- 第 {record['row_no']} 行：{record['question']}（意图：{record['intent']}）" for record in skipped]
    if failed:
        lines += ["", "## 跑批失败", ""]
        lines += [f"- 第 {record['row_no']} 行：{record.get('error')}" for record in failed]
    if prefix_rows:
        lines += [
            "",
            "## 附：前置轮自己的出口（不计入上面的分布）",
            "",
        ]
        lines += [
            f"- 第 {record['row_no']} 行的前置轮 "
            f"`{item['question']}` → `{item['exit'] or '（无标记）'}`"
            + (f"（固定话术：{item['fixed_reply']}）" if item.get("fixed_reply") else "")
            for record in counted for item in (record.get("prefix_exits") or [])
        ]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------
# 六、入口
# ----------------------------------------------------------------------

def _resolve_cases_path(args: argparse.Namespace) -> Path:
    if args.cases:
        path = Path(args.cases)
        return path if path.is_absolute() else WORKSPACE_ROOT / path
    if not args.library:
        raise SystemExit("必须给 --cases <cases.md 路径> 或 --library <库文件夹名> 之一")
    return KB_ROOT / args.library / "cases.md"


def _print_inspect(cases: list[Case], owners: dict[str, set[str]], library_config: dict, folder_name: str) -> None:
    print(f"[inspect] 库文件夹={folder_name}，MySQL 库名=「{library_config['library_name']}」")
    print(f"[inspect] entity_mode={library_config['entity_recognition_mode']}, "
          f"use_hyde={library_config['use_hyde']}, 词表条数={len(library_config['entity_vocabulary'])}")
    print("[inspect] 词表规范名实际形状（导入清洗后的真值，判读 T4 的前提）：")
    for item in library_config["entity_vocabulary"][:30]:
        aliases = f"（别名：{'、'.join(item['aliases'])}）" if item["aliases"] else ""
        print(f"          - {item['name']}{aliases}")
    if len(library_config["entity_vocabulary"]) > 30:
        print(f"          … 其余 {len(library_config['entity_vocabulary']) - 30} 条")
    ambiguous = {value: names for value, names in owners.items() if len(names) > 1}
    print(f"[inspect] 电池指纹条目={len(owners)}，其中同值多归属={len(ambiguous)}"
          f"（同库内应互斥，非 0 说明语料参数重复了，串款机检会失真）")
    status_text = "、".join(f"{status}×{count}" for status, count in sorted(library_config["document_status"].items())) or "无文档"
    source = library_config["entity_source"]
    print(f"[inspect] 库就绪度：文档状态 {status_text}；实体 llm自动 {source['llm']} 条 / manual人工 {source['manual']} 条 / "
          f"maybe_series {source['maybe_series']} 条")
    if library_config["entity_recognition_mode"] == "required" and not library_config["entity_vocabulary"]:
        print("[inspect] ⚠ 这是个 required 空壳库：词表 0 条 → 所有带实体的问法都会落 empty_vocab（不收窄、不拒答）。"
              "除非这就是 L4，否则文档还没跑完，现在别跑全量。")
    print(f"[inspect] 用例 {len(cases)} 条：")
    for case in cases:
        mark = {"column": "多轮·列已给", "text": "多轮·从要点里抓到", "unseeded": "多轮·拿不到上一轮(不判读)",
                "none": "单轮"}[case.prefix_source]
        prefix = f" ← 前置：{case.prefix_questions}" if case.prefix_questions else ""
        print(f"          [{case.row_no}] {case.question} ｜ {case.intent} ｜ {mark}{prefix}")
    print(f"[inspect] 预计全量耗时约 {len(cases) * 25 // 60}~{len(cases) * 45 // 60} 分钟"
          "（含多轮前置轮，首轮还要加 BGE-M3 加载数十秒）")


def main() -> None:
    parser = argparse.ArgumentParser(description="实体识别模式出口探针跑批")
    parser.add_argument("--library-id", type=int, required=True, help="知识库 ID（管理端列表里能看到的那个数字）")
    parser.add_argument("--cases", default="", help="cases.md 路径（相对工作区根目录也可）")
    parser.add_argument("--library", default="", help="kb 根目录下的库文件夹名，与 --cases 二选一")
    parser.add_argument("--top-k", type=int, default=5, help="与 QueryRequest.top_k 默认值一致")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部），先用它冒烟")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--inspect", action="store_true", help="只解析用例与读词表，不调模型、不检索")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="不跑批，只把已存在的 jsonl 重新生成报表（报表渲染有 bug 时不必重跑几十分钟）",
    )
    args = parser.parse_args()

    cases_path = _resolve_cases_path(args)
    if not cases_path.exists():
        raise SystemExit(f"找不到用例文件：{cases_path}")
    folder_name = cases_path.parent.name
    cases = parse_cases(cases_path)
    owners = build_param_owners(cases_path.parent)

    library_config = asyncio.run(load_library_config(args.library_id))
    out_dir = Path(args.out_dir)
    jsonl_path = out_dir / f"{folder_name}.jsonl"
    report_path = out_dir / f"{folder_name}_report.md"
    if args.report_only:
        if not jsonl_path.exists():
            raise SystemExit(f"没有可复用的跑批结果：{jsonl_path}")
        records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        write_report(report_path, records, folder_name, args.library_id, library_config, owners)
        print(f"[done] 已从 {len(records)} 条现有结果重生报表：{report_path}")
        return
    if args.inspect:
        _print_inspect(cases, owners, library_config, folder_name)
        return

    if args.limit:
        cases = cases[: args.limit]
    out_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    print(f"[run] 输出：{jsonl_path}（重跑覆盖）；{len(cases)} 条用例")
    with jsonl_path.open("w", encoding="utf-8") as sink:
        try:
            for case in cases:
                record = probe_case(case, args.library_id, args.top_k, library_config, owners)
                records.append(record)
                sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                sink.flush()  # 逐条落盘：中途 Ctrl-C 也已保住前面所有条
                signals = record.get("signals") or {}
                print(f"[{case.row_no}/{len(cases)}] exit={record.get('exit') or '（无）'}"
                      f" relaxed={int(record.get('entity_filter_relaxed') or 0)}"
                      f" fixed={record.get('fixed_reply') or '-'}"
                      f" guard={record.get('topic_guard') or '-'}"
                      f" 候选={signals.get('candidate_count', '-')} 收窄={record.get('item_names') or '-'}"
                      f" {record.get('elapsed_ms', 0)}ms"
                      f"{' [报错]' + str(record.get('error')) if record.get('error') else ''}")
        except KeyboardInterrupt:
            print("[run] 已中断，前面已跑完的条目都在 jsonl 与报表里")

    write_report(report_path, records, folder_name, args.library_id, library_config, owners)
    print(f"[done] 报表：{report_path}")
    print("[done] 下一步：只扫报表「待人工标注」最后一列，四选一填 对/错/拒答对/拒答错")


if __name__ == "__main__":
    main()
