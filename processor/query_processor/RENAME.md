# Query Processor 检索问答模块说明

## 1. 模块简介

`processor/query_processor/` 负责将用户问题转换为可执行的知识库检索流程，并根据召回内容生成最终回答。模块支持本地向量检索、HyDE、可选联网搜索、RRF 融合、重排序、来源整理和流式答案输出。

## 2. 检索流程

```text
node_prepare_query
    │
    ├─ 识别问题类型、上下文和检索开关
    ├─ 若已有直接答案，直接进入答案节点
    ▼
node_search_embedding
    │
    ├─ 本地向量召回
    ├─ 根据状态决定是否执行 HyDE 或联网搜索
    ├──────────────┐
    ▼              ▼
node_search_embedding_hyde  node_web_search_mcp（可选）
    │              │
    └──────┬───────┘
           ▼
      node_rrf
           │
           ├─ 合并不同来源的候选结果
           ▼
      node_rerank
           │
           ├─ BGE Reranker 重新排序
           ▼
      node_source_collection
           │
           ├─ 补齐文档、切片、页码、标题和图片来源
           ▼
      node_answer_output
           │
           └─ 生成最终答案或流式增量
```

## 3. 目录与文件职责

- `main_graph.py`：注册查询节点、配置条件路由、编译并运行工作流。
- `state.py`：定义问题、会话、检索开关、召回结果、来源和答案状态。
- `base.py`：提供查询处理的基础能力和共享约定。
- `nodes/node_prepare_query.py`：准备问题、会话上下文和检索策略。
- `nodes/node_search_embedding.py`：执行本地向量召回。
- `nodes/node_search_embedding_hyde.py`：生成假设文档并执行辅助召回。
- `nodes/node_web_search_mcp.py`：按开关调用 MCP Web Search。
- `nodes/node_rrf.py`：融合多路检索结果。
- `nodes/node_rerank.py`：使用重排序模型提升候选顺序。
- `nodes/node_source_collection.py`：收集并清洗来源信息。
- `nodes/node_answer_output.py`：根据提示词生成回答、图片和最终输出。
- `prompt/answer_prompt.py`：答案生成规则、引用和图片输出约束。
- `prompt/search_embedding_hyde.py`：HyDE 查询提示词。
- `prompt/item_name_confirm.py`：商品或实体名称确认提示词。

## 4. 输入与输出

### 输入

- 用户问题。
- 当前会话历史和会话 ID。
- 目标知识库 ID。
- 检索开关，例如是否启用 HyDE、联网搜索或实体过滤。
- Embedding、Reranker、Milvus、MongoDB 和模型配置。

### 输出

- 最终回答文本。
- 结构化来源：文档、切片、页码、标题、分数和内容。
- 关联图片地址。
- 流式响应事件或普通 QueryResponse。
- 会话消息和问答历史。

## 5. 后端调用链

```text
frontend/src/components/user/UserChatPanel.vue
    ↓
frontend/src/api/query.ts
    ↓
backend/app/api/query.py
    ↓
processor/query_processor/main_graph.py
    ↓
Milvus / MySQL / MongoDB / LLM
    ↓
普通响应或 SSE
```

`backend/app/api/query.py` 负责认证、会话、知识库状态、来源补齐和历史保存；Processor 负责检索与答案生成，不应绕过后端直接向前端暴露接口。

## 6. 检索质量与图片规则

- 本地召回结果必须限定在当前知识库范围内。
- 来源补齐应以 MySQL 中仍有效的文档和切片为准。
- 已删除、未完成或不属于当前知识库的切片不能继续作为来源。
- 图片必须来自当前检索内容或明确关联的来源，不能把同一批召回中的无关商品图片带入回答。
- `MINIO_IMG_DIR` 错误会导致图片地址异常，历史文档通常需要重新导入。
- 回答提示词、来源收集和前端图片渲染需要保持结构化字段一致。

## 7. 调试方法

### 没有召回结果

检查知识库状态、文档状态、Milvus 集合、Embedding 维度、相似度阈值和文档是否已完成导入。

### 召回了其他商品或文档

检查实体识别、知识库 ID 过滤、文档来源过滤、RRF 合并和重排序前后的候选集合。

### 回答与来源不一致

检查 `node_source_collection.py` 是否按向量 ID 补齐了正确文档，检查答案提示词是否收到正确的来源上下文。

### SSE 中断

检查后端流式接口、模型响应、客户端 AbortController 和 MongoDB 历史保存异常。

### 图片不显示

检查来源中的 `image_urls`、MinIO 对象是否存在、URL 是否包含错误目录，以及前端是否将图片作为结构化字段渲染。

## 8. 后续方向

1. 将用户或角色的知识库授权范围注入检索入口和所有召回节点。
2. 建立检索评测集，评估召回准确率、排序质量、引用准确率和图片相关性。
3. 增强多轮会话中的指代消解和实体约束，减少“这款”“上一张”等指代错误。
4. 支持结构化回答、代码块、表格和图片的统一输出协议。
5. 增加节点级耗时、召回数量、过滤数量和模型调用失败率监控。