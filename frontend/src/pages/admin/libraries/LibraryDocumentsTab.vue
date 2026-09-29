<!--
 * @FilePath: @/pages/admin/libraries/LibraryDocumentsTab.vue
 * @Author: 项目维护者
 * @Date: 2026-09-28
 * @Description: 知识库详情页「文档」Tab：当前库文档表格、搜索筛选、详情与删除、导入队列。
 * @BusinessRule: 数据按 library_id 服务端过滤；停用库的文档在本库内仍可见（由页面头部提示条表意）。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { FileIcon, SearchIcon } from 'tdesign-icons-vue-next'
import { mysqlApi, type Library } from '../../../api/mysql'
import { PAGINATION } from '../../../constants'
import { useRequest } from '../../../composables'
import { mapDocument, type DocumentRow } from '../shared/formatters'
import DocumentDetailDialog from '../documents/DocumentDetailDialog.vue'
import ImportTaskDialog from '../documents/ImportTaskDialog.vue'

const props = defineProps<{ library: Library }>()

/** 文档变更后上抛给详情页壳（刷新 document_count 等头部信息）。 */
const emit = defineEmits<{ (event: 'changed'): void }>()

const search = ref('')
const documentAction = ref<{ id: number; type: 'delete' }>()
const showDetail = ref(false)
const selectedDocumentId = ref<number>()
const showImportTasks = ref(false)
const tablePage = ref(1)
const tablePageSize = ref<number>(PAGINATION.PAGE_SIZE)
const mutationError = ref('')

const columns = [
  { colKey: 'name', title: '文档名称', width: 280 },
  { colKey: 'type', title: '类型', width: 90, align: 'center' },
  { colKey: 'size', title: '大小', width: 110, align: 'center' },
  { colKey: 'updated', title: '更新时间', width: 160, align: 'center' },
  { colKey: 'statusLabel', title: '状态', width: 100, align: 'center' },
  { colKey: 'chunkCount', title: 'Chunk', width: 80, align: 'center' },
  { colKey: 'op', title: '操作', width: 190, align: 'center' },
]

/** 按库加载文档；服务端 library_id 过滤，不做跨库展示。 */
const documentsRequest = useRequest(
  async () => {
    const documentData = await mysqlApi.listDocuments(`library_id=${props.library.id}`)
    return documentData.map((document) => mapDocument(document, [props.library]))
  },
  { immediate: true },
)
const documents = computed(() => documentsRequest.data.value ?? [])
const loading = documentsRequest.loading
const errorMessage = computed(() => mutationError.value || documentsRequest.error.value?.message || '')

const filteredDocuments = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return documents.value
  return documents.value.filter((item) => item.name.toLowerCase().includes(query))
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

watch(search, () => {
  tablePage.value = 1
})

// 同路由内切换库 id（如卡片跳转）时重拉列表；保存/状态切换不改 id，不触发双拉。
watch(
  () => props.library.id,
  () => {
    tablePage.value = 1
    void loadDocuments()
  },
)

/** 重新加载文档列表；加载错误由 errorMessage 统一展示。 */
async function loadDocuments() {
  mutationError.value = ''
  try {
    await documentsRequest.execute()
  } catch {
    // 加载错误通过 documentsRequest.error 在模板中展示
  }
}

/** 壳层上传弹窗完成后由父组件调用，刷新本 Tab 列表。 */
async function reload() {
  await loadDocuments()
}
/** 头部「导入队列」按钮触发：打开本 Tab 持有的导入任务弹窗。 */
function openImportQueue() {
  showImportTasks.value = true
}
defineExpose({ reload, openImportQueue })

function openDetail(row: DocumentRow) {
  selectedDocumentId.value = row.id
  showDetail.value = true
}
function showError(message: string) {
  mutationError.value = message
}
/** 文档变更后同时刷新列表与壳头部计数。 */
function handleDocumentChanged() {
  void loadDocuments()
  emit('changed')
}
/** 删除文档前二次确认；失败时不会从当前列表中移除。 */
async function deleteDocument(row: DocumentRow) {
  if (documentAction.value) return
  if (!window.confirm(`确定删除文档“${row.name}”吗？删除后不可在列表中查看。`)) return

  documentAction.value = { id: row.id, type: 'delete' }
  try {
    await mysqlApi.deleteDocument(row.id)
    await loadDocuments()
    emit('changed')
  } catch (error) {
    showError(error instanceof Error ? error.message : '文档删除失败')
  } finally {
    documentAction.value = undefined
  }
}
function openTaskDocument(documentId: number) {
  selectedDocumentId.value = documentId
  showDetail.value = true
}
</script>

<template>
  <div class="library-docs-tab">
    <div v-if="errorMessage" class="admin-error">{{ errorMessage }}<button @click="loadDocuments">重试</button></div>
    <div v-if="loading" class="admin-loading">正在加载文档数据...</div>
    <div class="toolbar document-toolbar">
      <t-input v-model="search" placeholder="搜索文档名称" class="table-search"
        ><template #prefix-icon><SearchIcon /></template
      ></t-input>
    </div>
    <div class="panel table-panel">
      <t-table
        :data="filteredDocuments"
        :columns="columns"
        row-key="id"
        bordered="false"
        table-layout="fixed"
        :pagination="tablePagination"
        @page-change="handlePageChange"
      >
        <template #name="{ row }"
          ><div class="doc-name"><FileIcon />{{ row.name }}</div></template
        >
        <template #statusLabel="{ row }"
          ><t-tag :theme="row.statusTheme" variant="light">{{ row.statusLabel }}</t-tag></template
        >
        <template #chunkCount="{ row }"
          ><span>{{ row.chunk_count ?? '-' }}</span></template
        >
        <template #op="{ row }">
          <div class="table-actions table-actions--multiple">
            <t-button
              variant="text"
              size="small"
              class="edit-action"
              :disabled="Boolean(documentAction)"
              @click="openDetail(row)"
              >详情</t-button
            >
            <t-button
              variant="text"
              theme="danger"
              size="small"
              :loading="documentAction?.id === row.id && documentAction?.type === 'delete'"
              :disabled="Boolean(documentAction) && documentAction?.id !== row.id"
              @click="deleteDocument(row)"
              >删除</t-button
            >
          </div>
        </template>
        <template #totalContent
          ><span class="table-pagination-total">共 {{ filteredDocuments.length }} 条数据</span></template
        >
      </t-table>
    </div>
    <ImportTaskDialog
      v-model:visible="showImportTasks"
      @error="showError"
      @updated="handleDocumentChanged"
      @open-document="openTaskDocument"
    />
    <DocumentDetailDialog
      v-model:visible="showDetail"
      :document-id="selectedDocumentId"
      @updated="handleDocumentChanged"
      @error="showError"
    />
  </div>
</template>

<style scoped>
.library-docs-tab {
  display: grid;
  gap: 0;
}
.document-toolbar {
  align-items: flex-start;
  margin-top: 12px;
  margin-bottom: 12px;
}
.table-search {
  margin-left: 8px;
}
.doc-name {
  display: flex;
  align-items: center;
  gap: 8px;
}
.doc-name svg {
  color: var(--primary);
  width: 16px;
}
@media (max-width: 560px) {
  .table-search {
    width: 100%;
    max-width: none;
  }
}
</style>
