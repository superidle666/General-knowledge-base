# Frontend 前端说明

## 1. 模块简介

`frontend/` 是 Knowledge Base 的 Vue 3 前端工程，统一提供用户端知识库问答工作台和管理端运营后台。前端通过 HTTP API 与 SSE 调用 FastAPI 后端，不再依赖旧版 `web/` 原生页面。

## 2. 前端架构

```text
src/
├─ pages/
│  ├─ user/       用户端问答、会话和登录页面
│  └─ admin/      管理端仪表盘、用户、角色、权限、知识库、文档页面
├─ components/
│  ├─ user/       用户端侧栏、顶部栏、聊天面板、来源面板
│  └─ admin/      管理端公共头部及业务组件
├─ layouts/       用户端和管理端整体布局
├─ api/           HTTP、SSE、认证和管理接口封装
├─ composables/   请求、分页、表格、弹窗和问答状态逻辑
├─ store/         Pinia 应用状态和用户认证状态
├─ router/        路由配置和登录访问控制
├─ types/         用户端和接口数据类型
├─ utils/         请求、日期、格式化、校验、本地存储工具
└─ style/         全局变量、重置、公共样式和布局样式
```

## 3. 技术栈

- Vue 3
- TypeScript
- Vite 8
- TDesign Vue Next
- Pinia
- Vue Router
- Less
- Marked
- `vue-tsc`：TypeScript 与 Vue 类型检查

依赖定义位于 `frontend/package.json`，锁定依赖以项目实际安装结果为准。

## 4. 页面与业务模块

### 4.1 用户端

- 选择有效知识库。
- 创建、切换、重命名和删除多个会话。
- 发送问题并接收普通或流式回答。
- 展示 Markdown 富文本、代码块、关联来源和回复图片。
- 复制回答文本或代码内容。
- 处理登录状态、退出登录和用户信息。

### 4.2 管理端

- 仪表盘统计和最近活动。
- 用户、角色、权限管理。
- 知识库创建、修改、软删除、恢复和成员管理。
- 文档上传、详情、标签、切片和导入任务管理。
- 操作日志查询。

## 5. 关键接口约定

默认后端地址为 `http://127.0.0.1:8000`，可通过 `VITE_API_BASE_URL` 覆盖。

- 用户认证：`/api/v1/auth/*`
- 用户问答：`/api/v1/query/*`
- 管理资源：`/api/v1/users`、`/api/v1/roles`、`/api/v1/libraries`、`/api/v1/documents`
- 导入任务：`/api/v1/import-tasks/*`
- 系统检查：`/health`、`/health/db`、`/api`

请求封装负责附加 Bearer Token，并统一处理 401、403 和后端错误信息。流式回答使用 SSE 事件解析，不应在页面组件中重复实现连接和令牌处理。

## 6. 启动说明

在项目根目录执行：

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

生产构建：

```powershell
npm.cmd run build
```

预览构建结果：

```powershell
npm.cmd run preview
```

前端启动前应确认后端已启动，并确认 `VITE_API_BASE_URL` 指向可访问的 FastAPI 服务。

## 7. 开发约定

- 页面业务逻辑优先放在页面或组合式函数中，API 调用统一放在 `src/api/`。
- 公共数据结构优先维护在 `src/types/`，避免页面内重复定义。
- 用户端和管理端样式应遵循现有 TDesign 风格，不随意改变整体布局。
- Vue、TypeScript、Less 文件按 `tmp/注释规范.md` 保持文件头和核心业务注释。
- 修改输入框、会话列表、消息渲染时，要同时考虑键盘操作、流式状态、移动端高度和滚动行为。
- 前端构建使用 `npm.cmd run build`，不要依赖 PowerShell 的 `npm.ps1`。

## 8. 当前限制与后续方向

当前知识库列表和部分用户端路径尚未完全落实“指定用户可访问知识库”的前端展示和拦截。后续需要：

1. 展示用户可访问的知识库范围。
2. 对无权限知识库提供明确提示，而不是仅显示通用错误。
3. 管理端增加成员批量授权、角色授权和权限变更审计展示。
4. 完善会话归档、搜索、批量删除和历史筛选。
5. 增加前端单元测试和关键交互测试。