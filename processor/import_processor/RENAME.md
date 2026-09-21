# Import Processor 文档导入模块说明

## 1. 模块简介

`processor/import_processor/` 负责将 PDF、Markdown 等原始资料转换为可检索的知识数据。模块通过 LangGraph 组织处理节点，最终生成文档切片、向量、来源元数据和图片对象。

## 2. 导入流程

```text
node_entry
    │
    ├─ 文件与任务初始化
    ▼
node_pdf_to_md
    │
    ├─ PDF 解析 / Markdown 规范化
    ▼
node_md_img
    │
    ├─ 提取图片、上传 MinIO、替换图片地址
    ▼
node_document_split
    │
    ├─ 按标题、段落、表格等结构生成切片
    ├─ 根据 entity_recognition_mode 选择实体识别节点
    ▼
node_entity_recognition_*
    │
    ├─ disabled：不识别实体
    ├─ optional：按条件识别实体
    └─ required：强制识别实体
    ▼
node_bge_embedding
    │
    ├─ 生成切片向量
    ▼
node_import_milvus
    │
    ├─ 写入 Milvus 向量集合
    ▼
node_persist_chunks
    │
    └─ 持久化 MySQL 文档切片和元数据
```

实际节点注册和路由见 `main_graph.py`。

## 3. 目录与文件职责

- `main_graph.py`：创建 LangGraph、注册节点、配置条件路由。
- `worker.py`：由导入任务队列调用，执行工作流并更新任务状态。
- `state.py`：定义任务 ID、文件路径、文档信息、节点状态和错误信息。
- `import_config.py`：导入阶段的默认配置和处理参数。
- `content_blocks.py`：标题、段落、表格等内容块的数据结构。
- `exceptions.py`：导入阶段的可识别异常类型。
- `nodes/node_entry.py`：准备导入上下文和输入文件。
- `nodes/node_pdf_to_md.py`：完成 PDF 到 Markdown 的解析。
- `nodes/node_md_img.py`：处理 Markdown 图片和 MinIO 对象地址。
- `nodes/node_document_split.py`：按内容结构生成知识切片。
- `nodes/node_entity_recognition_*.py`：按配置执行实体识别策略。
- `nodes/node_bge_embedding.py`：调用 Embedding 模型生成向量。
- `nodes/node_import_milvus.py`：写入 Milvus。
- `nodes/node_persist_chunks.py`：将切片和来源信息保存到 MySQL。

## 4. 输入与输出

### 输入

- 后端创建的导入任务。
- 原始 PDF 或 Markdown 文件路径。
- 目标知识库 ID 和文档 ID。
- 知识库的文件类型、切片、实体识别等配置。
- 模型、MinIO、Milvus 和数据库配置。

### 输出

- 文档处理状态：排队、处理中、完成、失败或取消。
- Markdown 或中间解析文件。
- MinIO 图片对象及可访问地址。
- 文档切片、标题路径、页码、图片元数据和实体元数据。
- Milvus 向量记录。
- MySQL 文档切片记录。

## 5. 运行方式

导入模块不单独提供 HTTP 服务，通常由以下链路触发：

```text
管理端上传
→ backend/app/api/mysql.py
→ utils/import_queue.py
→ processor/import_processor/worker.py
→ KBImportWorkflow
```

后端启动后，管理端上传接口会创建导入任务。任务执行需要 Redis 队列和相关外部服务正常运行。

## 6. 配置要求

重点环境变量：

- `MD_ROOT_DIR`：本地解析和临时文件根目录。
- `MINIO_ENDPOINT`：MinIO 地址。
- `MINIO_BUCKET_NAME`：图片和对象存储桶。
- `MINIO_IMG_DIR`：图片对象前缀，不能为空或被配置为 `None`。
- `MILVUS_URL`：向量库地址。
- `DOCUMENT_CHUNKS_V2_COLLECTION`：文档切片向量集合。
- `BGE_M3_PATH`、`BGE_DEVICE`、`BGE_FP16`：Embedding 配置。
- `REDIS_URL`、`REDIS_QUEUE_NAME`：任务队列配置。

## 7. 常见问题

- 图片 URL 中出现 `/None/`：通常是 `MINIO_IMG_DIR` 未配置，需要修复配置后重新导入历史文档。
- 文档状态一直为 `processing`：检查 Redis worker、任务超时和处理节点日志。
- 切片没有向量：检查 Embedding 模型路径、设备和 Milvus 连通性。
- 文档可以查看但问答搜不到：检查 `node_import_milvus`、`node_persist_chunks` 和文档状态是否为 `ready`。
- 重新导入产生重复内容：确认旧向量和旧切片是否已清理，检查文档版本与任务状态。

## 8. 后续方向

1. 增加导入任务的可恢复 checkpoint 和节点级重试。
2. 支持增量导入、文档版本和变更检测。
3. 将用户知识库权限作为导入前置校验和任务上下文。
4. 增强图片与所在章节、切片和商品实体的关联。
5. 增加导入结果完整性检查，避免 MySQL、Milvus 和 MinIO 状态不一致。