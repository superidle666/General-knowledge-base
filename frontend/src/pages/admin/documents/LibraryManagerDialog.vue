<!-- 知识库管理弹窗：分离新增、行内编辑、状态切换和软删除操作。 -->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { mysqlApi, type Library } from '../../../api/mysql'
import { PAGINATION } from '../../../constants'
import { useUserStore } from '../../../store/modules/user'

type EntityRecognitionMode = Library['entity_recognition_mode']
type LibraryVisibility = Library['visibility']

const allEntityModeOptions = [
  { label: '不启用实体识别', value: 'disabled' },
  { label: '可选实体识别', value: 'optional' },
  { label: '强制实体识别', value: 'required' },
] as const
const fileTypeOptions = [
  { label: 'PDF', value: 'pdf' },
  { label: 'Markdown', value: 'md' },
] as const
const visibilityOptions = [
  { label: '私有（仅授权成员）', value: 'private' },
  { label: '共享（可供全体用户检索）', value: 'shared' },
] as const
const entityModeLabels: Record<EntityRecognitionMode, string> = {
  disabled: '不启用实体识别',
  optional: '可选实体识别',
  required: '强制实体识别',
}

const props = defineProps<{ visible: boolean; libraries: Library[] }>()
const userStore = useUserStore()
const entityModeOptions = computed(() => userStore.userInfo?.roles.includes('admin')
  ? allEntityModeOptions
  : [allEntityModeOptions[0]])
const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'updated', library?: Library): void
  (event: 'error', message: string): void
}>()

const createName = ref('')
const createCode = ref('')
const createDescription = ref('')
const createEntityMode = ref<EntityRecognitionMode>('disabled')
const createAllowedFileTypes = ref<string[]>(['pdf', 'md'])
const createMaxFileSizeMb = ref<number>()
const createMaxDocumentCount = ref<number>()
const createMaxUploadFileCount = ref<number>()
const createChunkSize = ref<number>()
const createChunkOverlap = ref<number>()
const createVisibility = ref<LibraryVisibility>('private')
const editingId = ref<number>()
const editName = ref('')
const editDescription = ref('')
const editEntityMode = ref<EntityRecognitionMode>('disabled')
const editAllowedFileTypes = ref<string[]>(['pdf', 'md'])
const editMaxFileSizeMb = ref<number>()
const editMaxDocumentCount = ref<number>()
const editMaxUploadFileCount = ref<number>()
const editChunkSize = ref<number>()
const editChunkOverlap = ref<number>()
const editVisibility = ref<LibraryVisibility>('private')
const creating = ref(false)
const savingEdit = ref(false)
const changingStatusId = ref<number>()
const deletingId = ref<number>()
const errorMessage = ref('')
const libraryPage = ref(1)
const libraryPageSize = ref<number>(PAGINATION.PAGE_SIZE)
const visibleLibraries = computed(() => props.libraries.slice((libraryPage.value - 1) * libraryPageSize.value, libraryPage.value * libraryPageSize.value))

const isSystemAdmin = computed(() => userStore.userInfo?.roles.includes('admin') ?? false)

function handleLibraryPageChange(pageInfo: { current: number; pageSize: number }) {
  libraryPage.value = pageInfo.current
  libraryPageSize.value = pageInfo.pageSize
}

/** 只清空新增知识库表单，不影响正在编辑的知识库。 */
function resetCreateForm() {
  createName.value = ''
  createCode.value = ''
  createDescription.value = ''
  createEntityMode.value = 'disabled'
  createAllowedFileTypes.value = ['pdf', 'md']
  createMaxFileSizeMb.value = undefined
  createMaxDocumentCount.value = undefined
  createMaxUploadFileCount.value = undefined
  createChunkSize.value = undefined
  createChunkOverlap.value = undefined
  createVisibility.value = 'private'
}

/** 打开指定知识库下方的独立编辑表单。 */
function startEdit(library: Library) {
  if (library.deleted_at) return
  editingId.value = library.id
  editName.value = library.name
  editDescription.value = library.description || ''
  editEntityMode.value = library.entity_recognition_mode === 'required' && !isSystemAdmin.value
    ? 'disabled'
    : library.entity_recognition_mode || 'disabled'
  editAllowedFileTypes.value = library.allowed_file_types || ['pdf', 'md']
  editMaxFileSizeMb.value = library.max_file_size_mb || undefined
  editMaxDocumentCount.value = library.max_document_count || undefined
  editMaxUploadFileCount.value = library.max_upload_file_count || undefined
  editChunkSize.value = getChunkingValue(library, 'chunk_size')
  editChunkOverlap.value = getChunkingValue(library, 'chunk_overlap')
  editVisibility.value = library.visibility || 'private'
}

/** 关闭行内编辑并清除临时编辑值。 */
function cancelEdit() {
  editingId.value = undefined
  editName.value = ''
  editDescription.value = ''
}

function getChunkingValue(library: Library, key: 'chunk_size' | 'chunk_overlap') {
  const value = library.chunking_config?.[key]
  return typeof value === 'number' && value > 0 ? value : undefined
}

function buildRulePayload(
  allowedFileTypes: string[],
  maxFileSizeMb: number | undefined,
  maxDocumentCount: number | undefined,
  maxUploadFileCount: number | undefined,
  chunkSize: number | undefined,
  chunkOverlap: number | undefined,
  visibility: LibraryVisibility,
) {
  return {
    allowed_file_types: allowedFileTypes.length ? allowedFileTypes : null,
    max_file_size_mb: maxFileSizeMb || null,
    max_document_count: maxDocumentCount || null,
    max_upload_file_count: maxUploadFileCount || null,
    chunking_config: chunkSize
      ? { chunk_size: chunkSize, chunk_overlap: chunkOverlap || 0 }
      : null,
    visibility,
  }
}

function validateLibraryForm(
  name: string,
  code: string,
  chunkSize: number | undefined,
  chunkOverlap: number | undefined,
) {
  if (!name.trim()) return '请输入知识库名称。'
  if (code && !/^[a-z][a-z0-9-]*$/.test(code)) {
    return '知识库编码必须以小写字母开头，只能包含小写字母、数字和连字符。'
  }
  if (code && (code.length < 2 || code.length > 64)) return '知识库编码长度必须为 2 至 64 个字符。'
  if (chunkSize !== undefined && (!Number.isInteger(chunkSize) || chunkSize < 1)) return '切片长度必须为正整数。'
  if (chunkOverlap !== undefined && (!Number.isInteger(chunkOverlap) || chunkOverlap < 0)) return '切片重叠必须为非负整数。'
  if (chunkSize !== undefined && chunkOverlap !== undefined && chunkOverlap >= chunkSize) return '切片重叠必须小于切片长度。'
  return ''
}

function formatLibraryRules(library: Library) {
  const fileTypes = library.allowed_file_types?.map((item) => item.toUpperCase()).join('/') || '未限制格式'
  const size = library.max_file_size_mb ? `${library.max_file_size_mb} MB/文件` : '未限制大小'
  const batch = library.max_upload_file_count ? `${library.max_upload_file_count} 个/次` : '未限制批次'
  const visibility = library.visibility === 'shared' ? '共享' : '私有'
  return `${entityModeLabels[library.entity_recognition_mode]} · ${fileTypes} · ${size} · ${batch} · ${visibility}`
}

/** 根据名称生成符合后端约束的知识库编码。 */
function createLibraryCode(value: string) {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
  return normalized || `library-${Date.now()}`
}

/** 创建知识库，成功后仅重置新增表单。 */
async function createLibrary() {
  const name = createName.value.trim()
  const code = createCode.value.trim()
  const validationError = validateLibraryForm(name, code, createChunkSize.value, createChunkOverlap.value)
  if (validationError) {
    errorMessage.value = validationError
    return
  }

  errorMessage.value = ''
  creating.value = true
  try {
    const library = await mysqlApi.createLibrary({
      name,
      code: code || createLibraryCode(name),
      description: createDescription.value.trim() || null,
      entity_recognition_mode: createEntityMode.value,
      ...buildRulePayload(
        createAllowedFileTypes.value,
        createMaxFileSizeMb.value,
        createMaxDocumentCount.value,
        createMaxUploadFileCount.value,
        createChunkSize.value,
        createChunkOverlap.value,
        createVisibility.value,
      ),
    })
    resetCreateForm()
    emit('updated', library)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '知识库创建失败'
  } finally {
    creating.value = false
  }
}

/** 保存当前行的知识库名称和描述。 */
async function saveLibraryEdit() {
  if (!editingId.value) return
  const validationError = validateLibraryForm(editName.value, '', editChunkSize.value, editChunkOverlap.value)
  if (validationError) {
    errorMessage.value = validationError
    return
  }

  errorMessage.value = ''
  savingEdit.value = true
  try {
    await mysqlApi.updateLibrary(editingId.value, {
      name: editName.value.trim(),
      description: editDescription.value.trim() || null,
      entity_recognition_mode: editEntityMode.value,
      ...buildRulePayload(
        editAllowedFileTypes.value,
        editMaxFileSizeMb.value,
        editMaxDocumentCount.value,
        editMaxUploadFileCount.value,
        editChunkSize.value,
        editChunkOverlap.value,
        editVisibility.value,
      ),
    })
    cancelEdit()
    emit('updated')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '知识库修改失败'
  } finally {
    savingEdit.value = false
  }
}

/** 在启用和停用状态之间切换，不改变软删除状态。 */
async function toggleLibraryStatus(library: Library) {
  const status = library.status === 'active' ? 'disabled' : 'active'
  errorMessage.value = ''
  changingStatusId.value = library.id
  try {
    const updatedLibrary = await mysqlApi.updateLibrary(library.id, { status })
    emit('updated', updatedLibrary)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '知识库状态更新失败'
  } finally {
    changingStatusId.value = undefined
  }
}

/** 恢复旧版本中已软删除的知识库及其原有文档归属。 */
async function restoreLibrary(library: Library) {
  errorMessage.value = ''
  changingStatusId.value = library.id
  try {
    const restoredLibrary = await mysqlApi.restoreLibrary(library.id)
    emit('updated', restoredLibrary)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '知识库恢复失败'
  } finally {
    changingStatusId.value = undefined
  }
}

/** 仅删除没有有效文档的空知识库。 */
async function deleteLibrary(library: Library) {
  if (library.document_count > 0) return
  if (!window.confirm(`确定删除空知识库“${library.name}”吗？`)) return
  errorMessage.value = ''
  deletingId.value = library.id
  try {
    await mysqlApi.deleteLibrary(library.id)
    if (editingId.value === library.id) cancelEdit()
    emit('updated')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '知识库删除失败'
  } finally {
    deletingId.value = undefined
  }
}

function getStatusLabel(library: Library) {
  if (library.deleted_at) return '已删除'
  return library.status === 'active' ? '已启用' : '已停用'
}

function getStatusTheme(library: Library): 'danger' | 'success' | 'warning' {
  if (library.deleted_at) return 'danger'
  return library.status === 'active' ? 'success' : 'warning'
}

watch(
  () => props.visible,
  (visible) => {
    if (!visible) return
    resetCreateForm()
    cancelEdit()
    errorMessage.value = ''
  },
)

</script>

<template>
  <t-dialog
    :visible="visible"
    header="知识库管理"
    width="820px"
    :footer="false"
    @update:visible="emit('update:visible', $event)"
  >
    <p v-if="errorMessage" class="dialog-form-error">{{ errorMessage }}</p>
    <section class="library-create-section">
      <h3>新增知识库</h3>
      <div class="dialog-fields">
        <div class="library-basic-grid">
          <t-input v-model="createName" label="知识库名称" placeholder="例如：工业设备资料" />
          <t-input v-model="createCode" label="编码（可选）" placeholder="例如：industrial-equipment" />
          <t-select v-model="createEntityMode" :options="entityModeOptions" label="实体识别模式" />
        </div>
        <div class="library-rule-panel">
          <div class="library-rule-panel-heading"><strong>导入规则</strong><span>规则均为可选；留空表示不额外限制</span></div>
          <div class="library-rule-grid">
            <t-select v-model="createAllowedFileTypes" :options="fileTypeOptions" label="允许文件类型" multiple clearable />
            <t-select v-model="createVisibility" :options="visibilityOptions" label="可见范围" />
            <t-input-number class="library-rule-number" v-model="createMaxFileSizeMb" label="单文件大小上限（MB）" :min="1" :max="10240" clearable />
            <t-input-number class="library-rule-number" v-model="createMaxDocumentCount" label="知识库文档总上限" :min="1" clearable />
            <t-input-number class="library-rule-number" v-model="createMaxUploadFileCount" label="单次上传文件上限" :min="1" clearable />
            <t-input-number class="library-rule-number" v-model="createChunkSize" label="切片长度（字符）" :min="1" clearable />
            <t-input-number class="library-rule-number" v-model="createChunkOverlap" label="切片重叠（字符）" :min="0" clearable />
          </div>
        </div>
        <t-textarea v-model="createDescription" label="描述" placeholder="知识库用途说明（可选）" />
      </div>
      <div class="library-form-actions">
        <t-button variant="outline" @click="resetCreateForm">清空新增内容</t-button>
        <t-button theme="primary" :loading="creating" @click="createLibrary">
          新增知识库
        </t-button>
      </div>
    </section>

    <section class="library-list-section">
      <h3>现有知识库</h3>
      <div class="library-manager-list">
        <div
          v-for="library in visibleLibraries"
          :key="library.id"
          class="library-manager-entry"
        >
          <div class="library-manager-item">
            <div>
              <strong>{{ library.name }}</strong>
               <span>{{ library.code }} · {{ library.document_count }} 篇文档</span>
               <span class="library-rule-summary">{{ formatLibraryRules(library) }}</span>
              <p v-if="library.description" class="library-description">
                说明：{{ library.description }}
              </p>
            </div>
            <div class="library-manager-actions">
              <t-tag :theme="getStatusTheme(library)" variant="light">
                {{ getStatusLabel(library) }}
              </t-tag>
              <template v-if="library.deleted_at">
                <t-button
                  variant="text"
                  class="edit-action"
                  size="small"
                  :loading="changingStatusId === library.id"
                  @click="restoreLibrary(library)"
                >恢复</t-button>
              </template>
              <template v-else>
                <t-button
                  variant="text"
                  class="edit-action"
                  size="small"
                  @click="startEdit(library)"
                >编辑</t-button>
                <t-button
                  variant="text"
                  class="edit-action"
                  size="small"
                  :loading="changingStatusId === library.id"
                  @click="toggleLibraryStatus(library)"
                >{{ library.status === 'active' ? '停用' : '启用' }}</t-button>
                <t-button
                  variant="text"
                  theme="danger"
                  size="small"
                  :disabled="library.document_count > 0"
                  :loading="deletingId === library.id"
                  :title="library.document_count > 0 ? '请先迁移或删除知识库中的文档' : '删除空知识库'"
                  @click="deleteLibrary(library)"
                >删除</t-button>
              </template>
            </div>
          </div>

          <div v-if="editingId === library.id" class="library-inline-edit">
            <h4>编辑“{{ library.name }}”</h4>
            <div class="library-basic-grid">
              <t-input v-model="editName" label="知识库名称" />
              <t-select v-model="editEntityMode" :options="entityModeOptions" label="实体识别模式" />
              <t-select v-model="editVisibility" :options="visibilityOptions" label="可见范围" />
            </div>
            <div class="library-rule-panel">
              <div class="library-rule-panel-heading"><strong>导入规则</strong><span>规则均为可选；留空表示不额外限制</span></div>
              <div class="library-rule-grid">
                <t-select v-model="editAllowedFileTypes" :options="fileTypeOptions" label="允许文件类型" multiple clearable />
                <t-input-number class="library-rule-number" v-model="editMaxFileSizeMb" label="单文件大小上限（MB）" :min="1" :max="10240" clearable />
                <t-input-number class="library-rule-number" v-model="editMaxDocumentCount" label="知识库文档总上限" :min="1" clearable />
                <t-input-number class="library-rule-number" v-model="editMaxUploadFileCount" label="单次上传文件上限" :min="1" clearable />
                <t-input-number class="library-rule-number" v-model="editChunkSize" label="切片长度（字符）" :min="1" clearable />
                <t-input-number class="library-rule-number" v-model="editChunkOverlap" label="切片重叠（字符）" :min="0" clearable />
              </div>
            </div>
            <t-textarea v-model="editDescription" label="描述" />
            <div class="library-form-actions">
              <t-button variant="outline" @click="cancelEdit">取消</t-button>
              <t-button
                theme="primary"
                :loading="savingEdit"
                @click="saveLibraryEdit"
              >保存修改</t-button>
            </div>
          </div>
        </div>
        <p v-if="!libraries.length" class="empty-state">暂无知识库</p>
      </div>
      <div v-if="libraries.length" class="dialog-pagination">
        <t-pagination
          :current="libraryPage"
          :page-size="libraryPageSize"
          :total="libraries.length"
          :page-size-options="[...PAGINATION.PAGE_SIZES]"
          @change="handleLibraryPageChange"
        />
      </div>
    </section>
  </t-dialog>
</template>

<style scoped>
.library-create-section, .library-list-section { display: grid; min-width: 0; gap: 12px; }
.library-create-section { padding-bottom: 20px; border-bottom: 1px solid #e5e7eb; }
.library-list-section { padding-top: 18px; }
.library-create-section h3, .library-list-section h3 { margin: 0; color: #1f2937; font-size: 14px; font-weight: 600; }
.library-basic-grid, .library-rule-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.library-rule-panel { display: grid; gap: 12px; padding: 14px; border: 1px solid #dbeafe; border-radius: 6px; background: #f8fbff; }
.library-rule-panel-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.library-rule-panel-heading strong { color: #334155; font-size: 13px; }
.library-rule-panel-heading span { color: #64748b; font-size: 11px; }
.library-manager-list { display: grid; gap: 8px; max-height: min(48vh, 420px); overflow-y: auto; padding: 2px; }
.library-manager-entry { box-sizing: border-box; min-width: 0; border: 1px solid #e5e7eb; border-radius: 6px; background: #fff; }
.library-manager-item { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; min-height: 40px; padding: 10px 12px; }
.library-inline-edit { display: grid; gap: 12px; min-width: 0; padding: 14px 12px; border-top: 1px solid #e5e7eb; background: #f8fafc; }
.library-inline-edit h4 { margin: 0; color: #374151; font-size: 13px; font-weight: 600; }
.library-form-actions, .library-manager-actions { display: flex; min-width: 0; align-items: center; justify-content: flex-end; gap: 6px; flex-wrap: wrap; }
.library-manager-item > div:first-child { display: grid; flex: 1 1 auto; min-width: 0; gap: 3px; }
.library-manager-item strong { font-size: 13px; }
.library-manager-item span { overflow: hidden; color: #9ca3af; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.library-manager-item .library-rule-summary { color: #64748b; }
.library-rule-number { width: 100%; }
.library-rule-number :deep(.t-input-number) { width: 100%; padding: 0 28px; }
.library-rule-number :deep(.t-input-number__decrease), .library-rule-number :deep(.t-input-number__increase) {
  width: 28px;
  height: 32px;
  padding: 0;
  margin: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0;
  flex: 0 0 28px;
  line-height: 1;
}
.library-rule-number :deep(.t-input-number__decrease) { left: 0; }
.library-rule-number :deep(.t-input-number__increase) { right: 0; }
.library-rule-number :deep(.t-input-number__decrease .t-button__icon), .library-rule-number :deep(.t-input-number__increase .t-button__icon) { margin: 0; }
.library-rule-number :deep(.t-input) { width: 100%; min-width: 0; }
.library-rule-number :deep(.t-input__inner) { text-align: center; }
.library-description { display: -webkit-box; margin: 2px 0 0; overflow: hidden; color: #6b7280; font-size: 12px; line-height: 1.5; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.empty-state { color: #9ca3af; font-size: 12px; }
.dialog-pagination { display: flex; align-items: center; justify-content: flex-end; gap: 12px; margin-top: 12px; padding-top: 12px; border-top: 1px solid #e5e7eb; color: #6b7280; font-size: 12px; }
@media (max-width: 680px) { .library-basic-grid, .library-rule-grid { grid-template-columns: 1fr; } .library-manager-item { flex-direction: column; } .library-manager-actions { width: 100%; justify-content: flex-start; } }
</style>
