r"""实体出口探针集「建库 + 导入」脚本（把 tmp/entity_exit_probe_kb 投进真库）。

eval/provision_probe_kb.py
为每个语料文件夹建一个知识库、上传其中的 markdown、轮询到 ready，中途落在
needs_review 的文档按**正文里的产品全名**自动确认主实体，并把"哪几篇是人工兜出来的"如实记账。

为什么写侧走 HTTP、读侧走 MySQL：
- 写侧（建库/上传/确认实体）必须走接口，因为上传那一段管着鉴权（required 库只有系统管理员能传，
  见 backend/app/api/mysql.py 的 403 分支）、file_hash 去重、storage/uploads 目录分层、
  任务配置快照与 Redis 入队。脚本自己拼一遍就会与线上不一致，探针量出来的分布也就代表不了线上。
- 读侧（查库是否存在、轮询文档状态）只是反复读，不改变任何行为，直连 MySQL 更快也更稳
  （接口层的列表筛选参数不是给脚本用的，硬猜容易错）。

前置三件事必须你在本机做（脚本替不了）：
    # 终端 1：后端
    uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
    # 终端 2：导入 worker 是独立进程，不起它任务永远停在 queued
    uv run python -m processor.import_processor.worker
    # 终端 3：跑本脚本。口令只从环境变量或交互输入进来——不进代码、不进文件、不进 shell 历史
    $env:KB_PROVISION_PASSWORD = '...'
    .venv\Scripts\python.exe eval\provision_probe_kb.py --username <管理员账号>

常用变体：
    --dry-run                     只打印计划（建哪些库、投哪些文件、确认用什么实体名），一个请求都不发
    --only L3_apple_three_models  只处理一个文件夹
    --reset                       先删干净该探针库里的文档再重投（库与 library_id 保留）
    --base-url http://127.0.0.1:8000 --timeout 1800

--reset 为什么删文档而不是删库重建：两道接口闸挡着——
`DELETE /libraries/{id}` 在 `document_count > 0` 时直接 400，而 `kb_library.code` 是唯一约束
（含软删行），就算库能建回来、同 code 也插不回去。删文档走 `DELETE /documents/{id}`，它会同步清
Milvus 向量、切片、任务与磁盘文件，而且 library_id 不变——exit_probe 的命令以永远不用改。

输出：控制台逐轮进度 + eval\exit_probe_out\provision_report.md（每库 ID、ready 数、
人工兜实体清单，末尾直接给出可以复制就用的 exit_probe 命令）。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# 让 "python eval/provision_probe_kb.py" 直跑时也能 import 项目根目录下的 backend/processor 等包
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 重定向到某些设备时 reconfigure 可能不可用，不影响建库
        pass

import httpx  # noqa: E402

KB_ROOT = WORKSPACE_ROOT / "tmp" / "entity_exit_probe_kb"
OUT_DIR = WORKSPACE_ROOT / "eval" / "exit_probe_out"

# 实体名从「常见问题」首条取：模板保证每篇都有这一句，且用的是产品全名（比文件名干净、
# 也比正文首次出现的简称完整）。取不到就退回文件名 stem——退回是有意义的：探针集本来就包含
# 乱名文档，我要看的正是"这种名字模型认不认得"，而不是替它洗成一个漂亮名字。
BATTERY_FAQ_PATTERN = re.compile(r"问：\s*(.+?)\s*的电池容量是多少[？?]\s*答：\s*\d{3,4}\s*(?:毫安时|mAh)")

# 文档生命周期：uploaded → processing → (needs_review → processing) → ready / failed
DOC_TERMINAL = {"ready", "failed"}

# code 唯一且必须匹配 ^[a-z][a-z0-9-]*$：脚本靠它做幂等（重跑不重复建库），也靠它在管理端辨认
LIBRARY_PLAN: tuple[tuple[str, str, str, str], ...] = (
    ("L1_single_model", "probe-l1-single", "P1_探针单款", "required"),
    ("L2_two_brands", "probe-l2-two-brands", "P2_探针跨品牌", "required"),
    ("L3_apple_three_models", "probe-l3-apple3", "P3_探针苹果三款", "required"),
    ("L5_apple_ten_models", "probe-l5-apple10", "P5_探针苹果十款", "required"),
    ("L4_empty_library", "probe-l4-empty", "P4_探针空壳库", "required"),
    ("L0_disabled_mirror", "probe-l0-disabled", "P0_探针对照disabled", "disabled"),
)


@dataclass
class Job:
    """一篇已上传文档的跟踪记录。"""

    library_code: str
    filename: str
    entity_name: str
    document_id: int
    task_id: int
    status: str = "uploaded"
    confirmed: bool = False
    detail: str = ""


# ----------------------------------------------------------------------
# 一、语料侧：确认实体时用什么名字
# ----------------------------------------------------------------------

def entity_name_for_doc(doc_path: Path) -> str:
    text = doc_path.read_text(encoding="utf-8")
    matched = BATTERY_FAQ_PATTERN.search(text)
    if matched:
        return matched.group(1).strip()
    return doc_path.stem


def docs_for_folder(folder: Path) -> list[Path]:
    return sorted(
        path for path in folder.glob("*.md")
        if path.name.lower() not in {"cases.md", "readme.md"}
    )


# ----------------------------------------------------------------------
# 二、读侧：幂等查库与轮询文档状态（只读，不写任何东西）
# ----------------------------------------------------------------------

async def fetch_library_id_by_code(code: str) -> int | None:
    from sqlalchemy import select

    from backend.app.db.models import Library
    from backend.app.db.session import SessionLocal

    async with SessionLocal() as session:
        return await session.scalar(
            select(Library.id).where(Library.code == code, Library.deleted_at.is_(None))
        )


async def fetch_library_document_ids(library_id: int) -> list[int]:
    """该库当前未删除的全部文档 ID（--reset 要用）。"""
    from sqlalchemy import select

    from backend.app.db.models import Document
    from backend.app.db.session import SessionLocal

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Document.id)
                .where(Document.library_id == library_id, Document.deleted_at.is_(None))
                .order_by(Document.id)
            )
        ).scalars()
    return [int(document_id) for document_id in rows]


async def fetch_active_task_ids(document_id: int) -> list[int]:
    from sqlalchemy import select

    from backend.app.db.models import ImportTask
    from backend.app.db.session import SessionLocal

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ImportTask.id).where(
                    ImportTask.document_id == document_id,
                    ImportTask.status.in_(("queued", "processing")),
                )
            )
        ).scalars()
    return [int(task_id) for task_id in rows]


async def fetch_document_status(document_ids: list[int]) -> dict[int, tuple[str, str]]:
    """返回 {文档ID: (状态, 失败原因)}；ImportTask.error_message 比 parse_error 更常带现场。"""
    from sqlalchemy import select

    from backend.app.db.models import Document, ImportTask
    from backend.app.db.session import SessionLocal

    if not document_ids:
        return {}
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Document.id, Document.status, ImportTask.error_message)
                .join(ImportTask, ImportTask.document_id == Document.id)
                .where(Document.id.in_(document_ids))
                .order_by(ImportTask.id.desc())
            )
        ).all()
    latest: dict[int, tuple[str, str]] = {}
    for document_id, status, error_message in rows:
        # 同一文档可能有多次任务（确认后重跑），只留最新一条的状态与错误
        latest.setdefault(int(document_id), (str(status), str(error_message or "")))
    return latest


# ----------------------------------------------------------------------
# 三、写侧：全部走接口
# ----------------------------------------------------------------------

async def login(client: httpx.AsyncClient, username: str, password: str) -> None:
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if response.status_code != 200:
        raise RuntimeError(f"登录失败（{response.status_code}）：{response.text[:200]}")
    token = str(response.json().get("access_token") or "")
    if not token:
        raise RuntimeError("登录成功但响应里没有 access_token")
    client.headers["Authorization"] = f"Bearer {token}"


async def create_library(client: httpx.AsyncClient, code: str, name: str, mode: str) -> int:
    payload = {
        "name": name,
        "code": code,
        "description": "实体出口探针集（自动生成，勿手工塞别的文档）",
        "entity_recognition_mode": mode,
        # HyDE 关：探针要量的是"实体对齐走了哪条出口"，多一次假设文档生成只会让同一问法
        # 在不同轮次落到不同窗口，分布就不可比（库级开关见 Library.use_hyde）
        "use_hyde": False,
        "allowed_file_types": ["md"],
        "visibility": "private",
    }
    response = await client.post("/api/v1/libraries", json=payload)
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"建库失败 code={code}（{response.status_code}）：{response.text[:200]}")
    return int(response.json()["id"])


async def reset_library_documents(client: httpx.AsyncClient, library_id: int) -> int:
    """删掉该库现有文档（连向量与文件一起清），库本身留着。

    接口层不接受“文档还有 queued/processing 任务时删除”（mysql.py 的 400 分支），所以先取消
    活跃任务再重试一次；第二次还不行就当失败报出去，不自行动手删 DB 记录。
    """
    deleted = 0
    for document_id in await fetch_library_document_ids(library_id):
        response = await client.delete(f"/api/v1/documents/{document_id}")
        if response.status_code == 400:
            for task_id in await fetch_active_task_ids(document_id):
                await client.post(f"/api/v1/import-tasks/{task_id}/cancel")
            response = await client.delete(f"/api/v1/documents/{document_id}")
        if response.status_code not in {200, 204}:
            print(f"[reset!] 文档 {document_id} 删不掉（{response.status_code}）：{response.text[:160]}")
            continue
        deleted += 1
    return deleted


async def upload_doc(client: httpx.AsyncClient, library_id: int, path: Path) -> tuple[int, int]:
    """上传一篇，返回 (document_id, task_id)。

    一次一个文件而不是整夹并发：接口对"本批含重复文件"与"库内已存在"都是整批 409，
    逐篇投才能把失败定位到具体那一篇，而不是让一篇重复拖垮整个文件夹。
    """
    with path.open("rb") as handle:
        files = {"files": (path.name, handle.read(), "text/markdown")}
    response = await client.post("/api/v1/documents/upload", data={"library_id": str(library_id)}, files=files)
    if response.status_code == 403:
        raise RuntimeError(
            f"上传被拒 403：{response.text[:160]}\n"
            "  → required 库只允许系统管理员导入（backend/app/api/mysql.py 的上传分支）。"
            "换一个管理员账号，或先在管理端把该账号的角色调对。"
        )
    if response.status_code == 409:
        raise RuntimeError(f"重复文件，跳过：{path.name}｜{response.text[:160]}")
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"上传失败 {path.name}（{response.status_code}）：{response.text[:200]}")
    body = response.json()
    created = body[0] if isinstance(body, list) else body
    return int(created["id"]), int(created["task_id"])


async def confirm_entity(client: httpx.AsyncClient, task_id: int, item_name: str) -> None:
    response = await client.post(
        f"/api/v1/import-tasks/{task_id}/confirm-entity", json={"item_name": item_name, "no_entity": False}
    )
    if response.status_code not in {200, 201, 202}:
        raise RuntimeError(f"确认实体失败 task={task_id}（{response.status_code}）：{response.text[:200]}")


# ----------------------------------------------------------------------
# 四、编排
# ----------------------------------------------------------------------

def iter_plan(args: argparse.Namespace):
    """按 --only 过滤后的 (文件夹, code, 库名, mode, 待投文档) 序列。"""
    for folder_name, code, name, mode in LIBRARY_PLAN:
        if args.only and args.only != folder_name:
            continue
        folder = KB_ROOT / folder_name
        if not folder.is_dir():
            print(f"[skip] 语料目录不存在：{folder}")
            continue
        yield folder_name, code, name, mode, docs_for_folder(folder)


def print_plan(args: argparse.Namespace) -> None:
    """dry-run 的打印必须在登录之前：计划不依赖服务在不在线，后端没起也该能看。"""
    print(f"[dry-run] 目标 {args.base_url}（不发任何请求、不写任何东西）：")
    for folder_name, code, name, mode, paths in iter_plan(args):
        print(f"[plan] {folder_name} → 库「{name}」code={code} mode={mode}，{len(paths)} 篇：")
        for path in paths:
            print(f"        - {path.name}  （needs_review 时确认实体用：{entity_name_for_doc(path)}）")
        if not paths:
            print("        （这个文件夹没有正文文档，只建空库—L4 就应该是这样）")


async def provision(args: argparse.Namespace, password: str) -> dict[str, list[Job]]:
    jobs: dict[str, list[Job]] = {}
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        await login(client, args.username, password)
        print(f"[login] 已通过 {args.base_url} 登录为 {args.username}")

        for folder_name, code, name, mode, paths in iter_plan(args):
            library_id = await fetch_library_id_by_code(code)
            if library_id is None:
                library_id = await create_library(client, code, name, mode)
                print(f"[create] 新建库 id={library_id} name={name} mode={mode}")
            elif args.reset:
                deleted = await reset_library_documents(client, library_id)
                print(f"[reset] 库 id={library_id}（{name}）删掉 {deleted} 篇（连向量与文件），准备重投")
            else:
                print(f"[reuse] 库已存在 id={library_id}（{name}），本次只补投缺的；要全部重来加 --reset")
            # 记住 ID，报表末尾要拿它拼 exit_probe 命令
            args.library_ids[code] = library_id
            args.library_meta[code] = (folder_name, name, mode)

            folder_jobs: list[Job] = []
            for path in paths:
                try:
                    document_id, task_id = await upload_doc(client, library_id, path)
                except RuntimeError as error:
                    print(f"[upload!] {error}")
                    continue
                folder_jobs.append(Job(
                    library_code=code, filename=path.name, entity_name=entity_name_for_doc(path),
                    document_id=document_id, task_id=task_id,
                ))
                print(f"[upload] {path.name} → document={document_id} task={task_id}")
            jobs[code] = folder_jobs

        if jobs:
            # 投完再统一等：worker 是单进程串行消费，提前阻塞只会把上传也拖住
            await wait_for_documents(client, args, jobs)
    return jobs


async def wait_for_documents(client: httpx.AsyncClient, args: argparse.Namespace, jobs: dict[str, list[Job]]) -> None:
    """轮询到全部终态；期间把 needs_review 的文档按正文全名确认实体（只兜一次，不循环）。"""
    by_id = {job.document_id: job for code in jobs for job in jobs[code]}
    deadline = time.monotonic() + args.timeout
    round_no = 0
    while by_id and time.monotonic() < deadline:
        round_no += 1
        statuses = await fetch_document_status(list(by_id))
        for document_id, (status, error_message) in statuses.items():
            job = by_id[document_id]
            job.status = status
            if status == "needs_review" and not job.confirmed:
                job.confirmed = True  # 先置位再动作：确认后又回到 needs_review 就说明名字不被接受，不再重试
                try:
                    await confirm_entity(client, job.task_id, job.entity_name)
                    print(f"[confirm] {job.filename} → 主实体「{job.entity_name}」（source 会记成 manual，报表如实体现）")
                except RuntimeError as error:
                    job.detail = str(error)
                    print(f"[confirm!] {error}")
            elif status in DOC_TERMINAL:
                job.detail = error_message
                by_id.pop(document_id)
        remaining = ", ".join(f"{job.filename}:{job.status}" for job in list(by_id.values())[:6])
        print(f"[poll#{round_no}] 还剩 {len(by_id)} 篇：{remaining}{' …' if len(by_id) > 6 else ''}")
        if not by_id:
            break
        await asyncio.sleep(args.poll_seconds)

    for job in by_id.values():
        job.detail = job.detail or f"超时 {args.timeout}s 仍未终态"


# ----------------------------------------------------------------------
# 五、报表
# ----------------------------------------------------------------------

def write_report(args: argparse.Namespace, jobs: dict[str, list[Job]]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "provision_report.md"
    lines = [
        "# 探针集建库结果",
        "",
        f"- 后端：`{args.base_url}`　生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "- 每个库跑完后请用 `--inspect` 复核词表实际形状，再跑全量。",
        "",
        "| 库 | code | mode | library_id | 投了 | ready | needs_confirm | 其他 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for code, folder_jobs in jobs.items():
        folder_name, name, mode = args.library_meta.get(code, (code, code, "?"))
        ready = sum(1 for job in folder_jobs if job.status == "ready")
        confirmed = sum(1 for job in folder_jobs if job.confirmed)
        other = len(folder_jobs) - ready
        lines.append(
            f"| {name}（{folder_name}） | `{code}` | {mode} | **{args.library_ids.get(code, '?')}** "
            f"| {len(folder_jobs)} | {ready} | {confirmed} | {other} |"
        )
    lines += [
        "",
        "## 需要人工确认过实体的文档（导入期就绪度的原始记录）",
        "",
        "这些篇的 `kb_entity.source` 会是 `manual`：模型自己没能定下主实体，是脚本按正文全名兜的。",
        "它们正是「名单认不出」那一类问题在**导入侧**的对应物，第二、三件事排期时要拿这个数说话。",
        "",
    ]
    confirmed_jobs = [job for code in jobs for job in jobs[code] if job.confirmed]
    lines += (
        [f"- {job.filename} → 「{job.entity_name}」（document={job.document_id}，最终 {job.status}）" for job in confirmed_jobs]
        or ["（无：所有文档的实体都被模型自动抽出了，词表就绪度满分）"]
    )
    problem_jobs = [job for code in jobs for job in jobs[code] if job.status != "ready"]
    if problem_jobs:
        lines += ["", "## 没跑到 ready 的文档（必须先处理，否则库不可用）", ""]
        lines += [f"- {job.filename}：{job.status}｜{job.detail[:200]}" for job in problem_jobs]
    lines += [
        "",
        "## 下一步：跑探针（library_id 已经填好）",
        "",
        "```powershell",
        "cd e:\\python_project\\knowledge_base",
    ]
    for code, folder_jobs in jobs.items():
        folder_name, _name, _mode = args.library_meta.get(code, (code, "", ""))
        library_id = args.library_ids.get(code)
        if not library_id or not folder_jobs:
            continue
        lines.append(
            f".venv\\Scripts\\python.exe eval\\exit_probe.py --library-id {library_id} "
            f"--library {folder_name} --inspect"
        )
    lines += [
        "```",
        "",
        "L4 空壳库不投文档，单独跑一次就能看到 `empty_vocab` 那一格：",
        f"`.venv\\Scripts\\python.exe eval\\exit_probe.py --library-id {args.library_ids.get('probe-l4-empty', '?')} "
        "--library L4_empty_library`（需要先给它准备一个只有 README 的用例文件，或临时把 L3 的 cases.md 复制过去）",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="为实体出口探针集建库并导入文档（写操作，走真实接口）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="", help="系统管理员账号（required 库上传必须是管理员）；--dry-run 时可不给")
    parser.add_argument("--password", default="", help="留空则读环境变量 KB_PROVISION_PASSWORD，再留空则交互输入")
    parser.add_argument("--only", default="", help="只处理一个语料文件夹名，如 L3_apple_three_models")
    parser.add_argument("--reset", action="store_true", help="先删光该库的文档（库与 library_id 保留）再重投")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不发任何请求、不写库")
    parser.add_argument("--timeout", type=int, default=1800, help="等全部文档终态的总时长上限（秒）")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()
    # 挂两个 dict 给报表用：省掉一层参数传递，也让 provision/wait/write_report 的签名保持短
    args.library_ids: dict[str, int] = {}
    args.library_meta: dict[str, tuple[str, str, str]] = {}

    if args.dry_run:
        # 计划阶段不碰服务也不碰密码：后端没起、没口令也能先把建库对照表看完
        print_plan(args)
        print("[dry-run] 未发送任何请求")
        return

    password = args.password or os.environ.get("KB_PROVISION_PASSWORD", "")
    if not args.username:
        raise SystemExit("--username 必须给：required 库只允许系统管理员导入（见上传接口的 403 分支）")
    if not password:
        password = getpass.getpass(f"账号 {args.username} 的密码（不会写进任何文件）：")

    try:
        jobs = asyncio.run(provision(args, password))
    except httpx.ConnectError as error:
        raise SystemExit(f"连不上 {args.base_url}：后端起了吗？（{error}）")
    if not args.library_ids:
        raise SystemExit("没有任何库被处理，检查 --only 参数与语料目录")
    report = write_report(args, jobs)
    print(f"[done] 报表：{report}")


if __name__ == "__main__":
    main()
