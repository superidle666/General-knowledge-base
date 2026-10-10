/**
 * 知识库表单共享逻辑：创建弹窗与详情页配置 Tab 共用的字段模型与纯函数。
 *
 * 口径规则：字段默认值、校验文案、payload 组装只在本文件定义，
 * 两处表单不得再各自维护一份（防止创建/编辑行为漂移）。
 */

import type { Library } from '../../../api/mysql'

/** 实体识别模式取值（与后端 entity_recognition_mode 对齐）。 */
export type EntityRecognitionMode = Library['entity_recognition_mode']
/** 实体确认闸严格程度取值（与后端 entity_confirm_mode 对齐）。 */
export type EntityConfirmMode = Library['entity_confirm_mode']
/** 知识库可见性取值。 */
export type LibraryVisibility = Library['visibility']

/** 知识库表单的界面态模型（数字字段用 undefined 表示"未设置"，提交时转 null）。 */
export type LibraryForm = {
  name: string
  code: string
  description: string
  entityMode: EntityRecognitionMode
  entityConfirmMode: EntityConfirmMode
  allowedFileTypes: string[]
  maxFileSizeMb: number | undefined
  maxDocumentCount: number | undefined
  maxUploadFileCount: number | undefined
  chunkSize: number | undefined
  chunkOverlap: number | undefined
  useHyde: boolean
  visibility: LibraryVisibility
}

/** 实体识别模式下拉选项（普通用户仅见 disabled，与后端权限口径一致）。 */
export const ALL_ENTITY_MODE_OPTIONS: Array<{ label: string; value: EntityRecognitionMode }> = [
  { label: '不启用实体识别', value: 'disabled' },
  { label: '强制实体识别', value: 'required' },
]

/**
 * 实体确认严格程度下拉选项（只对“强制实体识别”的库起作用）。
 * 文案写成用户能看懂的后果，而不是 strict/lenient 这两个字：选错一档的表现是
 * “本该问一句却直接答了”，光看标签名看不出来。
 */
export const ALL_ENTITY_CONFIRM_OPTIONS: Array<{ label: string; value: EntityConfirmMode }> = [
  { label: '严格：确认不了型号就先问一句', value: 'strict' },
  { label: '宽松：只按型号筛选，不反问', value: 'lenient' },
]

/** 允许文件类型下拉选项。 */
export const FILE_TYPE_OPTIONS = [
  { label: 'PDF', value: 'pdf' },
  { label: 'Markdown', value: 'md' },
]

/** 可见范围下拉选项。 */
export const VISIBILITY_OPTIONS: Array<{ label: string; value: LibraryVisibility }> = [
  { label: '私有（仅授权成员）', value: 'private' },
  { label: '共享（可供全体用户检索）', value: 'shared' },
]

/** 返回空白表单（新增知识库初始值）。 */
export function emptyLibraryForm(): LibraryForm {
  return {
    name: '',
    code: '',
    description: '',
    entityMode: 'disabled',
    // 新建库默认最严那一档：往宽松改等于给误答开口子，得管理员显式做这个决定
    entityConfirmMode: 'strict',
    allowedFileTypes: ['pdf', 'md'],
    maxFileSizeMb: undefined,
    maxDocumentCount: undefined,
    maxUploadFileCount: undefined,
    chunkSize: undefined,
    chunkOverlap: undefined,
    useHyde: false,
    visibility: 'private',
  }
}

/** 由知识库接口数据回填表单（编辑态初始值）。 */
export function libraryToForm(library: Library): LibraryForm {
  const chunkSize = library.chunking_config?.chunk_size
  const chunkOverlap = library.chunking_config?.chunk_overlap
  return {
    name: library.name,
    code: library.code,
    description: library.description || '',
    entityMode: library.entity_recognition_mode,
    entityConfirmMode: library.entity_confirm_mode || 'strict',
    allowedFileTypes: [...(library.allowed_file_types || ['pdf', 'md'])],
    maxFileSizeMb: library.max_file_size_mb || undefined,
    maxDocumentCount: library.max_document_count || undefined,
    maxUploadFileCount: library.max_upload_file_count || undefined,
    chunkSize: typeof chunkSize === 'number' ? chunkSize : undefined,
    chunkOverlap: typeof chunkOverlap === 'number' ? chunkOverlap : undefined,
    useHyde: Boolean(library.use_hyde),
    visibility: library.visibility,
  }
}

/** 就地规整单文件大小上限为整数 MB（浮点输入向上取整）。 */
export function normalizeMaxFileSize(form: LibraryForm): void {
  if (form.maxFileSizeMb === undefined) return
  form.maxFileSizeMb = Math.ceil(form.maxFileSizeMb)
}

/** 文件类型兜底：清空时恢复默认两类并返回提示文案，由调用方决定错误展示位置。 */
export function ensureAllowedFileType(form: LibraryForm): string {
  if (form.allowedFileTypes.length) return ''
  form.allowedFileTypes = ['pdf', 'md']
  return '至少选择一个文件类型。'
}

/** 校验表单；返回空字符串表示通过。requireCode 区分新建（需编码）与编辑（编码不可改）。 */
export function validateLibraryForm(form: LibraryForm, requireCode = true): string {
  if (!form.name.trim()) return '请输入知识库名称。'
  if (requireCode && !/^[a-z][a-z0-9-]{1,63}$/.test(form.code.trim()))
    return '知识库编码必须以小写字母开头，只能包含小写字母、数字和连字符，长度为 2 至 64 个字符。'
  if (form.chunkSize !== undefined && (!Number.isInteger(form.chunkSize) || form.chunkSize < 1))
    return '切片长度必须为正整数。'
  if (form.chunkOverlap !== undefined && (!Number.isInteger(form.chunkOverlap) || form.chunkOverlap < 0))
    return '切片重叠必须为非负整数。'
  if (form.chunkSize !== undefined && form.chunkOverlap !== undefined && form.chunkOverlap >= form.chunkSize)
    return '切片重叠必须小于切片长度。'
  return ''
}

/** 表单界面态 → 接口 payload；非管理员不提交实体识别模式（后端同样校验，前端先行收敛）。 */
export function libraryFormToPayload(
  form: LibraryForm,
  opts: { isAdmin: boolean; code?: string },
): Record<string, unknown> {
  return {
    name: form.name.trim(),
    code: opts.code ?? form.code.trim(),
    description: form.description.trim() || null,
    ...(opts.isAdmin ? { entity_recognition_mode: form.entityMode } : {}),
    // 确认闸沿用同一个权限口径（后端也只允许管理员往宽松改）；不提交时后端按默认 strict 落
    ...(opts.isAdmin ? { entity_confirm_mode: form.entityConfirmMode } : {}),
    allowed_file_types: form.allowedFileTypes.length ? form.allowedFileTypes : null,
    max_file_size_mb: form.maxFileSizeMb || null,
    max_document_count: form.maxDocumentCount || null,
    max_upload_file_count: form.maxUploadFileCount || null,
    chunking_config: form.chunkSize ? { chunk_size: form.chunkSize, chunk_overlap: form.chunkOverlap || 0 } : null,
    use_hyde: form.useHyde,
    visibility: form.visibility,
  }
}
