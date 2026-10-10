"""普通用户可调用的本地知识库查询接口。"""

from __future__ import annotations

import asyncio
import json
import queue
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.schemas import (
    QueryChatMessageOut,
    QueryLibraryOut,
    QueryRequest,
    QueryResponse,
    QuerySessionCreate,
    QuerySessionMessagesOut,
    QuerySessionOut,
    QuerySessionUpdate,
    QuerySourceOut,
)
from backend.app.api.library_facts import (
    READY_DOCUMENT_CLAUSES,
    empty_library_facts,
    load_library_facts,
)
from backend.app.core.auth import AuthenticatedUser, get_current_user, get_optional_current_user
from backend.app.db.models import Document, DocumentChunk, Entity, Library
from backend.app.db.session import get_db
from processor.query_processor.main_graph import get_kb_query_workflow
from processor.query_processor.metadata_gate import is_metadata_question
from tool.logger import logger
from utils.sse_utils import SSEEvent, create_sse_queue, get_sse_queue, push_to_session, remove_sse_queue
from utils.mongo_history_utils import (
    ensure_user_session,
    get_or_create_user_library_session,
    get_recent_messages,
    get_session_messages,
    get_user_session,
    list_user_sessions,
    delete_user_session,
    rename_user_session,
    save_chat_message,
    touch_user_session,
)

router = APIRouter(prefix="/api/v1/query", tags=["query"])


class UTF8JSONResponse(JSONResponse):
    """明确声明查询响应使用 UTF-8，避免中间层按本地编码解释中文。"""

    media_type = "application/json; charset=utf-8"


def _sse_event(event: str, data: dict) -> str:
    """序列化一条 UTF-8 SSE 事件。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/libraries", response_model=list[QueryLibraryOut], response_class=UTF8JSONResponse)
async def list_query_libraries(
    session: AsyncSession = Depends(get_db),
    _: AuthenticatedUser | None = Depends(get_optional_current_user),
) -> list[Library]:
    """返回用户端可选择的有效知识库；当前阶段暂不执行成员范围过滤。"""
    statement = (
        select(Library)
        .where(Library.status == "active", Library.deleted_at.is_(None))
        .order_by(Library.id.desc())
    )
    return list((await session.execute(statement)).scalars())


async def _get_active_library(session: AsyncSession, library_id: int) -> Library:
    library = await session.get(Library, library_id)
    if library is None or library.deleted_at is not None:
        raise HTTPException(404, "知识库不存在")
    if library.status != "active":
        raise HTTPException(400, "知识库已停用，暂不支持查询")
    return library


def _entity_vocab_payload(rows: list[tuple[str, list[str] | None]]) -> list[dict]:
    """把 (实体名, 别名列表) 行转成检索对齐用的紧凑词表；名与别名去空白、丢空串。"""
    vocabulary: list[dict] = []
    for name, aliases in rows:
        clean_name = str(name or "").strip()
        if not clean_name:
            continue
        clean_aliases = [str(alias).strip() for alias in (aliases or []) if str(alias).strip()]
        vocabulary.append({"name": clean_name, "aliases": clean_aliases})
    return vocabulary


async def _load_entity_vocabulary(session: AsyncSession, library_id: int, library: Library) -> list[dict]:
    """required 库载入本库实体词表供查询侧对齐；只取就绪且未删除文档，与可召回切片保持一致。

    词表由异步接口注入 state，而不是在同步检索图里读库：查询图跑在线程池且 MySQL 只有异步会话。

    口径常量 `READY_DOCUMENT_CLAUSES` 与元数据画像共用一份：当年它就是四处各抄了一遍，
    改一处忘三处的表现不是报错而是"清单里混进了待确认和已删除的文档"。
    """
    if str(library.entity_recognition_mode or "disabled") != "required":
        return []
    rows = (
        await session.execute(
            select(Entity.name, Entity.aliases)
            .join(Document, Document.id == Entity.document_id)
            .where(
                Entity.library_id == library_id,
                *READY_DOCUMENT_CLAUSES,
            )
        )
    ).all()
    return _entity_vocab_payload([(row[0], row[1]) for row in rows])


async def _load_library_facts_if_needed(
    session: AsyncSession, library_id: int, library: Library, query: str
) -> dict:
    """只在"本库会启用元数据闸 且 句式像库级元问题"时读画像，其余一律空壳。

    两个前提缺一不可：
    - 模式不是 required → 图里的闸整段不生效（与 `_entity_alignment_available` 同口径），
      画像装了也没人读，还要为每次普通问答多付五次聚合查询；
    - 句式不像元问题 → 同样用不上。探测函数与图内判定是**同一个** `is_metadata_question`：
      两处各写一套判据会出现"接口没装载、图里却判成元问题"，那正好落进降级话术，
      用户看见的是一句本可以不出现的错误。

    读库失败不抛 503：内容检索照常跑，只把 `loaded=False` 带下去由元数据闸出降级句。
    """
    if str(library.entity_recognition_mode or "disabled") != "required":
        return empty_library_facts(library_id, library)
    if not is_metadata_question(query):
        return empty_library_facts(library_id, library)
    try:
        return await load_library_facts(session, library_id, library)
    except Exception as error:  # noqa: BLE001 - 画像读挂不能拖垮问答，降级成 loaded=False 继续
        logger.warning("库元数据画像装载失败，已降级（library_id=%s）：%s", library_id, error)
        return empty_library_facts(library_id, library)


def _web_source_out(source: dict) -> QuerySourceOut:
    """联网来源不参与 MySQL 回填：它的可信标识就是 URL 本身。"""
    try:
        score = float(source.get("score"))
    except (TypeError, ValueError):
        score = None
    url = str(source.get("url") or "").strip() or None
    title = str(source.get("title") or "").strip()
    site_name = str(source.get("site_name") or "").strip() or None
    return QuerySourceOut(
        document_title=title or site_name or url or "联网资料",
        content=str(source.get("content") or "").strip(),
        score=score,
        score_kind=str(source.get("score_kind") or "rerank"),
        source="web",
        url=url,
        site_name=site_name,
    )


def _local_source_out(
    chunk: DocumentChunk,
    document: Document,
    source: dict,
    valid_part_ids: set[str],
) -> QuerySourceOut:
    """把一条本地来源转成对外结构；合并来源的正文与段数按**实际可溯源**的分片给出。"""
    metadata = chunk.extra_metadata or {}
    part_ids = [
        str(part_id) for part_id in (source.get("part_chunk_ids") or []) if part_id is not None
    ]
    # 只数“库里还存在”的分片：文档已删/未 ready 的那几段不能算进“含 N 段”，
    # 否则展示的是一个无法溯源的段数。
    merged_parts = len({part_id for part_id in part_ids if part_id in valid_part_ids})
    content = chunk.content
    if merged_parts > 1:
        # 合并来源是个例外：模型实际读到的是几段拼接后的正文，MySQL 里代表片的单段正文
        # 已不代表它；这里改用检索链路带出的合并正文，保证“展示的内容 = 读过的内容”。
        content = str(source.get("content") or "").strip() or chunk.content
    return QuerySourceOut(
        chunk_id=chunk.vector_id,
        document_id=document.id,
        document_title=document.title,
        chunk_index=chunk.chunk_index,
        page_number=chunk.page_number,
        section_title=chunk.section_title,
        parent_title=str(metadata.get("parent_title") or "") or None,
        content=content,
        score=source.get("score"),
        score_kind=str(source.get("score_kind") or "rerank"),
        source="local",
        merged_parts=merged_parts if merged_parts > 1 else None,
    )


async def _hydrate_sources(
    session: AsyncSession,
    library_id: int,
    sources: list[dict],
) -> list[QuerySourceOut]:
    """以 MySQL 为准补齐本地来源展示信息，并过滤已删除或未完成文档；联网来源直接透传。

    本地走 MySQL 而不是直接信任向量库返回值，是为了避免文档已删除但索引未清理时仍被引用；
    联网结果没有库内主键，因此不能被同一套校验覆盖。
    """
    hydrated: list[QuerySourceOut] = []
    source_by_vector_id: dict[str, dict] = {}
    check_ids: set[str] = set()
    for source in sources:
        if str(source.get("source") or "local") == "web":
            hydrated.append(_web_source_out(source))
            continue
        if source.get("chunk_id") is None:
            continue
        representative = str(source["chunk_id"])
        source_by_vector_id[representative] = source
        # 校验集合包含合并来源的全部候选分片：它们共用这一次 IN 查询（不额外多一次 RPC），
        # 拿回来的存在集合才能算出“哪几段现在还可溯源”。
        check_ids.add(representative)
        for part_id in source.get("part_chunk_ids") or []:
            if part_id is not None:
                check_ids.add(str(part_id))
    if not source_by_vector_id:
        hydrated.sort(key=lambda item: (item.score is None, -(item.score or 0)))
        return hydrated
    rows = await session.execute(
        select(DocumentChunk, Document)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.library_id == library_id,
            # 来源可溯源的文档口径与词表/元数据画像同一条：已删或未就绪的文档不能出现在面板里，
            # 也不能因为口径写得不一样而在三个地方各自漂移。
            *READY_DOCUMENT_CLAUSES,
            DocumentChunk.vector_id.in_(check_ids),
        )
    )
    row_by_vector_id: dict[str, tuple[DocumentChunk, Document]] = {}
    valid_vector_ids: set[str] = set()
    for chunk, document in rows:
        vector_id = str(chunk.vector_id)
        valid_vector_ids.add(vector_id)
        row_by_vector_id.setdefault(vector_id, (chunk, document))
    for vector_id, source in source_by_vector_id.items():
        row = row_by_vector_id.get(vector_id)
        if row is None:
            # 代表片查不到（文档已删/未 ready）→ 整条来源丢弃：宁可不展示，也不展示无法溯源的引用
            continue
        chunk, document = row
        hydrated.append(_local_source_out(chunk, document, source, valid_vector_ids))
    hydrated.sort(
        key=lambda item: (
            item.score is None,
            -(item.score or 0),
            item.document_id or 0,
            item.chunk_index or 0,
        )
    )
    return hydrated


async def _prepare_session_context(
    payload: QueryRequest,
    user: AuthenticatedUser | None,
) -> tuple[str, list[dict]]:
    """为登录用户创建会话、读取最近十条历史，并保存当前问题。"""
    if user is None:
        return payload.session_id or f"query-session-{uuid.uuid4().hex}", []
    try:
        if payload.session_id and not payload.session_id.startswith("query-session-local-"):
            session_id = payload.session_id
        else:
            session = await asyncio.to_thread(
                get_or_create_user_library_session,
                user.id,
                payload.library_id,
                payload.query,
            )
            session_id = str(session["_id"])
        await asyncio.to_thread(
            ensure_user_session,
            session_id,
            user.id,
            payload.library_id,
            payload.query,
        )
        history = await asyncio.to_thread(get_recent_messages, session_id, 10)
        await asyncio.to_thread(save_chat_message, session_id, "user", payload.query)
        await asyncio.to_thread(touch_user_session, session_id)
        return session_id, history
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(503, "会话历史服务暂时不可用，请稍后重试") from error


async def _persist_assistant_message(
    user: AuthenticatedUser | None,
    session_id: str,
    answer: str,
    sources: list[QuerySourceOut],
    image_urls: list[str],
    clarify_candidates: list[str] | None = None,
) -> None:
    """登录用户查询完成后保存助手答复与可追溯来源。"""
    if user is None:
        return
    try:
        await asyncio.to_thread(
            save_chat_message,
            session_id,
            "assistant",
            answer,
            sources=[source.model_dump(mode="json") for source in sources],
            image_urls=image_urls,
            # 反问候选必须跟着落库：下一轮“是否在回答反问”只认这个字段，不从正文里抓
            clarify_candidates=list(clarify_candidates or []),
        )
        await asyncio.to_thread(touch_user_session, session_id)
    except Exception:
        # 历史写入失败不应覆盖已经生成的回答；下次请求仍会明确报出会话服务问题。
        return


@router.post("/sessions", response_model=QuerySessionOut, response_class=UTF8JSONResponse)
async def create_saved_session(
    payload: QuerySessionCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> QuerySessionOut:
    """为当前用户在指定知识库下创建一个新的连续会话。"""
    session_id = f"query-session-{uuid.uuid4().hex}"
    try:
        await asyncio.to_thread(ensure_user_session, session_id, user.id, payload.library_id, payload.title)
        row = await asyncio.to_thread(get_user_session, session_id, user.id)
    except Exception as error:
        raise HTTPException(503, "会话历史服务暂时不可用，请稍后重试") from error
    if row is None:
        raise HTTPException(503, "新建会话失败，请稍后重试")
    return QuerySessionOut(
        id=session_id,
        library_id=int(row["library_id"]),
        title=str(row.get("title") or payload.title),
        created_at=float(row.get("created_at") or 0),
        updated_at=float(row.get("updated_at") or 0),
    )

@router.get("/sessions", response_model=list[QuerySessionOut], response_class=UTF8JSONResponse)
async def list_saved_sessions(
    library_id: int | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[QuerySessionOut]:
    """读取当前登录用户的历史会话，可按知识库筛选。"""
    try:
        rows = await asyncio.to_thread(list_user_sessions, user.id, library_id)

    except Exception as error:
        raise HTTPException(503, "会话历史服务暂时不可用，请稍后重试") from error
    return [
        QuerySessionOut(
            id=str(row["_id"]),
            library_id=int(row["library_id"]),
            title=str(row.get("title") or "新建技术咨询"),
            created_at=float(row.get("created_at") or 0),
            updated_at=float(row.get("updated_at") or 0),
        )
        for row in rows
    ]


@router.get("/sessions/{session_id}/messages", response_model=QuerySessionMessagesOut, response_class=UTF8JSONResponse)
async def get_saved_session_messages(
    session_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> QuerySessionMessagesOut:
    """读取一个归属当前用户的历史会话及其消息。"""
    try:
        session_row = await asyncio.to_thread(get_user_session, session_id, user.id)
        if session_row is None:
            raise HTTPException(404, "会话不存在或无权访问")
        rows = await asyncio.to_thread(get_session_messages, session_id)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, "会话历史服务暂时不可用，请稍后重试") from error
    return QuerySessionMessagesOut(
        session_id=session_id,
        library_id=int(session_row["library_id"]),
        messages=[
            QueryChatMessageOut(
                id=str(row.get("_id") or ""),
                role=str(row.get("role") or "assistant"),
                content=str(row.get("text") or ""),
                created_at=float(row.get("ts") or 0),
                sources=row.get("sources") or [],
                image_urls=row.get("image_urls") or [],
            )
            for row in rows
        ],
    )


@router.patch("/sessions/{session_id}", response_model=QuerySessionOut, response_class=UTF8JSONResponse)
async def rename_saved_session(
    session_id: str,
    payload: QuerySessionUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> QuerySessionOut:
    """重命名当前用户的历史会话。"""
    try:
        updated = await asyncio.to_thread(rename_user_session, session_id, user.id, payload.title)
        if not updated:
            raise HTTPException(404, "会话不存在或无权访问")
        row = await asyncio.to_thread(get_user_session, session_id, user.id)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, "会话历史服务暂时不可用，请稍后重试") from error
    return QuerySessionOut(
        id=session_id,
        library_id=int(row["library_id"]),
        title=str(row.get("title") or payload.title),
        created_at=float(row.get("created_at") or 0),
        updated_at=float(row.get("updated_at") or 0),
    )


@router.delete("/sessions/{session_id}", response_class=UTF8JSONResponse)
async def delete_saved_session(
    session_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, bool]:
    """删除当前用户的历史会话及消息。"""
    try:
        deleted = await asyncio.to_thread(delete_user_session, session_id, user.id)
    except Exception as error:
        raise HTTPException(503, "会话历史服务暂时不可用，请稍后重试") from error
    if not deleted:
        raise HTTPException(404, "会话不存在或无权访问")
    return {"deleted": True}


@router.post("", response_model=QueryResponse, response_class=UTF8JSONResponse)
async def query_knowledge_base(
    payload: QueryRequest,
    session: AsyncSession = Depends(get_db),
    user: AuthenticatedUser | None = Depends(get_optional_current_user),
) -> QueryResponse:
    """查询单个知识库；当前阶段不执行成员或可见范围权限过滤。"""
    library = await _get_active_library(session, payload.library_id)
    session_id, history = await _prepare_session_context(payload, user)
    entity_vocabulary = await _load_entity_vocabulary(session, payload.library_id, library)
    library_facts = await _load_library_facts_if_needed(session, payload.library_id, library, payload.query)
    try:
        # 图是进程内共享的（节点无请求态），每请求只新建 state 字典
        workflow = get_kb_query_workflow()
        # 工作流内部是同步阻塞调用（BGE-M3 编码、Milvus、LLM，合计可达数十秒），
        # 必须挪到线程池执行：直接在本协程里跑会冻结整个事件循环，
        # 使该请求期间所有其他请求（含 SSE 心跳）都无法响应；
        # 联网检索节点内部的 asyncio.run 也只有在“当前线程无运行循环”时才可用。
        result = await asyncio.to_thread(workflow.run, {
            "library_id": payload.library_id,
            "original_query": payload.query,
            "session_id": session_id,
            "history": history,
            "top_k": payload.top_k,
            "use_hyde": bool(library.use_hyde),
            "use_web_search": payload.use_web_search,
            "entity_recognition_mode": str(library.entity_recognition_mode or "disabled"),
            # 库级确认闸严格程度：required 库据此走“先问一句”还是“只筛选不反问”
            "entity_confirm_mode": str(library.entity_confirm_mode or "strict"),
            "entity_vocabulary": entity_vocabulary,
            # 库元数据画像：同步与流式两个入口必须成对注入，漏一个就是"同一个问题在两个入口
            # 一个答得出一个答不出"，比都不支持更难查（守卫见 tests/test_query_entity_alignment.py）
            "library_facts": library_facts,
            "is_stream": False,
            "persist_history": False,
        })
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, "知识库查询暂时不可用，请稍后重试") from error

    sources = await _hydrate_sources(session, payload.library_id, result.get("sources") or [])
    answer = str(result.get("answer") or "").strip()
    image_urls = [str(url) for url in result.get("image_urls") or [] if str(url).strip()]
    if not sources and not answer:
        answer = "当前知识库中没有找到与该问题相关的内容。"
    await _persist_assistant_message(
        user, session_id, answer, sources, image_urls,
        clarify_candidates=[str(c) for c in result.get("entity_clarify_candidates") or [] if str(c).strip()],
    )
    return QueryResponse(
        library_id=payload.library_id,
        session_id=session_id,
        query=payload.query,
        answer=answer,
        sources=sources,
        image_urls=image_urls,
    )


@router.post("/stream")
async def stream_query_knowledge_base(
    request: Request,
    payload: QueryRequest,
    session: AsyncSession = Depends(get_db),
    user: AuthenticatedUser | None = Depends(get_optional_current_user),
) -> StreamingResponse:
    """以 SSE 增量返回知识库回答；客户端可通过 fetch 请求携带认证头。"""
    library = await _get_active_library(session, payload.library_id)
    session_id, history = await _prepare_session_context(payload, user)
    entity_vocabulary = await _load_entity_vocabulary(session, payload.library_id, library)
    library_facts = await _load_library_facts_if_needed(session, payload.library_id, library, payload.query)
    stream_id = f"query-stream-{uuid.uuid4().hex}"
    create_sse_queue(stream_id)

    def run_workflow() -> None:
        try:
            result = get_kb_query_workflow().run({
                "library_id": payload.library_id,
                "original_query": payload.query,
                "session_id": stream_id,
                "history": history,
                "top_k": payload.top_k,
                "use_hyde": bool(library.use_hyde),
                "use_web_search": payload.use_web_search,
                "entity_recognition_mode": str(library.entity_recognition_mode or "disabled"),
                "entity_confirm_mode": str(library.entity_confirm_mode or "strict"),
                "entity_vocabulary": entity_vocabulary,
                "library_facts": library_facts,
                "is_stream": True,
                "persist_history": False,
            })
            push_to_session(stream_id, "workflow_complete", {
                "answer": str(result.get("answer") or "").strip(),
                "sources": result.get("sources") or [],
                "image_urls": result.get("image_urls") or [],
                # 候选清单随事件透传出去，event_generator 才能把它写进会话历史
                "clarify_candidates": [
                    str(c) for c in result.get("entity_clarify_candidates") or [] if str(c).strip()
                ],
            })
        except Exception:
            push_to_session(stream_id, SSEEvent.ERROR, {
                "error": "知识库查询暂时不可用，请稍后重试",
            })

    async def event_generator() -> AsyncGenerator[str, None]:
        stream_queue = get_sse_queue(stream_id)
        if stream_queue is None:
            return
        task = asyncio.create_task(asyncio.to_thread(run_workflow))
        loop = asyncio.get_running_loop()
        try:
            yield _sse_event("ready", {"library_id": payload.library_id, "query": payload.query})
            while True:
                if await request.is_disconnected():
                    return
                try:
                    message = await loop.run_in_executor(None, stream_queue.get, True, 1.0)
                except queue.Empty:
                    continue

                event = message.get("event")
                data = message.get("data") or {}
                if event == SSEEvent.DELTA:
                    yield _sse_event(SSEEvent.DELTA, {"delta": str(data.get("delta") or "")})
                    continue
                if event == SSEEvent.ERROR:
                    yield _sse_event(SSEEvent.ERROR, {"error": str(data.get("error") or "查询失败，请稍后重试")})
                    return
                if event != "workflow_complete":
                    continue

                sources = await _hydrate_sources(session, payload.library_id, data.get("sources") or [])
                answer = str(data.get("answer") or "").strip()
                image_urls = [str(url) for url in data.get("image_urls") or [] if str(url).strip()]
                if not sources and not answer:
                    answer = "当前知识库中没有找到与该问题相关的内容。"
                response = QueryResponse(
                    library_id=payload.library_id,
                    session_id=session_id,
                    query=payload.query,
                    answer=answer,
                    sources=sources,
                    image_urls=image_urls,
                )
                await _persist_assistant_message(
                    user, session_id, answer, sources, image_urls,
                    clarify_candidates=[
                        str(c) for c in data.get("clarify_candidates") or [] if str(c).strip()
                    ],
                )
                yield _sse_event(SSEEvent.FINAL, response.model_dump(mode="json"))
                return
        except asyncio.CancelledError:
            raise
        finally:
            remove_sse_queue(stream_id)
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
