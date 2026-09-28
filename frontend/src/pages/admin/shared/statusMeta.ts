/** 管理端共享状态元数据：状态码到展示标签与标签主题的唯一映射来源。 */

import { DOCUMENT_STATUS, TASK_STATUS } from '../../../constants'

/** TDesign 标签主题取值。 */
export type TagTheme = 'default' | 'primary' | 'warning' | 'danger' | 'success'

/** 单个状态的展示元数据。 */
export type StatusMeta = {
  label: string
  theme: TagTheme
}

// 口径规则：同一状态的标签与主题只在本文件定义，页面不得再自行拼接三元表达式；
// 后端新增状态时只修改本文件映射表，未知状态回退为原文 + default 主题。

/** 文档生命周期状态；published/archived 为历史数据兼容项。 */
export const DOCUMENT_STATUS_META: Record<string, StatusMeta> = {
  [DOCUMENT_STATUS.UPLOADED]: { label: '待处理', theme: 'warning' },
  [DOCUMENT_STATUS.PROCESSING]: { label: '处理中', theme: 'warning' },
  [DOCUMENT_STATUS.NEEDS_REVIEW]: { label: '待确认实体', theme: 'primary' },
  [DOCUMENT_STATUS.READY]: { label: '可检索', theme: 'success' },
  [DOCUMENT_STATUS.FAILED]: { label: '失败', theme: 'danger' },
  [DOCUMENT_STATUS.DELETED]: { label: '已删除', theme: 'default' },
  published: { label: '可检索', theme: 'success' },
  archived: { label: '已删除', theme: 'default' },
}

/** 文档导入任务状态（与后端 IMPORT_TASK_STATUSES 对齐）。 */
export const TASK_STATUS_META: Record<string, StatusMeta> = {
  [TASK_STATUS.QUEUED]: { label: '排队中', theme: 'warning' },
  [TASK_STATUS.PROCESSING]: { label: '处理中', theme: 'warning' },
  [TASK_STATUS.NEEDS_REVIEW]: { label: '待确认', theme: 'primary' },
  [TASK_STATUS.COMPLETED]: { label: '已完成', theme: 'success' },
  [TASK_STATUS.FAILED]: { label: '失败', theme: 'danger' },
  [TASK_STATUS.CANCELLED]: { label: '已取消', theme: 'default' },
}

/** 知识库状态；停用用 warning 提示不可新增文档。 */
export const LIBRARY_STATUS_META: Record<string, StatusMeta> = {
  active: { label: '已启用', theme: 'success' },
  disabled: { label: '已停用', theme: 'warning' },
}

/** 用户状态。 */
export const USER_STATUS_META: Record<string, StatusMeta> = {
  active: { label: '正常', theme: 'success' },
  disabled: { label: '已停用', theme: 'default' },
  locked: { label: '已锁定', theme: 'default' },
}

/** 角色状态；软删除场景配合 getLifecycleStatusMeta 使用。 */
export const ROLE_STATUS_META: Record<string, StatusMeta> = {
  active: { label: '已启用', theme: 'success' },
  disabled: { label: '已停用', theme: 'default' },
}

/** 权限状态。 */
export const PERMISSION_STATUS_META: Record<string, StatusMeta> = {
  active: { label: '已启用', theme: 'success' },
  disabled: { label: '已停用', theme: 'default' },
}

/** 软删除状态的通用元数据。 */
const DELETED_STATUS_META: StatusMeta = { label: '已删除', theme: 'danger' }

/** 查询状态元数据；未知状态回退为原状态文本与 default 主题。 */
export function getStatusMeta(map: Record<string, StatusMeta>, status: string): StatusMeta {
  return map[status] ?? { label: status, theme: 'default' }
}

/** 查询带软删除的状态元数据；deletedAt 非空时优先返回"已删除"。 */
export function getLifecycleStatusMeta(map: Record<string, StatusMeta>, status: string, deletedAt: string | null) {
  return deletedAt ? DELETED_STATUS_META : getStatusMeta(map, status)
}
