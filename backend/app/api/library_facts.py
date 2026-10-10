# -*- coding: utf-8 -*-
"""库元数据事实的唯一读取层（元数据闸的数据源）。

要回答的是"本库有哪些产品/有多少份文档/开了 HyDE 没"这类**库级元问题**——它们的
事实全在 MySQL 里，与切片内容无关，让内容检索去答必然落在手册前言上（见
`processor/query_processor/inventory_gate.py` 记的那次线上复现）。

为什么读库放在接口层而不是查询图里（这条决定形态的硬约束，别处别再试第二次）：
查询图由 `asyncio.to_thread` 承载同步链路，跑在**工作线程**里，而 MySQL 只有
asyncmy 异步引擎、连接池与事件循环绑定。于是图内节点想读库只有两条路，都不通：
- 在工作线程 `asyncio.run(SessionLocal())`：可能取到主循环创建的连接，跨事件循环，
  行为未定义；
- 照抄导入侧的 `run_coroutine_threadsafe(coroutine, database_event_loop)`：**必然死锁**，
  因为主线程正 `await` 等这个 worker，而 worker 在等主循环腾出来。导入侧敢这么写是因为
  worker 是独立进程、有自己的常驻循环，API 进程没有这个条件。
所以本模块只在异步接口层被调用，产出一个纯 dict 画像注入 state，图里只读 dict、不碰库。

口径唯一化：`ready + 未删` 这套条件原本在 query.py、mysql.py、eval/ 两处各抄了一遍
（共四处独立复制，改一处忘三处，而漏写的表现不是报错是"清单里混进待确认和已删除的文档"）。
这里导出常量给接口层两处消费；eval 那两份是离线脚本、自带连库代码，不动。

@FilePath: backend/app/api/library_facts.py
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Document, DocumentChunk, Entity, Library

# 就绪文档口径：元数据事实只统计这个集合，与"可召回切片"保持一致。
READY_DOCUMENT_CLAUSES: tuple = (
    Document.status == "ready",
    Document.deleted_at.is_(None),
)
# 在册文档口径：只排除软删除，用于"总共有多少份"这类包含处理中/失败的全量计数。
ALIVE_DOCUMENT_CLAUSES: tuple = (
    Document.deleted_at.is_(None),
)
# 尚未就绪但也还没失败的中间态（上传完/处理中/等人工确认）。
PENDING_DOCUMENT_STATUSES: tuple[str, ...] = ("uploaded", "processing", "needs_review")

# 清单上界：画像是要塞进 state、并最终只展示前几项的中间数据，不能随库规模线性膨胀。
# 计数一律走 SQL 聚合（不受这两个上界影响），只有清单列表被截断，截断事实由画像带出去，
# 渲染层据此补"以上为前 N 项"，绝不把"列出数"当成"总数"报给用户。
MAX_FACTS_ENTITIES = 200
MAX_FACTS_DOCUMENTS = 50


def empty_library_facts(library_id: int | None, library: Library | None = None) -> dict:
    """不装载时用的空画像：`loaded=False`，调用方据此走降级路径。

    刻意带上 `library_id` 与识别模式：图里的闸要靠模式判断"这个库本就不该启用元数据闸"，
    而不是靠画像里有没有数据去猜（猜错的表现是静默不答）。
    """
    return {
        "loaded": False,
        "library_id": library_id,
        "library_name": str(getattr(library, "name", "") or ""),
        "entity_recognition_mode": str(getattr(library, "entity_recognition_mode", None) or "disabled"),
    }


def _clean_str_list(value: object) -> list[str]:
    """JSON 列（别名）转干净的字符串列表：非 list、空串、非字符串一律丢掉。"""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


async def load_library_facts(session: AsyncSession, library_id: int, library: Library) -> dict:
    """读本库元数据事实，产出可直接注入查询 state 的紧凑画像。

    三条聚合 + 两条有界清单，合计五次往返，与文档数无关（聚合不随规模线性增长，
    清单被上界截断）。任何一条失败都由调用方兜成 `loaded=False`，不在这层做降级判断。
    """
    ready_clauses = (*READY_DOCUMENT_CLAUSES, Document.library_id == library_id)

    # 一、在册/就绪/失败/中间态四个文档计数。分四条 count 而不是一条条件聚合：
    # 表上有 (library_id, status) 联合索引，每条都是索引区间扫描，合成一条反而要用
    # CASE 表达式丢掉这个索引，而且分条写每条口径都能单独读懂。
    total_document_count = int((await session.execute(
        select(func.count(Document.id)).where(Document.library_id == library_id, *ALIVE_DOCUMENT_CLAUSES)
    )).scalar_one() or 0)

    ready_document_count = int((await session.execute(
        select(func.count(Document.id)).where(*ready_clauses)
    )).scalar_one() or 0)
    failed_document_count = int((await session.execute(
        select(func.count(Document.id)).where(
            Document.library_id == library_id,
            Document.status == "failed",
            *ALIVE_DOCUMENT_CLAUSES,
        )
    )).scalar_one() or 0)
    pending_document_count = int((await session.execute(
        select(func.count(Document.id)).where(
            Document.library_id == library_id,
            Document.status.in_(PENDING_DOCUMENT_STATUSES),
            *ALIVE_DOCUMENT_CLAUSES,
        )
    )).scalar_one() or 0)

    # 二、切片总数：只算就绪文档的切片，与"可召回"口径一致（口径不同会被用户当成矛盾）
    chunk_total = int((await session.execute(
        select(func.count(DocumentChunk.id))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(*ready_clauses)
    )).scalar_one() or 0)

    # 三、实体计数与清单：口径必须与 query.py 的词表装载同一条，否则"有多少个产品"
    # 和"能报出哪些产品名"会对不上，而这两个数在同一句话里出现给用户看。
    entity_count = int((await session.execute(
        select(func.count(func.distinct(Entity.name)))
        .join(Document, Document.id == Entity.document_id)
        .where(Entity.library_id == library_id, *ready_clauses)
    )).scalar_one() or 0)
    entity_rows = (await session.execute(
        select(Entity.name, Entity.aliases, Entity.source, Entity.maybe_series, Entity.document_id)
        .join(Document, Document.id == Entity.document_id)
        .where(Entity.library_id == library_id, *ready_clauses)
        .order_by(Entity.id)
        .limit(MAX_FACTS_ENTITIES)
    )).all()
    # 同一规范名可能来自多篇文档（系列拆篇），展示口径按名去重、别名合并，
    # 否则用户会看到"共 12 个产品"却数出 15 个名字。
    entities: dict[str, dict] = {}
    for name, aliases, source, maybe_series, document_id in entity_rows:
        clean_name = str(name or "").strip()
        if not clean_name:
            continue
        bucket = entities.setdefault(clean_name, {
            "name": clean_name,
            "aliases": [],
            "source": str(source or ""),
            "maybe_series": bool(maybe_series),
            "document_id": document_id,
        })
        for alias in _clean_str_list(aliases):
            if alias not in bucket["aliases"]:
                bucket["aliases"].append(alias)

    # 四、文档清单：只按未删（不挑 ready）——用户问"有哪些文档"时，处理中的文档
    # 也该被看见，status 字段带出去由渲染层说明。倒序按发布时间，没发布过的按创建时间兜底。
    document_rows = (await session.execute(
        select(Document.id, Document.title, Document.status, Document.version)
        .where(Document.library_id == library_id, *ALIVE_DOCUMENT_CLAUSES)
        .order_by(func.coalesce(Document.published_at, Document.created_at).desc())
        .limit(MAX_FACTS_DOCUMENTS)
    )).all()

    return {
        "loaded": True,
        "library_id": library_id,
        "library_name": str(library.name or ""),
        "entity_recognition_mode": str(library.entity_recognition_mode or "disabled"),
        "entity_confirm_mode": str(library.entity_confirm_mode or "strict"),
        "use_hyde": bool(library.use_hyde),
        "visibility": str(library.visibility or ""),
        # 统计时刻：元数据答案必须带时效声明，画像是在这一刻算的，
        # 库里随后导入了新文档而用户拿旧答案核对时，这句话是唯一的解释。
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_document_count": total_document_count,
        "ready_document_count": ready_document_count,
        "failed_document_count": failed_document_count,
        "pending_document_count": pending_document_count,
        "chunk_total": chunk_total,
        "entity_count": entity_count,
        "entities": list(entities.values()),
        "documents": [
            {
                "id": document_id,
                "title": str(title or ""),
                "status": str(status or ""),
                "version": int(version or 1),
            }
            for document_id, title, status, version in document_rows
        ],
        "entities_truncated": entity_count > len(entities),
        "documents_truncated": total_document_count > len(document_rows),
    }
