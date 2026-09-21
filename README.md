# Knowledge Base 项目说明

## 1. 项目简介

Knowledge Base 是一个面向企业内部技术资料和产品资料的全栈知识库问答系统。项目将文档导入、内容解析、图片处理、知识切片、向量检索、重排序、答案生成和运营管理整合到统一服务中。

系统主要服务两类用户：

- 普通用户：选择知识库进行连续对话，查看回答来源和关联图片，管理自己的会话。
- 管理人员：管理用户、角色、权限、知识库、文档、标签、导入任务和操作日志。

项目当前已经从早期的 `web/` 原生页面和独立旧服务迁移到 `frontend/` + `backend/` 的统一架构。旧版 `web/` 目录及其兼容挂载已移除，当前运行不依赖旧页面。

## 2. 项目架构

```text
┌──────────────────────────────────────────────────────────────┐
│                         frontend/                            │
│ Vue 3 用户端 / 管理端                                       │
│ 会话交互、Markdown 展示、来源与图片、权限页面、数据管理       │
└──────────────────────────────┬───────────────────────────────┘
                               │ HTTP / SSE
┌──────────────────────────────▼───────────────────────────────┐
│                         backend/                             │
│ FastAPI 统一 API                                             │
│ 认证授权、会话、知识库、文档、导入任务、审计、系统设置         │
└───────────────┬──────────────────┬───────────────────────────┘
                │                  │
                │                  └──────────────┐
                │                                 │
┌───────────────▼──────────────┐  ┌───────────────▼─────────────┐
│          processor/           │  │       数据与基础设施          │
│ 文档导入工作流                │  │ MySQL：管理数据和权限        │
│ 知识库检索问答工作流          │  │ MongoDB：会话和消息历史      │
└───────────────┬──────────────┘  │ Milvus：向量索引              │
                │                 │ MinIO：文档图片和对象文件     │
                └─────────────────┴─────────────────────────────┘
```

### 2.1 文档导入链路

1. 管理端上传 PDF 或 Markdown 文件。
2. 后端创建文档记录和导入任务。
3. 导入工作流解析文件，必要时执行 PDF 转 Markdown。
4. 提取并规范化 Markdown 图片地址，图片写入 MinIO。
5. 按标题、段落、表格等内容切分知识块。
6. 按知识库配置执行实体识别。
7. 生成向量并写入 Milvus。
8. 将切片、来源位置和元数据持久化到 MySQL。
9. 管理端通过导入任务接口查看处理进度和错误信息。

### 2.2 用户问答链路

1. 用户端创建或切换会话并提交问题。
2. 后端校验用户、会话和知识库状态。
3. 查询工作流进行问题准备、向量召回、可选 HyDE、可选联网搜索、RRF 合并和重排序。
4. 从 MySQL 补齐文档、切片、页码、标题和图片等来源信息。
5. 生成带来源上下文的回答。
6. 通过普通响应或 SSE 流式返回答案、来源和关联图片。
7. 会话与消息历史写入 MongoDB。

## 3. 项目技术栈

### 3.1 前端

- Vue 3 + `<script setup>`
- TypeScript
- Vite
- TDesign Vue Next
- Pinia
- Vue Router
- Less
- Marked：Markdown 渲染

### 3.2 后端

- Python 3.11 及以上
- FastAPI
- Uvicorn
- SQLAlchemy Async
- Alembic
- Pydantic
- `asyncmy`：MySQL 异步驱动

### 3.3 AI 与工作流

- LangGraph：组织导入和问答节点流程
- LangChain：模型和文本处理集成
- DashScope 兼容 OpenAI API：大语言模型和视觉模型
- BGE-M3：向量嵌入
- BGE Reranker：召回结果重排序
- MinerU：PDF 文档解析
- MCP Web Search：可选联网搜索

### 3.4 数据与基础设施

- MySQL：用户、角色、权限、知识库、文档、切片、导入任务和操作日志
- MongoDB：会话、消息和问答历史
- Milvus：知识切片和商品实体向量检索
- MinIO：文档图片及对象文件
- Redis：导入任务队列和任务状态协作

## 4. 目录结构

```text
knowledge_base/
├─ backend/       FastAPI 后端、数据库模型、迁移和接口
├─ frontend/      Vue 用户端与管理端
├─ processor/     导入工作流与检索问答工作流
├─ config/        模型、数据库、MinIO、Milvus 等配置
├─ utils/         向量、Mongo、MinIO、Redis、日志等基础工具
├─ tool/          下载模型、日志等辅助工具
├─ storage/       本地处理文件和中间产物
├─ tmp/           项目规范、规划和临时资料
├─ pyproject.toml Python 依赖和 uv 配置
└─ .env.example   环境变量模板
```

## 5. 环境准备

### 5.1 必需软件

- Python 3.11+
- Node.js 和 npm
- MySQL
- MongoDB
- Milvus
- MinIO
- Redis
- 可用的 LLM / Embedding / Reranker 模型或远程服务

项目没有提交统一的 Docker Compose 文件，MySQL、MongoDB、Milvus、MinIO 和 Redis 需要按实际部署环境自行启动。

### 5.2 配置环境变量

复制 `.env.example` 为 `.env`，至少检查以下配置：

- `MYSQL_URL`
- `MONGO_URL`、`MONGO_DB_NAME`
- `MILVUS_URL`
- `MINIO_ENDPOINT`、`MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY`、`MINIO_BUCKET_NAME`
- `MINIO_IMG_DIR`
- `REDIS_URL`、`REDIS_QUEUE_NAME`
- `OPENAI_API_KEY`、`OPENAI_API_BASE`
- `JWT_SECRET`
- `SETTINGS_ENCRYPTION_KEY`

`JWT_SECRET` 和 `SETTINGS_ENCRYPTION_KEY` 不能在生产环境使用示例值，也不要提交到 Git。

## 6. 项目启动说明

### 6.1 安装 Python 依赖

推荐使用 uv：

```powershell
uv sync
```

也可以使用已经准备好的 Python 虚拟环境安装 `pyproject.toml` 中的依赖。

### 6.2 初始化数据库

```powershell
alembic upgrade head
uv run python -m backend.scripts.init_data
```

`init_data` 会幂等初始化角色、权限、示例用户和基础业务数据。首次启动前必须确保 `MYSQL_URL` 可连接。

### 6.3 启动后端

在项目根目录执行：

```powershell
uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload
```

也可以直接使用：

```powershell
python -m backend.app.main
```

后端地址：

- API：`http://127.0.0.1:8000`
- Swagger：`http://127.0.0.1:8000/docs`
- ReDoc：`http://127.0.0.1:8000/redoc`
- 健康检查：`http://127.0.0.1:8000/health`

### 6.4 启动前端

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

前端开发地址通常为 Vite 输出的本地地址，例如 `http://127.0.0.1:5173`。前端 API 默认访问 `http://127.0.0.1:8000`，如需修改请配置 `VITE_API_BASE_URL`。

生产构建：

```powershell
npm.cmd run build
```

## 7. 常用验证命令

```powershell
python -m py_compile backend/app/main.py backend/app/api/auth.py backend/app/api/mysql.py backend/app/api/query.py
npm.cmd run build --prefix frontend
git diff --check
```

Windows PowerShell 下如果 `npm` 命令被执行策略拦截，使用 `npm.cmd`。

## 8. 当前功能范围

- 用户登录、刷新令牌、退出登录和权限上下文查询。
- 用户端多会话、会话切换、重命名、删除和历史消息展示。
- 知识库、文档、标签和导入任务管理。
- 角色、权限、用户和操作日志管理。
- PDF、Markdown 导入和图片对象存储。
- 本地向量召回、HyDE、RRF、重排序和来源追溯。
- Markdown 富文本、代码块、图片回复和回答复制。
- 管理端仪表盘统计和最近活动展示。

## 9. 后续规划

1. 完善会话生命周期管理，包括归档、批量操作、搜索、状态和更细粒度的历史管理。
2. 实现知识库权限管理，支持为指定用户或角色授权，并在知识库列表、会话创建、历史读取和检索接口统一校验。
3. 完善权限审计，记录授权、撤销、越权拦截和管理操作。
4. 优化图片与回答内容的关联，避免同批召回中的无关图片被带入答案。
5. 增强检索质量评估、召回过滤、实体识别和回答引用准确性。
6. 逐步补充自动化测试、部署脚本和生产环境监控。

## 10. 开发约束

- 不执行 `git reset`，不回退或覆盖无关工作区修改。
- 敏感配置只放在 `.env` 或部署环境，不写入源码和文档。
- 修改后端代码后必须重启 Uvicorn 服务。
- 导入和检索涉及外部服务，排查问题时优先检查 MySQL、MongoDB、Milvus、MinIO、Redis 连通性。
- 新增权限必须同步更新权限注册表和接口映射。