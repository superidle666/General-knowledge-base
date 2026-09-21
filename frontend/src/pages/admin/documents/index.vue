<!-- 文档管理：文件上传、元数据维护、标签关联和 Chunk 预览。 -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { AddIcon, EditIcon, FileIcon, SearchIcon } from 'tdesign-icons-vue-next'
import {
  mysqlApi,
  type Library,
} from '../../../api/mysql'
import { DOCUMENT_STATUS, PAGINATION } from '../../../constants'
import { mapDocument, type DocumentRow } from '../shared/formatters'
import DocumentDetailDialog from './DocumentDetailDialog.vue'
import ImportTaskDialog from './ImportTaskDialog.vue'
import DocumentUploadDialog from './DocumentUploadDialog.vue'
import TagManagerDialog from './TagManagerDialog.vue'

const props = defineProps<{
  filterLibraryId?: number
  uploadLibraryId?: number
  uploadRequestId?: number
}>()

const emit = defineEmits<{
  (event: 'update-counts', counts: { documents: number }): void
  (event: 'navigate', key: 'libraries' | 'documents', libraryId: number): void
}>()
const libraries = ref<Library[]>([])
const documents = ref<DocumentRow[]>([])
const search = ref('')
const selectedLibrary = ref('全部知识库')
const selectedTags = ref<string[]>([])
const tags = ref<Array<{ id: number; name: string }>>([])
const loading = ref(false)
const errorMessage = ref('')
const documentAction = ref<{ id: number; type: 'delete' }>()
const showUpload = ref(false)
const requestedUploadLibraryId = ref<number>()
const showDetail = ref(false)
const selectedDocumentId = ref<number>()
const showTagManager = ref(false)
const showImportTasks = ref(false)
const tablePage = ref(1)
const tablePageSize = ref<number>(PAGINATION.PAGE_SIZE)

const columns = [
  { colKey: 'name', title: '文档名称', width: 280 },
  { colKey: 'library', title: '所属知识库' },
  { colKey: 'type', title: '类型', width: 90, align: 'center' },
  { colKey: 'size', title: '大小', width: 110, align: 'center' },
  { colKey: 'updated', title: '更新时间', width: 160, align: 'center' },
  { colKey: 'statusLabel', title: '状态', width: 100, align: 'center' },
  { colKey: 'chunkCount', title: 'Chunk', width: 80, align: 'center' },
  { colKey: 'tags', title: '标签', width: 180 },
  { colKey: 'op', title: '操作', width: 190, align: 'center' },
]
const libraryTabs = computed(() => [
  '全部知识库',
  ...libraries.value
    .filter((item) => item.status === 'active' && !item.deleted_at)
    .map((item) => item.name),
])
const libraryOptions = computed(() =>
  libraries.value
    .filter((item) => item.status === 'active' && !item.deleted_at)
    .map((item) => ({ label: item.name, value: item.id })),
)
const tagOptions = computed(() => tags.value.map((tag) => ({ label: tag.name, value: tag.name })))
const selectedLibraryId = computed(() => {
  if (selectedLibrary.value === '全部知识库') return undefined
  return libraries.value.find((library) => library.name === selectedLibrary.value)?.id
})
const filteredDocuments = computed(() => {
  const query = search.value.trim().toLowerCase()
  return documents.value.filter((item) => {
    const matchesSearch = !query || item.name.toLowerCase().includes(query)
    const matchesLibrary =
      selectedLibrary.value === '全部知识库' || item.library === selectedLibrary.value
    const matchesTags =
      !selectedTags.value.length ||
      selectedTags.value.every((tag) => item.tags?.includes(tag))
    return matchesSearch && matchesLibrary && matchesTags
  })
})

const tablePagination = computed(() => ({
  current: tablePage.value,
  pageSize: tablePageSize.value,
  total: filteredDocuments.value.length,
  pageSizeOptions: [...PAGINATION.PAGE_SIZES],
}))

function handlePageChange(pageInfo: { current: number; pageSize: number }) {
  tablePage.value = pageInfo.current
  tablePageSize.value = pageInfo.pageSize
}

watch([search, selectedLibrary, selectedTags], () => {
  tablePage.value = 1
}, { deep: true })

watch(() => props.uploadRequestId, (requestId) => {
  if (!requestId || !props.uploadLibraryId) return
  requestedUploadLibraryId.value = props.uploadLibraryId
  showUpload.value = true
}, { immediate: true })

watch(showUpload, (visible) => {
  if (!visible) requestedUploadLibraryId.value = undefined
})

function getDocumentTheme(row: DocumentRow) {
  if (row.status === DOCUMENT_STATUS.READY) return 'success'
  if (row.status === DOCUMENT_STATUS.FAILED) return 'danger'
  if (row.status === DOCUMENT_STATUS.DELETED) return 'default'
  return 'warning'
}
async function loadDocuments() {
  loading.value = true
  errorMessage.value = ''
  try {
    const [libraryData, documentData, tagData] = await Promise.all([
      mysqlApi.listLibraries(true, false),
      mysqlApi.listDocuments(),
      mysqlApi.listTags(),
    ])
    libraries.value = libraryData
    const filterLibrary = libraryData.find((library) =>
      library.id === props.filterLibraryId && library.status === 'active' && !library.deleted_at,
    )
    if (filterLibrary) selectedLibrary.value = filterLibrary.name
    if (
      selectedLibrary.value !== '全部知识库' &&
      !libraryData.some(
        (library) =>
          library.status === 'active' &&
          !library.deleted_at &&
          library.name === selectedLibrary.value,
      )
    ) {
      selectedLibrary.value = '全部知识库'
    }
    tags.value = tagData
    selectedTags.value = selectedTags.value.filter((selectedTag) =>
      tagData.some((tag) => tag.name === selectedTag),
    )
    // 文档列表只展示启用知识库中的文档；重新启用知识库后文档会恢复显示。
    const activeLibraryIds = new Set(
      libraryData
        .filter((library) => library.status === 'active' && !library.deleted_at)
        .map((library) => library.id),
    )
    documents.value = documentData
      .filter((document) => activeLibraryIds.has(document.library_id))
      .map((document) => mapDocument(document, libraryData))
    emit('update-counts', { documents: documents.value.length })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '文档加载失败'
  } finally {
    loading.value = false
  }
}
function openDetail(row: DocumentRow) {
  selectedDocumentId.value = row.id
  showDetail.value = true
}
function openUploadForSelectedLibrary() {
  requestedUploadLibraryId.value = selectedLibraryId.value
  showUpload.value = true
}
function showError(message: string) {
  errorMessage.value = message
}
/** 删除文档前二次确认；失败时不会从当前列表中移除。 */
async function deleteDocument(row: DocumentRow) {
  if (documentAction.value) return
  if (!window.confirm(`确定删除文档“${row.name}”吗？删除后不可在列表中查看。`)) return

  documentAction.value = { id: row.id, type: 'delete' }
  try {
    await mysqlApi.deleteDocument(row.id)
    await loadDocuments()
  } catch (error) {
    showError(error instanceof Error ? error.message : '文档删除失败')
  } finally {
    documentAction.value = undefined
  }
}
function handleTagUpdated() {
  void loadDocuments()
}
function openTaskDocument(documentId: number) {
  selectedDocumentId.value = documentId
  showDetail.value = true
}
onMounted(() => void loadDocuments())
</script>

<template>
  <section class="admin-module">
    <div class="admin-page-head compact">
      <div><div class="eyebrow">KNOWLEDGE BASE</div><h1>文档管理</h1><p>上传、整理和维护设备技术资料。</p></div>
      <div class="document-head-actions">
        <t-button variant="outline" @click="showImportTasks = true">导入队列</t-button>
        <t-button theme="primary" class="create-action" @click="openUploadForSelectedLibrary"><AddIcon />新增文档</t-button>
      </div>
    </div>
    <div v-if="errorMessage" class="admin-error">{{ errorMessage }}<button @click="loadDocuments">重试</button></div>
    <div v-if="loading" class="admin-loading">正在加载文档数据...</div>
    <div class="toolbar">
      <div class="library-filter">
        <div class="library-tabs"><button v-for="library in libraryTabs" :key="library" :class="{ active: selectedLibrary === library }" @click="selectedLibrary = library">{{ library }}</button></div>
        <t-button theme="primary" size="small" class="metadata-action-button" @click="emit('navigate', 'libraries', selectedLibraryId || 0)">
          <template #icon><FileIcon /></template>
          知识库管理
        </t-button>
      </div>
      <div class="document-filters">
        <t-select v-model="selectedTags" :options="tagOptions" multiple filterable clearable placeholder="按标签筛选" class="tag-filter" />
        <t-button theme="primary" size="small" class="metadata-action-button" @click="showTagManager = true">
          <template #icon><EditIcon /></template>
          标签管理
        </t-button>
        <t-input v-model="search" placeholder="搜索文档名称" class="table-search"><template #prefix-icon><SearchIcon /></template></t-input>
      </div>
    </div>
    <div class="panel table-panel">
      <t-table :data="filteredDocuments" :columns="columns" row-key="id" bordered="false" table-layout="fixed" :pagination="tablePagination" @page-change="handlePageChange">
        <template #name="{ row }"><div class="doc-name"><FileIcon />{{ row.name }}</div></template>
        <template #statusLabel="{ row }"><t-tag :theme="getDocumentTheme(row)" variant="light">{{ row.statusLabel }}</t-tag></template>
        <template #chunkCount="{ row }"><span>{{ row.chunk_count ?? '-' }}</span></template>
        <template #tags="{ row }"><div class="document-tags"><t-tag v-for="tag in row.tags || []" :key="tag" variant="light-outline">{{ tag }}</t-tag><span v-if="!row.tags?.length">-</span></div></template>
        <template #op="{ row }">
          <div class="table-actions table-actions--multiple">
            <t-button
              variant="text"
              size="small"
              class="edit-action"
              :disabled="Boolean(documentAction)"
              @click="openDetail(row)"
            >详情</t-button>
            <t-button
              variant="text"
              theme="danger"
              size="small"
              :loading="documentAction?.id === row.id && documentAction?.type === 'delete'"
              :disabled="Boolean(documentAction) && documentAction?.id !== row.id"
              @click="deleteDocument(row)"
            >删除</t-button>
          </div>
        </template>
              <template #totalContent><span class="table-pagination-total">共 {{ filteredDocuments.length }} 条数据</span></template>
</t-table>
    </div>
    <ImportTaskDialog v-model:visible="showImportTasks" @error="showError" @updated="loadDocuments" @open-document="openTaskDocument" />
        <DocumentUploadDialog
      v-model:visible="showUpload"
      :library-options="libraryOptions"
      :initial-library-id="requestedUploadLibraryId"
      @uploaded="loadDocuments"
      @error="showError"
    />
    <DocumentDetailDialog
      v-model:visible="showDetail"
      :document-id="selectedDocumentId"
      @updated="loadDocuments"
      @error="showError"
    />
    <TagManagerDialog
      v-model:visible="showTagManager"
      @updated="handleTagUpdated"
      @error="showError"
    />
  </section>
</template>

<style scoped>
.document-head-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.library-filter { min-width: 0; flex: 1; display: flex; align-items: center; gap: 10px; }
.library-filter .library-tabs { min-width: 0; flex: 1; max-height: 52px; padding-bottom: 8px; overflow-x: auto; overflow-y: hidden; flex-wrap: nowrap; scrollbar-gutter: stable; }
.library-filter .library-tabs button { flex: 0 0 auto; white-space: nowrap; }
.metadata-action-button { flex: 0 0 auto; height: 32px; min-height: 32px; padding: 0 12px; color: #fff; white-space: nowrap; }
.metadata-action-button .t-button__content, .metadata-action-button .t-button__text { display: inline-flex; align-items: center; gap: 6px; line-height: 1; }
.metadata-action-button .t-button__icon, .metadata-action-button .t-icon { display: inline-flex; align-items: center; flex: 0 0 auto; margin: 0; color: #fff; }
.metadata-action-button:hover, .metadata-action-button:focus { color: #fff; }
.document-filters { display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-width: 0; }
.tag-filter { width: 180px; }
.tag-manage-button { flex: 0 0 auto; }
.doc-name { display: flex; align-items: center; gap: 8px; }
.doc-name svg { color: #2563eb; width: 16px; }
.document-tags { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.document-tags .t-tag { max-width: 130px; overflow: hidden; text-overflow: ellipsis; }
@media (max-width: 760px) { .document-head-actions { width: 100%; justify-content: flex-end; } }
@media (max-width: 560px) { .document-filters { align-items: stretch; flex-wrap: wrap; justify-content: flex-start; } .tag-filter, .table-search { width: 100%; max-width: none; } }
</style>
