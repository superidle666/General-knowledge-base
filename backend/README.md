# Backend 后端说明

## 1. 模块简介

`backend/` 是 Knowledge Base 的 FastAPI 后端，负责统一 API、认证授权、会话持久化、管理端业务、导入任务调度和系统启动。后端不再依赖旧 `web/` 服务，当前所有新前端功能均通过 `backend.app.main:app` 暴露。

## 2. 后端架构

```text
backend/
├─ app/main.py             FastAPI 应用入口、生命周期和路由注册
├─ app/api/
│  ├─ auth.py              登录、刷新令牌、退出登录、当前用户
│  ├─ health.py            健康检查、数据库探针、API 能力列表
│  ├─ query.py             用户端知识库、会话、问答和 SSE
│  ├─ mysql.py             管理端用户、角色、权限、知识库、文档、任务、日志
│  ├─ settings.py          系统设置读取与维护
│  └─ schemas.py           Pydantic 请求和响应模型
├─ app/core/
│  ├─ auth.py              JWT、密码哈希、用户解析、权限依赖
│  ├─ permissions.py       权限注册表和权限展开
│  ├─ audit.py             操作审计上下文和日志
│  ├─ config.py             环境变量和应用配置
│  └─ system_settings.py   加密系统设置同步
├─ app/db/
│  ├─ models.py            SQLAlchemy 数据模型
│  └─ session.py           异步数据库引擎和会话
├─ alembic/                 数据库迁移配置和版本
└─ scripts/init_data.py     初始化角色、权限和演示数据
```

## 3. 技术栈

- Python 3.11+
- FastAPI
- Uvicorn
- SQLAlchemy Async + `asyncmy`
- Alembic
- Pydantic
- MySQL
- MongoDB / PyMongo
- Milvus
- MinIO
- Redis

## 4. API 分组

### 4.1 认证接口

前缀：`/api/v1/auth`

- `POST /login`：账号密码登录并签发访问、刷新令牌。
- `POST /refresh`：轮换刷新令牌。
- `POST /logout`：撤销当前刷新令牌。
- `GET /me`：返回当前用户、角色和权限。

### 4.2 用户问答接口

前缀：`/api/v1/query`

- `GET /libraries`：查询可用知识库。
- `POST /sessions`：创建会话。
- `GET /sessions`：查询会话列表。
- `GET /sessions/{id}/messages`：读取会话消息。
- `DELETE /sessions/{id}`：删除会话。
- `POST /`：普通问答。
- `POST /stream`：SSE 流式问答。

### 4.3 管理接口

前缀：`/api/v1`

覆盖用户、角色、权限、知识库、成员、文档、标签、导入任务、操作日志和仪表盘。

所有管理路由通过 `require_management_access` 进行权限校验。系统管理员拥有管理端全量权限，其他用户根据角色分配的权限编码访问资源。

## 5. 数据职责

- MySQL：用户、角色、权限、知识库、成员关系、文档、文档切片、标签、导入任务、操作日志和系统设置。
- MongoDB：会话、用户消息、助手消息、来源和问答历史。
- Milvus：向量检索集合，包括文档切片和实体相关集合。
- MinIO：上传文件或解析过程中产生的图片和对象。
- Redis：导入任务队列、任务状态、重试和取消协作。

## 6. 启动说明

### 6.1 环境变量

在项目根目录准备 `.env`，重点配置：

```text
MYSQL_URL=...
MONGO_URL=...
MONGO_DB_NAME=...
MILVUS_URL=...
MINIO_ENDPOINT=...
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
MINIO_BUCKET_NAME=...
MINIO_IMG_DIR=...
REDIS_URL=...
OPENAI_API_KEY=...
OPENAI_API_BASE=...
JWT_SECRET=...
SETTINGS_ENCRYPTION_KEY=...
```

### 6.2 数据库迁移和初始化

```powershell
alembic upgrade head
uv run python -m backend.scripts.init_data
```

迁移脚本位于 `backend/alembic/versions/`。生产环境不得使用手工修改表结构代替 Alembic 迁移。

### 6.3 启动服务

```powershell
uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload
```

启动生命周期会：

1. 检查 `JWT_SECRET`。
2. 检查 `SETTINGS_ENCRYPTION_KEY`。
3. 同步代码权限注册表。
4. 同步系统设置元数据。

## 7. 调试和排障

- 返回 401：检查 Bearer Token、令牌有效期和账号状态。
- 返回 403：检查角色权限、接口权限映射和账号是否为系统管理员。
- 导入任务不动：检查 Redis、文件存储和处理器日志。
- 图片地址异常：检查 `MINIO_IMG_DIR`、MinIO 桶和文档重新导入情况。
- 来源为空：检查 Milvus 向量、MySQL 文档状态和文档切片是否为 `ready`。
- 修改后端代码无效：重启 Uvicorn，确认没有旧进程占用端口。

## 8. 后续方向

1. 将知识库成员关系真正落实到知识库列表、会话创建、历史读取和检索链路。
2. 支持指定用户和角色的知识库授权、批量授权、撤销和有效期。
3. 完善权限变更审计和越权访问拦截日志。
4. 增加 API 集成测试、认证测试、导入任务测试和检索链路测试。
5. 完善后台任务监控、失败重试、超时回收和分布式部署能力。