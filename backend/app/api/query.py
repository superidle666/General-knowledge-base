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
from backend.app.core.auth import AuthenticatedUser, get_current_user, get_optional_current_user
from backend.app.db.models import Document, DocumentChunk, Library
from backend.app.db.session import get_db
from processor.query_processor.main_graph import KBQueryWorkflow
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


async def _hydrate_sources(
    session: AsyncSession,
    library_id: int,
    sources: list[dict],
) -> list[QuerySourceOut]:
    """以 MySQL 为准补齐来源展示信息，并过滤已删除或未完成文档。"""
    source_by_vector_id = {
        str(source["chunk_id"]): source
        for source in sources
        if source.get("source", "local") == "local" and source.get("chunk_id") is not None
    }
    if not source_by_vector_id:
        return []
    rows = await session.execute(
        select(DocumentChunk, Document)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.library_id == library_id,
            Document.status == "ready",
            Document.deleted_at.is_(None),
            DocumentChunk.vector_id.in_(source_by_vector_id),
        )
    )
    hydrated: list[QuerySourceOut] = []
    for chunk, document in rows:
        source = source_by_vector_id.get(str(chunk.vector_id))
        if source is None:
            continue
        metadata = chunk.extra_metadata or {}
        hydrated.append(QuerySourceOut(
            chunk_id=chunk.vector_id,
            document_id=document.id,
            document_title=document.title,
            chunk_index=chunk.chunk_index,
            page_number=chunk.page_number,
            section_title=chunk.section_title,
            parent_title=str(metadata.get("parent_title") or "") or None,
            content=chunk.content,
            score=source.get("score"),
            source="local",
        ))
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
    await _get_active_library(session, payload.library_id)
    session_id, history = await _prepare_session_context(payload, user)
    try:
        workflow = KBQueryWorkflow()
        result = workflow.run({
            "library_id": payload.library_id,
            "original_query": payload.query,
            "session_id": session_id,
            "history": history,
            "top_k": payload.top_k,
            "use_hyde": payload.use_hyde,
            "use_web_search": payload.use_web_search,
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
    await _persist_assistant_message(user, session_id, answer, sources, image_urls)
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
    await _get_active_library(session, payload.library_id)
    session_id, history = await _prepare_session_context(payload, user)
    stream_id = f"query-stream-{uuid.uuid4().hex}"
    create_sse_queue(stream_id)

    def run_workflow() -> None:
        try:
            result = KBQueryWorkflow().run({
                "library_id": payload.library_id,
                "original_query": payload.query,
                "session_id": stream_id,
                "history": history,
                "top_k": payload.top_k,
                "use_hyde": payload.use_hyde,
                "use_web_search": payload.use_web_search,
                "is_stream": True,
                "persist_history": False,
            })
            push_to_session(stream_id, "workflow_complete", {
                "answer": str(result.get("answer") or "").strip(),
                "sources": result.get("sources") or [],
                "image_urls": result.get("image_urls") or [],
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
                await _persist_assistant_message(user, session_id, answer, sources, image_urls)
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
