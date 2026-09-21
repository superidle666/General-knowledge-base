<!-- 独立知识库管理页：左侧选择知识库，右侧展示当前知识库规则。 -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { AddIcon, FileIcon, SearchIcon } from 'tdesign-icons-vue-next'
import { MessagePlugin } from 'tdesign-vue-next'
import { mysqlApi, type Library } from '../../../api/mysql'
import { useUserStore } from '../../../store/modules/user'

const props = defineProps<{ selectedLibraryId?: number }>()
const userStore = useUserStore()
const emit = defineEmits<{
  (event: 'update-counts', counts: { documents: number }): void
  (event: 'view-documents', libraryId: number): void
  (event: 'import-documents', libraryId: number): void
}>()

type EntityRecognitionMode = Library['entity_recognition_mode']
type LibraryVisibility = Library['visibility']

const allEntityModeOptions = [
  { label: '不启用实体识别', value: 'disabled' },
  { label: '可选实体识别', value: 'optional' },
  { label: '强制实体识别', value: 'required' },
]
const isSystemAdmin = computed(() => userStore.userInfo?.roles.includes('admin') ?? false)
const entityModeOptions = computed(() => isSystemAdmin.value ? allEntityModeOptions : [allEntityModeOptions[0]])
const fileTypeOptions = [
  { label: 'PDF', value: 'pdf' },
  { label: 'Markdown', value: 'md' },
]
const visibilityOptions = [
  { label: '私有（仅授权成员）', value: 'private' },
  { label: '共享（可供全体用户检索）', value: 'shared' },
]

const libraries = ref<Library[]>([])
const search = ref('')
const selectedLibraryId = ref<number>()
const loading = ref(false)
const saving = ref(false)
const creating = ref(false)
const deleting = ref(false)
const changingStatus = ref(false)
const errorMessage = ref('')
const createDialogError = ref('')
const showCreateDialog = ref(false)

const createForm = ref({
  name: '', code: '', description: '',
  entityMode: 'disabled' as EntityRecognitionMode, allowedFileTypes: ['pdf', 'md'],
  maxFileSizeMb: undefined as number | undefined, maxDocumentCount: undefined as number | undefined,
  maxUploadFileCount: undefined as number | undefined, chunkSize: undefined as number | undefined,
  chunkOverlap: undefined as number | undefined, visibility: 'private' as LibraryVisibility,
})
const editForm = ref(emptyForm())

const visibleLibraries = computed(() => libraries.value.filter((library) => !library.deleted_at))
const filteredLibraries = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return visibleLibraries.value
  return visibleLibraries.value.filter((library) => `${library.name} ${library.code} ${library.description || ''}`.toLowerCase().includes(query))
})
const selectedLibrary = computed(() => visibleLibraries.value.find((library) => library.id === selectedLibraryId.value))
const activeLibraries = computed(() => visibleLibraries.value.filter((library) => library.status === 'active'))
const totalDocuments = computed(() => activeLibraries.value.reduce((total, library) => total + library.document_count, 0))

function emptyForm() {
  return { name: '', code: '', description: '', entityMode: 'disabled' as EntityRecognitionMode, allowedFileTypes: ['pdf', 'md'], maxFileSizeMb: undefined as number | undefined, maxDocumentCount: undefined as number | undefined, maxUploadFileCount: undefined as number | undefined, chunkSize: undefined as number | undefined, chunkOverlap: undefined as number | undefined, visibility: 'private' as LibraryVisibility }
}

function selectLibrary(library: Library) {
  selectedLibraryId.value = library.id
  editForm.value = toForm(library)
  errorMessage.value = ''
}

function openDocumentUpload() {
  if (!selectedLibrary.value || selectedLibrary.value.status !== 'active') return
  emit('import-documents', selectedLibrary.value.id)
}

function viewDocuments() {
  if (!selectedLibrary.value) return
  emit('view-documents', selectedLibrary.value.id)
}

function ensureAllowedFileType(form: typeof createForm.value, inCreateDialog = false) {
  if (form.allowedFileTypes.length) {
    if (inCreateDialog && createDialogError.value === '至少选择一个文件类型。') createDialogError.value = ''
    if (!inCreateDialog && errorMessage.value === '至少选择一个文件类型。') errorMessage.value = ''
    return
  }
  form.allowedFileTypes = ['pdf', 'md']
  if (inCreateDialog) createDialogError.value = '至少选择一个文件类型。'
  else errorMessage.value = '至少选择一个文件类型。'
}

function normalizeMaxFileSize(form: typeof createForm.value) {
  if (form.maxFileSizeMb === undefined) return
  form.maxFileSizeMb = Math.ceil(form.maxFileSizeMb)
}

function toForm(library: Library) {
  const chunkSize = library.chunking_config?.chunk_size
  const chunkOverlap = library.chunking_config?.chunk_overlap
  return {
    name: library.name, code: library.code, description: library.description || '',
    entityMode: library.entity_recognition_mode,
    allowedFileTypes: [...(library.allowed_file_types || ['pdf', 'md'])],
    maxFileSizeMb: library.max_file_size_mb || undefined,
    maxDocumentCount: library.max_document_count || undefined,
    maxUploadFileCount: library.max_upload_file_count || undefined,
    chunkSize: typeof chunkSize === 'number' ? chunkSize : undefined,
    chunkOverlap: typeof chunkOverlap === 'number' ? chunkOverlap : undefined,
    visibility: library.visibility,
  }
}

function validateForm(form: typeof createForm.value, requireCode = true) {
  if (!form.name.trim()) return '请输入知识库名称。'
  if (requireCode && !/^[a-z][a-z0-9-]{1,63}$/.test(form.code.trim())) return '知识库编码必须以小写字母开头，只能包含小写字母、数字和连字符，长度为 2 至 64 个字符。'
  if (form.chunkSize !== undefined && (!Number.isInteger(form.chunkSize) || form.chunkSize < 1)) return '切片长度必须为正整数。'
  if (form.chunkOverlap !== undefined && (!Number.isInteger(form.chunkOverlap) || form.chunkOverlap < 0)) return '切片重叠必须为非负整数。'
  if (form.chunkSize !== undefined && form.chunkOverlap !== undefined && form.chunkOverlap >= form.chunkSize) return '切片重叠必须小于切片长度。'
  return ''
}

function toPayload(form: typeof createForm.value) {
  return {
    name: form.name.trim(), code: form.code.trim(), description: form.description.trim() || null,
    ...(isSystemAdmin.value ? { entity_recognition_mode: form.entityMode } : {}),
    allowed_file_types: form.allowedFileTypes.length ? form.allowedFileTypes : null,
    max_file_size_mb: form.maxFileSizeMb || null, max_document_count: form.maxDocumentCount || null,
    max_upload_file_count: form.maxUploadFileCount || null,
    chunking_config: form.chunkSize ? { chunk_size: form.chunkSize, chunk_overlap: form.chunkOverlap || 0 } : null,
    visibility: form.visibility,
  }
}

async function loadLibraries(preferredId?: number) {
  loading.value = true
  errorMessage.value = ''
  try {
    libraries.value = await mysqlApi.listLibraries(true, false)
    emit('update-counts', { documents: totalDocuments.value })
    const nextId = preferredId || props.selectedLibraryId || selectedLibraryId.value
    const nextLibrary = visibleLibraries.value.find((library) => library.id === nextId) || visibleLibraries.value[0]
    if (nextLibrary) selectLibrary(nextLibrary)
    else selectedLibraryId.value = undefined
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '知识库加载失败'
  } finally {
    loading.value = false
  }
}

async function createLibrary() {
  const form = createForm.value
  normalizeMaxFileSize(form)
  const validationError = validateForm(form)
  if (validationError) { createDialogError.value = validationError; return }
  creating.value = true
  createDialogError.value = ''
  try {
    const library = await mysqlApi.createLibrary(toPayload(form))
    createForm.value = emptyForm()
    showCreateDialog.value = false
    await loadLibraries(library.id)
    await MessagePlugin.success('知识库新增成功。')
  } catch (error) { createDialogError.value = error instanceof Error ? error.message : '知识库创建失败' } finally { creating.value = false }
}

function openCreateDialog() {
  createForm.value = emptyForm()
  createDialogError.value = ''
  showCreateDialog.value = true
}

async function saveLibrary() {
  if (!selectedLibrary.value) return
  normalizeMaxFileSize(editForm.value)
  const validationError = validateForm(editForm.value, false)
  if (validationError) { errorMessage.value = validationError; return }
  saving.value = true
  try {
    await mysqlApi.updateLibrary(selectedLibrary.value.id, toPayload({ ...editForm.value, code: selectedLibrary.value.code }))
    await loadLibraries(selectedLibrary.value.id)
    await MessagePlugin.success('知识库已保存。')
  } catch (error) { errorMessage.value = error instanceof Error ? error.message : '知识库保存失败' } finally { saving.value = false }
}

async function deleteSelectedLibrary() {
  if (!selectedLibrary.value || selectedLibrary.value.document_count > 0) return
  if (!window.confirm(`确定删除知识库“${selectedLibrary.value.name}”吗？删除后将不再在管理端列表展示。`)) return
  deleting.value = true
  try {
    await mysqlApi.deleteLibrary(selectedLibrary.value.id)
    await loadLibraries()
    await MessagePlugin.success('知识库删除成功。')
  } catch (error) { errorMessage.value = error instanceof Error ? error.message : '知识库删除失败' } finally { deleting.value = false }
}

async function toggleLibraryStatus() {
  if (!selectedLibrary.value || changingStatus.value) return
  changingStatus.value = true
  errorMessage.value = ''
  try {
    await mysqlApi.updateLibrary(selectedLibrary.value.id, {
      status: selectedLibrary.value.status === 'active' ? 'disabled' : 'active',
    })
    const nextStatus = selectedLibrary.value.status === 'active' ? '停用' : '启用'
    await loadLibraries(selectedLibrary.value.id)
    await MessagePlugin.success(`知识库已${nextStatus}。`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '知识库状态更新失败'
  } finally {
    changingStatus.value = false
  }
}

watch(() => props.selectedLibraryId, (id) => { if (id) void loadLibraries(id) })
onMounted(() => void loadLibraries())
</script>

<template>
  <section class="admin-module library-page">
    <div class="admin-page-head compact">
      <div><div class="eyebrow">KNOWLEDGE LIBRARIES</div><h1>知识库管理</h1><p>选择知识库后，在右侧查看和维护当前知识库规则。</p></div>
      <t-button theme="primary" class="create-action" @click="openCreateDialog"><AddIcon />新增知识库</t-button>
    </div>
    <div v-if="errorMessage" class="admin-error">{{ errorMessage }}<button @click="() => loadLibraries()">重试</button></div>
    <div v-if="loading" class="admin-loading">正在加载知识库数据...</div>
    <div class="library-overview-heading"><strong>知识库概述</strong><span>查看当前知识库数量、启用状态和文档规模</span></div>
    <div class="library-overview-grid">
      <div class="library-overview-card"><span>知识库总数</span><strong>{{ visibleLibraries.length }}</strong><small>已删除知识库不展示</small></div>
      <div class="library-overview-card"><span>启用中的知识库</span><strong>{{ activeLibraries.length }}</strong><small>当前允许新增文档</small></div>
      <div class="library-overview-card"><span>文档总数</span><strong>{{ totalDocuments }}</strong><small>启用知识库中的有效文档</small></div>
    </div>
    <t-dialog v-model:visible="showCreateDialog" header="新增知识库" width="760px" :footer="false">
      <div class="library-create-panel">
      <p v-if="createDialogError" class="dialog-form-error">{{ createDialogError }}</p>
      <div class="library-section-heading"><strong>知识库概述</strong><span>填写名称、编码和用途，便于后续识别和维护。</span></div>
      <div class="library-form-grid">
        <div class="library-field"><t-input v-model="createForm.name" label="知识库名称" placeholder="例如：工业设备资料" /></div>
        <div class="library-field"><t-input v-model="createForm.code" label="知识库编码" placeholder="例如：industrial-equipment" /></div>
        <div class="library-field"><t-select v-model="createForm.entityMode" :options="entityModeOptions" label="实体识别模式" /></div>
        <div class="library-field"><t-select v-model="createForm.allowedFileTypes" :options="fileTypeOptions" label="允许文件类型" multiple clearable @change="ensureAllowedFileType(createForm, true)" /></div>
        <div class="library-field"><t-select v-model="createForm.visibility" :options="visibilityOptions" label="可见范围" /></div>
        <div class="library-field"><t-input-number class="library-rule-number" v-model="createForm.maxFileSizeMb" label="单文件大小上限（MB）" :min="1" :max="10240" clearable @blur="normalizeMaxFileSize(createForm)" /></div>
        <div class="library-field"><t-input-number class="library-rule-number" v-model="createForm.maxDocumentCount" label="知识库文档总上限" :min="1" clearable /></div>
        <div class="library-field"><t-input-number class="library-rule-number" v-model="createForm.maxUploadFileCount" label="单次上传文件上限" :min="1" clearable /></div>
        <div class="library-field"><t-input-number class="library-rule-number" v-model="createForm.chunkSize" label="切片长度（字符）" :min="1" clearable /></div>
        <div class="library-field"><t-input-number class="library-rule-number" v-model="createForm.chunkOverlap" label="切片重叠（字符）" :min="0" clearable /></div>
        <div class="library-field"><t-textarea v-model="createForm.description" label="描述" placeholder="请输入知识库用途、资料范围和使用场景（可选）" /></div>
      </div>
      <div class="library-actions"><t-button variant="outline" @click="showCreateDialog = false">取消</t-button><t-button theme="primary" :loading="creating" @click="createLibrary">创建知识库</t-button></div>
    </div>
    </t-dialog>
    <div class="library-workspace">
      <aside class="panel library-list-panel">
        <div class="library-list-heading"><strong>知识库列表</strong><span>{{ filteredLibraries.length }} 个</span></div>
        <t-input v-model="search" placeholder="搜索名称或编码" clearable><template #prefix-icon><SearchIcon /></template></t-input>
        <div class="library-list">
          <button v-for="library in filteredLibraries" :key="library.id" class="library-list-item" :class="{ active: selectedLibraryId === library.id }" @click="selectLibrary(library)">
            <span class="library-list-icon"><FileIcon /></span><span class="library-list-copy"><strong>{{ library.name }}</strong><small>{{ library.code }} · {{ library.document_count }} 篇文档</small></span><t-tag :theme="library.status === 'active' ? 'success' : 'warning'" variant="light" size="small">{{ library.status === 'active' ? '启用' : '停用' }}</t-tag>
          </button>
          <div v-if="!filteredLibraries.length" class="library-list-empty">暂无知识库</div>
        </div>
      </aside>
      <main v-if="selectedLibrary" class="panel library-detail-panel">
        <div class="library-detail-heading"><div><div class="eyebrow">SELECTED LIBRARY</div><h2>{{ selectedLibrary.name }}</h2><p>{{ selectedLibrary.code }} · {{ selectedLibrary.document_count }} 篇文档</p></div><div class="library-detail-actions"><t-button variant="outline" @click="viewDocuments">查看文档</t-button><t-button theme="primary" :disabled="selectedLibrary.status !== 'active'" @click="openDocumentUpload">导入文档</t-button></div></div>
        <div class="library-section-heading"><strong>当前知识库规则</strong><span>以下内容仅针对“{{ selectedLibrary.name }}”；规则均为可选。</span></div>
        <div class="library-form-grid">
          <div class="library-field"><t-input v-model="editForm.name" label="知识库名称" /></div>
          <t-input :value="selectedLibrary.code" label="知识库编码" disabled />
          <div class="library-field"><t-select v-model="editForm.entityMode" :options="entityModeOptions" :disabled="!isSystemAdmin" label="实体识别模式" /><small v-if="!isSystemAdmin && selectedLibrary.entity_recognition_mode !== 'disabled'" class="library-rule-warning">当前知识库启用了实体识别，暂不支持普通用户导入，请联系管理员修改。</small></div>
          <div class="library-field"><t-select v-model="editForm.allowedFileTypes" :options="fileTypeOptions" label="允许文件类型" multiple clearable @change="ensureAllowedFileType(editForm)" /></div>
          <div class="library-field"><t-select v-model="editForm.visibility" :options="visibilityOptions" label="可见范围" /></div>
          <div class="library-field"><t-input-number class="library-rule-number" v-model="editForm.maxFileSizeMb" label="单文件大小上限（MB）" :min="1" :max="10240" clearable @blur="normalizeMaxFileSize(editForm)" /></div>
          <div class="library-field"><t-input-number class="library-rule-number" v-model="editForm.maxDocumentCount" label="知识库文档总上限" :min="1" clearable /></div>
          <div class="library-field"><t-input-number class="library-rule-number" v-model="editForm.maxUploadFileCount" label="单次上传文件上限" :min="1" clearable /></div>
          <div class="library-field"><t-input-number class="library-rule-number" v-model="editForm.chunkSize" label="切片长度（字符）" :min="1" clearable /></div>
          <div class="library-field"><t-input-number class="library-rule-number" v-model="editForm.chunkOverlap" label="切片重叠（字符）" :min="0" clearable /></div>
          <div class="library-field"><t-textarea v-model="editForm.description" label="描述" placeholder="请输入知识库用途、资料范围和使用场景（可选）" /></div>
        </div>
        <div class="library-actions"><t-button theme="primary" :loading="saving" @click="saveLibrary">保存当前规则</t-button><t-button variant="outline" :loading="changingStatus" @click="toggleLibraryStatus">{{ selectedLibrary.status === 'active' ? '停用知识库' : '启用知识库' }}</t-button><t-tooltip content="请先删除该知识库文档后再删除知识库" :disabled="selectedLibrary.document_count === 0"><span><t-button theme="danger" variant="outline" :disabled="selectedLibrary.document_count > 0" :loading="deleting" @click="deleteSelectedLibrary">删除知识库</t-button></span></t-tooltip></div>
      </main>
      <main v-else class="panel library-empty"><FileIcon /><strong>请选择一个知识库</strong><span>从左侧列表选择后查看当前规则。</span></main>
    </div>
  </section>
</template>

<style scoped>
.library-page { min-width: 0; }
.library-overview-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-bottom: 20px; }
.library-overview-heading { display: flex; align-items: baseline; gap: 12px; margin-bottom: 10px; }
.library-overview-heading strong { color: #1f2937; font-size: 15px; }
.library-overview-heading span { color: #94a3b8; font-size: 11px; }
.library-overview-card, .library-create-panel, .library-list-panel, .library-detail-panel { border: 1px solid #e5e7eb; border-radius: 7px; background: #fff; }
.library-overview-card { display: grid; gap: 5px; padding: 17px 18px; }
.library-overview-card span, .library-overview-card small, .library-list-heading span, .library-section-heading span { color: #64748b; font-size: 11px; }
.library-rule-warning { display: block; margin-top: 5px; color: #b45309; font-size: 11px; line-height: 1.5; }
.library-overview-card strong { color: #1f2937; font-size: 26px; line-height: 1; }
.library-create-panel { display: grid; gap: 14px; margin-bottom: 20px; padding: 18px; }
.library-section-heading, .library-list-heading, .library-detail-heading, .library-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.library-section-heading strong, .library-list-heading strong { color: #1f2937; font-size: 14px; }
.library-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.library-actions { justify-content: flex-end; }
.library-workspace { display: grid; grid-template-columns: minmax(250px, 0.8fr) minmax(0, 1.7fr); gap: 16px; align-items: start; }
.library-list-panel, .library-detail-panel { display: grid; gap: 16px; padding: 18px; }
.library-list { display: grid; max-height: 610px; gap: 6px; overflow-y: auto; }
.library-list-item { display: flex; min-width: 0; align-items: center; gap: 9px; padding: 10px; border: 1px solid transparent; border-radius: 6px; color: #475569; background: transparent; text-align: left; cursor: pointer; }
.library-list-item:hover, .library-list-item.active { border-color: #bfdbfe; background: #eff6ff; }
.library-list-icon { display: grid; width: 28px; height: 28px; flex: 0 0 auto; place-items: center; border-radius: 5px; color: #2563eb; background: #dbeafe; }
.library-list-copy { display: grid; min-width: 0; flex: 1; gap: 3px; }
.library-list-copy strong, .library-list-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.library-list-copy strong { color: #1f2937; font-size: 12px; }
.library-list-copy small { color: #94a3b8; font-size: 10px; }
.library-list-empty { padding: 30px 8px; color: #94a3b8; font-size: 12px; text-align: center; }
.library-detail-heading { align-items: flex-start; padding-bottom: 16px; border-bottom: 1px solid #f1f5f9; }
.library-detail-actions { display: flex; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.library-detail-actions :deep(.t-button) { min-height: 36px; padding: 0 15px; }
.library-detail-heading h2 { margin: 5px 0 4px; color: #1f2937; font-size: 18px; }
.library-detail-heading p { margin: 0; color: #64748b; font-size: 11px; }
.library-rule-number { width: 100%; padding: 0 28px; }
.library-rule-number :deep(.t-input-number__decrease), .library-rule-number :deep(.t-input-number__increase) { width: 28px; height: 32px; padding: 0; margin: 0; display: flex; align-items: center; justify-content: center; gap: 0; flex: 0 0 28px; line-height: 1; }
.library-rule-number :deep(.t-input-number__decrease) { left: 0; }
.library-rule-number :deep(.t-input-number__increase) { right: 0; }
.library-rule-number :deep(.t-input-number__decrease .t-button__icon), .library-rule-number :deep(.t-input-number__increase .t-button__icon) { margin: 0; }
.library-rule-number :deep(.t-input) { width: 100%; min-width: 0; }
.library-rule-number :deep(.t-input__inner) { text-align: center; }
.library-empty { display: grid; min-height: 330px; justify-items: center; align-content: center; gap: 8px; color: #94a3b8; font-size: 12px; }
@media (max-width: 860px) { .library-workspace { grid-template-columns: 1fr; } .library-list { max-height: 260px; } }
@media (max-width: 680px) { .library-overview-grid, .library-form-grid { grid-template-columns: 1fr; } .library-section-heading, .library-detail-heading, .library-actions { align-items: flex-start; flex-direction: column; } .library-actions { width: 100%; } .library-detail-actions { width: 100%; justify-content: flex-start; } }
</style>
