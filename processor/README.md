# Processor 知识处理模块说明

## 1. 模块简介

`processor/` 是 Knowledge Base 的知识处理核心，使用 LangGraph 将文档导入和知识库问答拆分为可组合、可观测的节点。后端负责接口、权限和任务生命周期，Processor 负责真正的内容解析、切分、向量化、召回和答案生成。

## 2. 模块架构

```text
processor/
├─ import_processor/        文档导入工作流
│  ├─ main_graph.py         导入图和节点路由
│  ├─ worker.py             后台任务执行入口
│  ├─ state.py              导入状态
│  ├─ import_config.py      导入配置
│  ├─ content_blocks.py     内容块结构
│  ├─ nodes/                导入节点
│  └─ exceptions.py         导入异常
└─ query_processor/         知识库检索问答工作流
   ├─ main_graph.py         查询图和条件路由
   ├─ state.py              查询状态
   ├─ base.py               查询基础能力
   ├─ nodes/                召回、重排、来源和答案节点
   └─ prompt/               查询改写和答案提示词
```

## 3. 技术栈

- Python 3.11+
- LangGraph：工作流编排和节点状态传递
- LangChain：模型、文档和文本处理集成
- MinerU / PDF 解析：文档结构化
- BGE-M3：Embedding
- BGE Reranker：重排序
- Milvus：向量检索
- MinIO：图片和对象存储
- MySQL：文档、切片及元数据持久化
- DashScope 兼容模型服务：实体识别、问题处理和答案生成

## 4. 工作流边界

- Processor 不负责用户登录和权限判断。
- Processor 接收后端传入的知识库、文档和查询上下文。
- 后端负责在调用前校验用户是否有权访问目标知识库。
- Processor 只处理当前任务传入范围内的文档和检索结果。
- 导入结果必须携带知识库、文档、切片和来源信息，便于问答阶段回溯。

## 5. 导入模块

详见 `processor/import_processor/RENAME.md`。

导入模块负责：

1. 校验文件和导入配置。
2. 将 PDF 或 Markdown 转换为统一的 Markdown 内容。
3. 提取、上传并替换文档中的图片地址。
4. 按内容结构切分文档。
5. 根据配置执行实体识别。
6. 生成 Embedding。
7. 写入 Milvus 和 MySQL。

## 6. 检索模块

详见 `processor/query_processor/RENAME.md`。

检索模块负责：

1. 分析用户问题和会话上下文。
2. 执行本地向量召回。
3. 按配置启用 HyDE 或联网搜索。
4. 使用 RRF 合并不同检索结果。
5. 使用 Reranker 重新排序。
6. 整理来源、图片和文档元数据。
7. 生成最终回答或流式增量回答。

## 7. 配置与运行

Processor 通常由后端导入任务或问答接口调用，不建议单独启动为 HTTP 服务。开发调试时应从项目根目录加载 `.env`，确保模型、Milvus、MinIO、MySQL、MongoDB 和 Redis 配置完整。

导入任务入口主要由：

- `backend/app/api/mysql.py`
- `utils/import_queue.py`
- `processor/import_processor/worker.py`
- `processor/import_processor/main_graph.py`

问答入口主要由：

- `backend/app/api/query.py`
- `processor/query_processor/main_graph.py`
- `utils/mongo_history_utils.py`

## 8. 调试重点

- 导入失败：按任务状态、当前步骤、异常信息检查节点日志。
- 图片不显示：检查 MinIO 桶、`MINIO_IMG_DIR`、对象访问地址和历史文档是否需要重新导入。
- 召回结果不准确：检查 Embedding、Milvus 集合、相似度阈值和 Reranker 配置。
- 回答引用错误：检查来源收集节点是否过滤了其他知识库、文档或商品实体。
- 重复处理：检查 Redis 任务状态、任务重试和文档状态转换。

## 9. 后续方向

1. 将知识库权限范围作为 Processor 的强制输入和检索过滤条件。
2. 强化实体识别与商品范围过滤，减少跨商品图片和内容串入回答。
3. 建立导入和问答的节点级耗时、失败率、召回质量监控。
4. 增加离线评测集，评估召回准确率、引用准确率和图片相关性。
5. 支持更细粒度的文档版本、增量导入和向量删除。