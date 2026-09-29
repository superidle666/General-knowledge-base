<!--
 * @FilePath: @/pages/admin/libraries/LibraryDocumentsTab.vue
 * @Author: 项目维护者
 * @Date: 2026-09-28
 * @Description: 知识库详情页「文档」Tab：当前库文档表格、搜索筛选、详情与删除、导入队列。
 * @BusinessRule: 数据按 library_id 服务端过滤；停用库的文档在本库内仍可见（由页面头部提示条表意）；
 *   实体列只反映导入期识别结果，未识别的已就绪文档在强制识别库下不参与实体收窄；
 *   导入队列弹窗内的 5 秒轮询只记脏不刷新，关闭弹窗时才统一补一次列表与头部计数刷新；
 *   删除与重新处理都走 confirmDialog 对话框（不用浏览器原生 confirm），文案里必须说清重跑期间文档短暂退出回答来源。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { FileIcon, SearchIcon } from 'tdesign-icons-vue-next'
import { MessagePlugin } from 'tdesign-vue-next'
import { mysqlApi, type Library } from '../../../api/mysql'
import { PAGINATION, USER_ROLE } from '../../../constants'
import { useRequest } from '../../../composables'
import { useUserStore } from '../../../store/modules/user'
import { confirmDialog } from '../../../utils/confirm'
import { mapDocument, type DocumentRow } from '../shared/formatters'
import DocumentDetailDialog from '../documents/DocumentDetailDialog.vue'
import ImportTaskDialog from '../documents/ImportTaskDialog.vue'

const props = defineProps<{ library: Library }>()

const userStore = useUserStore()
const isSystemAdmin = computed(() => userStore.userInfo?.roles.includes(USER_ROLE.ADMIN) ?? false)

/** 文档变更后上抛给详情页壳（刷新 document_count 等头部信息）。 */
const emit = defineEmits<{ (event: 'changed'): void }>()

const search = ref('')
const documentAction = ref<{ id: number; type: 'delete' | 'reprocess' }>()
const reprocessingAll = ref(false)
const showDetail = ref(false)
const selectedDocumentId = ref<number>()
const showImportTasks = ref(false)
// 队列弹窗打开期间的变化标记：导入任务每 5 秒轮询一次，期间不做整页级联刷新，关闭时统一补一次。
const importQueueDirty = ref(false)
const tablePage = ref(1)
const tablePageSize = ref<number>(PAGINATION.PAGE_SIZE)
const mutationError = ref('')

const columns = [
  { colKey: 'name', title: '文档名称', width: 280 },
  { colKey: 'type', title: '类型', width: 90, align: 'center' },
  { colKey: 'size', title: '大小', width: 110, align: 'center' },
  { colKey: 'updated', title: '更新时间', width: 160, align: 'center' },
  { colKey: 'statusLabel', title: '状态', width: 100, align: 'center' },
  // 实体列说“导入期有没有识别出主实体”，与检索时能不能按实体收窄直接相关。
  { colKey: 'entity', title: '实体', width: 170, align: 'center' },
  { colKey: 'chunkCount', title: 'Chunk', width: 80, align: 'center' },
  { colKey: 'op', title: '操作', width: 270, align: 'center' },
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

/** 已有实体标注的文档数：未启用库也能据此判断“切为强制是否立即生效”。 */
const entityAnnotatedCount = computed(() => documents.value.filter((item) => item.entity_name).length)
/** required 库里已就绪但无实体的文档：它们必须重新导入才会参与实体收窄。 */
const entityMissingCount = computed(
  () => documents.value.filter((item) => item.status === 'ready' && !item.entity_name).length,
)

/** 实体识别与当前文档集是否对得上；对不上时必须说出来，否则管理员只能靠猜。 */
const entityHint = computed(() => {
  if (props.library.entity_recognition_mode === 'required') {
    return entityMissingCount.value > 0
      ? `本库有 ${entityMissingCount.value} 份已就绪文档在导入期未识别出实体，检索时不参与实体收窄，可用「重新处理」按当前配置补齐。`
      : ''
  }
  return entityAnnotatedCount.value > 0
    ? `本库已有 ${entityAnnotatedCount.value} 份文档带实体标注，当前未启用实体识别；切换为强制后可直接生效。`
    : ''
})

/** 与后端闸门一致：required 库的重新处理只允许系统管理员触发。 */
const canReprocess = computed(() => isSystemAdmin.value || props.library.entity_recognition_mode === 'disabled')
/** required 库里已就绪但没实体的文档：重跑一次导入就能补齐。 */
const reprocessTargets = computed(() =>
  props.library.entity_recognition_mode === 'required'
    ? documents.value.filter((item) => item.status === 'ready' && !item.entity_name)
    : [],
)

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
  importQueueDirty.value = false
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
/** 队列指纹变化：只记脏，不刷新——轮询驱动的 loadDocuments 会让表格每 5 秒重载一次。 */
function markImportQueueDirty() {
  importQueueDirty.value = true
}
/** 关闭队列弹窗：期间若有任务变化（导入完成/失败/取消），此刻才补刷新列表与头部计数。 */
function handleImportQueueClosed() {
  if (!importQueueDirty.value) return
  importQueueDirty.value = false
  handleDocumentChanged()
}
/** 删除文档：确认后再删；失败时不会从当前列表中移除。 */
async function deleteDocument(row: DocumentRow) {
  if (documentAction.value) return
  const confirmed = await confirmDialog({
    header: '删除文档',
    body: `确定删除文档“${row.name}”吗？删除后不可在列表中查看。`,
    confirmText: '删除',
    danger: true,
  })
  if (!confirmed) return

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

/** 重跑导入的公共流程：确认 → 受理 → 刷新列表与壳头部计数。 */
async function runReprocess(documentIds: number[]) {
  if (!documentIds.length) return
  try {
    const outcomes = await mysqlApi.reprocessDocuments(documentIds)
    const notQueued = outcomes.filter((item) => !item.enqueued).length
    await MessagePlugin.success(
      notQueued
        ? `已受理 ${outcomes.length} 份文档，其中 ${notQueued} 份暂未排入队列（Worker 重启会自行补入队）。`
        : `已受理 ${outcomes.length} 份文档，正在按知识库当前配置重新处理。`,
    )
    handleDocumentChanged()
  } catch (error) {
    showError(error instanceof Error ? error.message : '重新处理受理失败')
  }
}

/** 单档重跑：确认后用原始文件按知识库当前配置重跑一遍导入。 */
async function reprocessOne(row: DocumentRow) {
  if (documentAction.value || reprocessingAll.value) return
  const confirmed = await confirmDialog({
    header: '按当前配置重新处理',
    body:
      `确定重新处理“${row.name}”吗？会用原始文件按知识库当前配置重跑一遍导入；` +
      '期间这份文档会短暂不出现在回答来源里，新结果落地后替换旧切片。',
    confirmText: '重新处理',
  })
  if (!confirmed) return
  documentAction.value = { id: row.id, type: 'reprocess' }
  try {
    await runReprocess([row.id])
  } finally {
    documentAction.value = undefined
  }
}

/** 批量补齐：把本库已就绪但没有实体记录的文档一次重跑。 */
async function reprocessMissingEntities() {
  if (documentAction.value || reprocessingAll.value) return
  const documentIds = reprocessTargets.value.map((item) => item.id)
  if (!documentIds.length) return
  const confirmed = await confirmDialog({
    header: '按当前配置重新处理',
    body:
      `本库有 ${documentIds.length} 份已就绪文档没有实体记录，将按知识库当前配置重新处理它们。` +
      '重跑期间这些文档会短暂不出现在回答来源里，旧切片会被新结果替换，不会重复。',
    confirmText: '重新处理',
  })
  if (!confirmed) return
  reprocessingAll.value = true
  try {
    await runReprocess(documentIds)
  } finally {
    reprocessingAll.value = false
  }
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
      <t-button
        v-if="canReprocess && reprocessTargets.length"
        class="docs-reprocess-all"
        variant="outline"
        size="small"
        :loading="reprocessingAll"
        :disabled="Boolean(documentAction)"
        @click="reprocessMissingEntities"
        >重新处理未识别实体的 {{ reprocessTargets.length }} 份</t-button
      >
    </div>
    <div v-if="entityHint" class="docs-entity-hint">{{ entityHint }}</div>
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
        <template #entity="{ row }">
          <t-tag v-if="row.entity_name" theme="success" variant="light">{{ row.entity_name }}</t-tag>
          <t-tag
            v-else-if="row.status === 'ready' && props.library.entity_recognition_mode === 'required'"
            theme="warning"
            variant="light"
            title="导入期未识别出主实体，需重新导入才会参与实体收窄"
            >待重导入</t-tag
          >
          <span v-else class="docs-entity-empty">-</span>
        </template>
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
              size="small"
              :disabled="!canReprocess || row.status !== 'ready' || Boolean(documentAction) || reprocessingAll"
              :loading="documentAction?.id === row.id && documentAction?.type === 'reprocess'"
              @click="reprocessOne(row)"
              >重新处理</t-button
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
      @updated="markImportQueueDirty"
      @closed="handleImportQueueClosed"
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
.docs-entity-hint {
  margin: -4px 0 10px;
  color: var(--text-soft);
  font-size: 12px;
}
.docs-reprocess-all {
  margin-left: 12px;
}
.docs-entity-empty {
  color: var(--text-soft);
}
@media (max-width: 560px) {
  .table-search {
    width: 100%;
    max-width: none;
  }
}
</style>
