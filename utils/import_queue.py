"""Reliable Redis-backed queue primitives for import tasks."""

from __future__ import annotations

import json
import time
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

from config.redis_config import redis_config


def create_redis_client(*, socket_timeout: float | None = 5) -> Redis:
    """Create an asynchronous Redis client from project configuration."""
    return Redis.from_url(
        redis_config.url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=socket_timeout,
    )


def _queued_key(task_id: int) -> str:
    """Return the deduplication key for a queued task."""
    return f"{redis_config.queue_name}:queued:{int(task_id)}"


def _lock_key(task_id: int) -> str:
    """Return the processing lock key for a task."""
    return f"{redis_config.queue_name}:lock:{int(task_id)}"


def _processing_key(task_id: int) -> str:
    """Return the durable processing payload key for a task."""
    return f"{redis_config.queue_name}:processing:{int(task_id)}"


def _lease_key() -> str:
    """Return the sorted-set key containing processing lease deadlines."""
    return f"{redis_config.queue_name}:leases"


def _cancel_key(task_id: int) -> str:
    """Return the cancellation marker key for a task."""
    return f"{redis_config.queue_name}:cancel:{int(task_id)}"


async def enqueue_import_task(
    task_id: int,
    *,
    redis_client: Redis | None = None,
    attempt: int = 0,
    queue_name: str | None = None,
) -> bool:
    """Enqueue a task once and return whether a new queue item was created."""
    client = redis_client or create_redis_client()
    owns_client = redis_client is None
    name = queue_name or redis_config.queue_name
    queued_key = f"{name}:queued:{int(task_id)}"
    try:
        payload = json.dumps({"task_id": int(task_id), "attempt": int(attempt)}, ensure_ascii=False)
        created = await client.eval(
            """
            if redis.call('EXISTS', KEYS[1]) == 1 then
                return 0
            end
            redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
            redis.call('LPUSH', KEYS[2], ARGV[1])
            return 1
            """,
            2,
            queued_key,
            name,
            payload,
            redis_config.visibility_timeout_seconds,
        )
        return bool(created)
    finally:
        if owns_client:
            await client.aclose()


async def dequeue_import_task(
    *,
    redis_client: Redis | None = None,
    timeout: int = 5,
    queue_name: str | None = None,
) -> tuple[int, int] | None:
    """Pop one queued task, acquire its processing lock, and create a lease."""
    client = redis_client or create_redis_client()
    owns_client = redis_client is None
    name = queue_name or redis_config.queue_name
    try:
        while True:
            try:
                result = await client.brpop(name, timeout=timeout)
            except RedisTimeoutError:
                return None
            if result is None:
                return None
            _, raw_payload = result
            payload: dict[str, Any] = json.loads(raw_payload)
            task_id = int(payload["task_id"])
            attempt = int(payload.get("attempt", 0))
            await client.delete(f"{name}:queued:{task_id}")
            lock_key = f"{name}:lock:{task_id}"
            acquired = await client.set(lock_key, raw_payload, nx=True, ex=redis_config.visibility_timeout_seconds)
            if not acquired:
                continue
            processing_key = f"{name}:processing:{task_id}"
            deadline = time.time() + redis_config.visibility_timeout_seconds
            await client.set(processing_key, raw_payload, ex=redis_config.visibility_timeout_seconds)
            await client.zadd(f"{name}:leases", {str(task_id): deadline})
            return task_id, attempt
    finally:
        if owns_client:
            await client.aclose()


async def acknowledge_import_task(task_id: int, *, redis_client: Redis | None = None, queue_name: str | None = None) -> None:
    """Remove queue lock and lease after a task reaches a terminal state."""
    client = redis_client or create_redis_client()
    owns_client = redis_client is None
    name = queue_name or redis_config.queue_name
    try:
        await client.delete(
            f"{name}:lock:{int(task_id)}",
            f"{name}:processing:{int(task_id)}",
            f"{name}:queued:{int(task_id)}",
            f"{name}:cancel:{int(task_id)}",
        )
        await client.zrem(f"{name}:leases", str(int(task_id)))
    finally:
        if owns_client:
            await client.aclose()


async def release_processing_import_task(task_id: int, *, redis_client: Redis | None = None, queue_name: str | None = None) -> None:
    """Release only the processing lease before a task is requeued for retry."""
    client = redis_client or create_redis_client()
    owns_client = redis_client is None
    name = queue_name or redis_config.queue_name
    try:
        await client.delete(f"{name}:lock:{int(task_id)}", f"{name}:processing:{int(task_id)}")
        await client.zrem(f"{name}:leases", str(int(task_id)))
    finally:
        if owns_client:
            await client.aclose()


async def refresh_import_lease(task_id: int, *, redis_client: Redis | None = None, queue_name: str | None = None) -> None:
    """Extend the processing lock and lease while a worker is running a task."""
    client = redis_client or create_redis_client()
    owns_client = redis_client is None
    name = queue_name or redis_config.queue_name
    try:
        raw_payload = await client.get(f"{name}:processing:{int(task_id)}")
        if raw_payload is None:
            return
        deadline = time.time() + redis_config.visibility_timeout_seconds
        await client.expire(f"{name}:lock:{int(task_id)}", redis_config.visibility_timeout_seconds)
        await client.expire(f"{name}:processing:{int(task_id)}", redis_config.visibility_timeout_seconds)
        await client.zadd(f"{name}:leases", {str(int(task_id)): deadline})
    finally:
        if owns_client:
            await client.aclose()


async def recover_expired_import_tasks(*, redis_client: Redis | None = None, queue_name: str | None = None) -> list[tuple[int, int]]:
    """Requeue tasks whose processing lease expired after a worker interruption."""
    client = redis_client or create_redis_client()
    owns_client = redis_client is None
    name = queue_name or redis_config.queue_name
    recovered: list[tuple[int, int]] = []
    try:
        expired_ids = await client.zrangebyscore(f"{name}:leases", min="-inf", max=time.time())
        for raw_task_id in expired_ids:
            task_id = int(raw_task_id)
            raw_payload = await client.get(f"{name}:processing:{task_id}")
            if raw_payload is None:
                await client.zrem(f"{name}:leases", raw_task_id)
                await client.delete(f"{name}:lock:{task_id}")
                continue
            payload = json.loads(raw_payload)
            attempt = int(payload.get("attempt", 0)) + 1
            await release_processing_import_task(task_id, redis_client=client, queue_name=name)
            if await enqueue_import_task(task_id, redis_client=client, attempt=attempt, queue_name=name):
                recovered.append((task_id, attempt))
        return recovered
    finally:
        if owns_client:
            await client.aclose()


async def request_import_cancellation(task_id: int, *, redis_client: Redis | None = None, queue_name: str | None = None) -> None:
    """Set a cancellation marker checked before a worker starts a task."""
    client = redis_client or create_redis_client()
    owns_client = redis_client is None
    name = queue_name or redis_config.queue_name
    try:
        await client.set(f"{name}:cancel:{int(task_id)}", "1", ex=redis_config.visibility_timeout_seconds)
    finally:
        if owns_client:
            await client.aclose()


async def is_import_cancelled(task_id: int, *, redis_client: Redis | None = None, queue_name: str | None = None) -> bool:
    """Return whether cancellation was requested for a task."""
    client = redis_client or create_redis_client()
    owns_client = redis_client is None
    name = queue_name or redis_config.queue_name
    try:
        return bool(await client.exists(f"{name}:cancel:{int(task_id)}"))
    finally:
        if owns_client:
            await client.aclose()
