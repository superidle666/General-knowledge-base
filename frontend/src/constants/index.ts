/** 用户角色编码，用于权限判断和角色展示。 */
export const USER_ROLE = {
  ADMIN: 'admin',
  EDITOR: 'editor',
  VIEWER: 'viewer',
} as const

/** 常用 HTTP 状态码，用于请求错误分支判断。 */
export const HTTP_STATUS = {
  UNAUTHORIZED: 401,
  FORBIDDEN: 403,
  NOT_FOUND: 404,
  SERVER_ERROR: 500,
} as const

/** 管理端列表的默认分页配置。 */
export const PAGINATION = {
  PAGE_SIZE: 10,
  PAGE_SIZES: [10, 20, 50, 100],
} as const

/** 文档生命周期状态编码。 */
export const DOCUMENT_STATUS = {
  UPLOADED: 'uploaded',
  PROCESSING: 'processing',
  NEEDS_REVIEW: 'needs_review',
  READY: 'ready',
  FAILED: 'failed',
  DELETED: 'deleted',
} as const

/** 文档导入任务状态编码（与后端 IMPORT_TASK_STATUSES 对齐）。 */
export const TASK_STATUS = {
  QUEUED: 'queued',
  PROCESSING: 'processing',
  NEEDS_REVIEW: 'needs_review',
  COMPLETED: 'completed',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
} as const

/** 常用表单校验正则表达式。 */
export const REGEX = {
  EMAIL: /^[^\s@]+@[^\s@]+\.[^\s@]+$/,
  PHONE: /^1[3-9]\d{9}$/,
  PASSWORD: /^(?=.*[A-Za-z])(?=.*\d).{8,}$/,
} as const

/** 浏览器本地存储键名，新增键必须先在此登记，禁止页面内裸字符串。 */
export const STORAGE_KEY = {
  TOKEN: 'kb-admin-access-token',
  REFRESH_TOKEN: 'kb-admin-refresh-token',
  USER_INFO: 'kb-admin-user',
  REMEMBERED_CREDENTIALS: 'kb-admin-remembered-credentials',
  ADMIN_DARK_MODE: 'kb-admin-dark-mode',
  USER_DARK_MODE: 'kb-user-dark-mode',
  USER_SIDEBAR_COLLAPSED: 'kb-user-sidebar-collapsed',
  USER_ANONYMOUS_QUERY_COUNT: 'kb-user-anonymous-query-count',
} as const

/** 应用级展示配置。 */
export const APP_CONFIG = {
  TITLE: '设备技术文档智能知识库平台',
  VERSION: '0.1.0',
} as const

export type UserRole = (typeof USER_ROLE)[keyof typeof USER_ROLE]
export type HttpStatus = (typeof HTTP_STATUS)[keyof typeof HTTP_STATUS]
export type DocumentStatus = (typeof DOCUMENT_STATUS)[keyof typeof DOCUMENT_STATUS]
export type TaskStatus = (typeof TASK_STATUS)[keyof typeof TASK_STATUS]
