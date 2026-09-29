<!-- 文档导入任务：按创建日期查看任务、处理失败重试、取消和清理终态记录。 -->
<!-- 事件约定：updated 只表示队列指纹变化（包含轮询期间的进度变化），消费方不得直接接整页刷新；
     需要把任务结果同步到外部列表时，监听弹窗关闭对应的 closed。 -->
<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { mysqlApi, type ImportTask } from '../../../api/mysql'
import { TASK_STATUS } from '../../../constants'
import { formatDate } from '../../../utils/date'
import { confirmDialog } from '../../../utils/confirm'
import { TASK_STATUS_META, getStatusMeta, type StatusMeta } from '../shared/statusMeta'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'error', message: string): void
  (event: 'updated'): void
  (event: 'closed'): void
  (event: 'open-document', documentId: number): void
}>()

const selectedDate = ref(getLocalDateValue())
const selectedStatus = ref('')
const tasks = ref<ImportTask[]>([])
const loading = ref(false)
const activeTaskId = ref<number>()
const confirmVisible = ref(false)
const confirmTask = ref<ImportTask>()
const confirmName = ref('')
const confirmBusy = ref(false)
const confirmNoEntityBusy = ref(false)
let refreshTimer: ReturnType<typeof setInterval> | undefined
let lastTaskFingerprint = ''

const statusOptions = [
  { label: '全部状态', value: '' },
  { label: '排队中', value: TASK_STATUS.QUEUED },
  { label: '处理中', value: TASK_STATUS.PROCESSING },
  { label: '待确认', value: TASK_STATUS.NEEDS_REVIEW },
  { label: '已完成', value: TASK_STATUS.COMPLETED },
  { label: '失败', value: TASK_STATUS.FAILED },
  { label: '已取消', value: TASK_STATUS.CANCELLED },
]

const taskSummary = computed(() => ({
  total: tasks.value.length,
  active: tasks.value.filter((task) => task.status === TASK_STATUS.QUEUED || task.status === TASK_STATUS.PROCESSING)
    .length,
  review: tasks.value.filter((task) => task.status === TASK_STATUS.NEEDS_REVIEW).length,
  failed: tasks.value.filter((task) => task.status === TASK_STATUS.FAILED).length,
}))

// 待确认任务的候选实体来自 worker 写入的 entity_result.candidates，供人工点选。
const confirmCandidates = computed<string[]>(() => {
  const raw = confirmTask.value?.entity_result?.candidates
  if (!Array.isArray(raw)) return []
  return raw.map((item) => String(item)).filter(Boolean)
})

function getLocalDateValue(date = new Date()) {
  const offsetDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return offsetDate.toISOString().slice(0, 10)
}

function buildQuery() {
  const params = new URLSearchParams({ import_date: selectedDate.value })
  if (selectedStatus.value) params.set('task_status', selectedStatus.value)
  return params.toString()
}

function formatTime(value: string | null) {
  return value ? formatDate(value, 'YYYY-MM-DD HH:mm:ss') : '-'
}

function taskStatusMeta(status: string): StatusMeta {
  return getStatusMeta(TASK_STATUS_META, status)
}

function taskStepLabel(step: string | null) {
  return (
    (
      {
        queued: '等待 Worker',
        queue_pending: '等待重新入队',
        recovered_after_worker_restart: 'Worker 重启后恢复',
        worker_claimed: 'Worker 已接收',
        node_entry: '检查文件',
        node_pdf_to_md: '解析 PDF',
        node_md_img: '处理图片和 OCR',
        node_document_split: '切分文档',
        node_entity_recognition_disabled: '跳过实体识别',
        node_entity_recognition_required: '强制识别实体',
        entity_review: '等待确认实体',
        node_bge_embedding: '生成向量',
        node_import_milvus: '写入向量库',
        node_persist_chunks: '保存 Chunk',
        worker_failed: 'Worker 失败',
        worker_cancelled: '已取消',
      } as Record<string, string>
    )[step || ''] || '处理中'
  )
}

function canDelete(task: ImportTask) {
  return !([TASK_STATUS.QUEUED, TASK_STATUS.PROCESSING] as string[]).includes(task.status)
}

async function loadTasks() {
  loading.value = true
  try {
    const nextTasks = await mysqlApi.listImportTasks(buildQuery())
    const fingerprint = nextTasks
      .map((task) => `${task.id}:${task.status}:${task.progress}:${task.updated_at}`)
      .join('|')
    tasks.value = nextTasks
    if (fingerprint !== lastTaskFingerprint) {
      lastTaskFingerprint = fingerprint
      emit('updated')
    }
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '导入任务加载失败')
  } finally {
    loading.value = false
  }
}

async function retryTask(task: ImportTask) {
  activeTaskId.value = task.id
  try {
    await mysqlApi.retryImportTask(task.id)
    await loadTasks()
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '重试导入失败')
  } finally {
    activeTaskId.value = undefined
  }
}

async function cancelTask(task: ImportTask) {
  activeTaskId.value = task.id
  try {
    await mysqlApi.cancelImportTask(task.id)
    await loadTasks()
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '取消导入失败')
  } finally {
    activeTaskId.value = undefined
  }
}

function entityGuess(task: ImportTask) {
  const raw = task.entity_result?.guess
  return typeof raw === 'string' ? raw : ''
}

function openConfirmDialog(task: ImportTask) {
  confirmTask.value = task
  confirmName.value = entityGuess(task) || confirmCandidates.value[0] || ''
  confirmVisible.value = true
}

function closeConfirmDialog() {
  if (confirmBusy.value || confirmNoEntityBusy.value) return
  confirmVisible.value = false
  confirmTask.value = undefined
  confirmName.value = ''
}

function handleConfirmVisible(visible: boolean) {
  if (!visible) closeConfirmDialog()
}

function resetConfirmDialog() {
  confirmVisible.value = false
  confirmTask.value = undefined
  confirmName.value = ''
}

async function submitConfirmEntity() {
  const task = confirmTask.value
  const name = confirmName.value.trim()
  if (!task || !name) return
  confirmBusy.value = true
  try {
    await mysqlApi.confirmImportTaskEntity(task.id, { item_name: name })
    resetConfirmDialog()
    await loadTasks()
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '确认主实体失败')
  } finally {
    confirmBusy.value = false
  }
}

async function confirmNoEntity() {
  const task = confirmTask.value
  if (!task) return
  // 本按钮已经开在一个 t-dialog 内，再确认不能继续套内联弹窗，所以走编程式对话框。
  const confirmed = await confirmDialog({
    header: '确认无实体',
    body: '确认该文档没有明确主实体？要求实体的知识库无法导入无实体文档，该导入任务将被标记为失败。',
    confirmText: '确认无实体',
    danger: true,
  })
  if (!confirmed) return
  confirmNoEntityBusy.value = true
  try {
    await mysqlApi.confirmImportTaskEntity(task.id, { no_entity: true })
    resetConfirmDialog()
    await loadTasks()
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '确认无实体失败')
  } finally {
    confirmNoEntityBusy.value = false
  }
}

async function deleteTask(task: ImportTask) {
  const confirmed = await confirmDialog({
    header: '删除导入任务记录',
    body:
      `确定删除“${task.document_title || `文档 #${task.document_id}`}”的导入任务记录吗？` +
      '这不会删除原文档和已入库的知识。',
    confirmText: '删除',
    danger: true,
  })
  if (!confirmed) return
  activeTaskId.value = task.id
  try {
    await mysqlApi.deleteImportTask(task.id)
    await loadTasks()
  } catch (error) {
    emit('error', error instanceof Error ? error.message : '删除导入任务失败')
  } finally {
    activeTaskId.value = undefined
  }
}

function stopPolling() {
  if (refreshTimer) clearInterval(refreshTimer)
  refreshTimer = undefined
}

function startPolling() {
  stopPolling()
  refreshTimer = setInterval(() => void loadTasks(), 5_000)
}

watch(
  () => props.visible,
  (visible, previousVisible) => {
    if (visible) {
      void loadTasks()
      startPolling()
    } else {
      stopPolling()
      // 关闭时上抛一次：队列变化对文档列表和头部计数的影响统一在这一刻补刷新，
      // 避免父侧把 5 秒轮询的 updated 当作整页重加载的触发源。
      if (previousVisible) emit('closed')
    }
  },
  { immediate: true },
)

watch([selectedDate, selectedStatus], () => {
  if (props.visible) void loadTasks()
})

onUnmounted(stopPolling)
</script>

<template>
  <t-dialog
    :visible="visible"
    header="导入队列"
    width="900px"
    :footer="false"
    placement="center"
    @update:visible="emit('update:visible', $event)"
  >
    <div class="import-queue">
      <div class="queue-toolbar">
        <div class="queue-date-filter">
          <label for="import-task-date">导入日期</label>
          <t-date-picker
            id="import-task-date"
            v-model="selectedDate"
            value-type="YYYY-MM-DD"
            format="YYYY-MM-DD"
            :clearable="false"
          />
        </div>
        <t-select v-model="selectedStatus" :options="statusOptions" class="queue-status-filter" />
        <t-button variant="outline" :loading="loading" @click="loadTasks">刷新</t-button>
      </div>

      <div class="queue-summary" aria-label="当天导入任务统计">
        <div>
          <strong>{{ taskSummary.total }}</strong
          ><span>当天任务</span>
        </div>
        <div>
          <strong>{{ taskSummary.active }}</strong
          ><span>进行中</span>
        </div>
        <div>
          <strong :class="{ review: taskSummary.review > 0 }">{{ taskSummary.review }}</strong
          ><span>待确认实体</span>
        </div>
        <div>
          <strong :class="{ danger: taskSummary.failed > 0 }">{{ taskSummary.failed }}</strong
          ><span>失败待处理</span>
        </div>
        <p>展示任务提交日期；处理中的任务每 5 秒自动刷新。</p>
      </div>

      <div v-if="loading && !tasks.length" class="queue-state">正在加载导入任务...</div>
      <div v-else-if="!tasks.length" class="queue-state">{{ selectedDate }} 暂无导入任务</div>
      <div v-else class="task-list">
        <article v-for="task in tasks" :key="task.id" class="task-card">
          <div class="task-card-header">
            <div class="task-title">
              <strong :title="task.document_title || `文档 #${task.document_id}`">{{
                task.document_title || `文档 #${task.document_id}`
              }}</strong>
              <span>{{ task.library_name || `知识库 #${task.library_id}` }}</span>
            </div>
            <t-tag :theme="taskStatusMeta(task.status).theme" variant="light">{{
              taskStatusMeta(task.status).label
            }}</t-tag>
          </div>
          <t-progress :percentage="task.progress" :show-info="true" size="small" />
          <div class="task-meta">
            <span>提交：{{ formatTime(task.created_at) }}</span>
            <span>开始：{{ formatTime(task.started_at) }}</span>
            <span>结束：{{ formatTime(task.finished_at) }}</span>
            <span>切片：{{ task.chunk_count ?? '-' }}</span>
          </div>
          <p v-if="task.error_message" class="task-error">失败原因：{{ task.error_message }}</p>
          <div class="task-card-footer">
            <span class="task-step">当前步骤：{{ taskStepLabel(task.current_step) }}</span>
            <div class="task-actions">
              <t-button
                v-if="task.chunk_count"
                variant="text"
                size="small"
                @click="emit('open-document', task.document_id)"
                >查看 Chunk</t-button
              >
              <t-button
                v-if="task.status === TASK_STATUS.NEEDS_REVIEW"
                variant="text"
                theme="primary"
                size="small"
                @click="openConfirmDialog(task)"
                >确认实体</t-button
              >
              <t-button
                v-if="task.status === TASK_STATUS.FAILED"
                variant="text"
                size="small"
                :loading="activeTaskId === task.id"
                @click="retryTask(task)"
                >重试</t-button
              >
              <t-button
                v-if="task.status === TASK_STATUS.QUEUED || task.status === TASK_STATUS.PROCESSING"
                variant="text"
                theme="danger"
                size="small"
                :loading="activeTaskId === task.id"
                @click="cancelTask(task)"
                >取消</t-button
              >
              <t-button
                v-if="canDelete(task)"
                variant="text"
                theme="danger"
                size="small"
                :loading="activeTaskId === task.id"
                @click="deleteTask(task)"
                >删除记录</t-button
              >
            </div>
          </div>
        </article>
      </div>
    </div>
  </t-dialog>

  <t-dialog
    :visible="confirmVisible"
    header="确认主实体"
    width="520px"
    placement="center"
    :confirm-btn="{ content: '确认并重新导入', theme: 'primary', loading: confirmBusy, disabled: !confirmName.trim() }"
    cancel-btn="稍后处理"
    @update:visible="handleConfirmVisible"
    @confirm="submitConfirmEntity"
    @cancel="closeConfirmDialog"
  >
    <div class="entity-confirm">
      <p class="entity-confirm-hint">
        该文档来自强制识别知识库，系统未能确定唯一主实体。请从候选中选择、手动填写，或确认无实体后终止导入。
      </p>
      <div v-if="confirmCandidates.length" class="entity-candidates">
        <span class="entity-candidates__label">识别候选</span>
        <div class="entity-candidates__list">
          <button
            v-for="candidate in confirmCandidates"
            :key="candidate"
            type="button"
            class="entity-chip"
            :class="{ 'is-active': confirmName === candidate }"
            @click="confirmName = candidate"
          >
            {{ candidate }}
          </button>
        </div>
      </div>
      <t-input v-model="confirmName" placeholder="填写或选择主实体名称" :maxlength="255" clearable />
      <t-button theme="danger" variant="text" size="small" :loading="confirmNoEntityBusy" @click="confirmNoEntity"
        >确认无实体并终止导入</t-button
      >
    </div>
  </t-dialog>
</template>

<style scoped>
.import-queue {
  display: grid;
  gap: 16px;
  min-height: 420px;
}
.queue-toolbar {
  display: flex;
  align-items: end;
  gap: 10px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.queue-date-filter {
  display: grid;
  gap: 6px;
}
.queue-date-filter label {
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
}
.queue-status-filter {
  width: 130px;
}
.queue-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 110px)) minmax(0, 1fr);
  gap: 10px;
  align-items: stretch;
}
.queue-summary > div {
  display: grid;
  align-content: center;
  gap: 2px;
  min-height: 68px;
  padding: 10px 12px;
  border: 1px solid var(--primary-soft);
  border-radius: 8px;
  background: var(--surface-alt);
}
.queue-summary strong {
  color: var(--primary-deep);
  font-size: 22px;
  line-height: 1;
}
.queue-summary strong.danger {
  color: var(--danger);
}
.queue-summary strong.review {
  color: var(--warning-strong);
}
.queue-summary span {
  color: var(--muted);
  font-size: 11px;
}
.queue-summary p {
  align-self: center;
  margin: 0;
  color: var(--weak);
  font-size: 12px;
  line-height: 20px;
}
.queue-state {
  display: grid;
  min-height: 250px;
  place-items: center;
  color: var(--weak);
  font-size: 13px;
}
.task-list {
  display: grid;
  gap: 10px;
  max-height: 510px;
  overflow: auto;
  padding-right: 4px;
}
.task-card {
  display: grid;
  gap: 11px;
  padding: 15px 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  transition:
    border-color 0.2s,
    box-shadow 0.2s;
}
.task-card:hover {
  border-color: var(--primary-border);
  box-shadow: var(--shadow-glow);
}
.task-card-header,
.task-card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.task-title {
  display: grid;
  min-width: 0;
  gap: 4px;
}
.task-title strong {
  overflow: hidden;
  color: var(--text);
  font-size: 14px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-title span,
.task-step {
  color: var(--muted);
  font-size: 12px;
}
.task-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  color: var(--weak);
  font-size: 11px;
}
.task-error {
  margin: 0;
  padding: 8px 10px;
  color: var(--danger-deep);
  background: var(--danger-bg);
  border-radius: 5px;
  font-size: 12px;
  line-height: 18px;
  white-space: pre-wrap;
  overflow-wrap: break-word;
}
.task-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}
.entity-confirm {
  display: grid;
  gap: 14px;
}
.entity-confirm-hint {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 20px;
}
.entity-candidates {
  display: grid;
  gap: 8px;
}
.entity-candidates__label {
  color: var(--weak);
  font-size: 12px;
}
.entity-candidates__list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.entity-chip {
  padding: 4px 12px;
  color: var(--primary-deep);
  background: var(--primary-weak);
  border: 1px solid var(--primary-border);
  border-radius: 999px;
  font-size: 13px;
  cursor: pointer;
  transition:
    color 0.2s,
    background 0.2s,
    border-color 0.2s;
}
.entity-chip:hover {
  border-color: var(--primary-border-strong);
}
.entity-chip.is-active {
  color: var(--on-primary);
  background: var(--primary);
  border-color: var(--primary);
}
@media (max-width: 700px) {
  .queue-toolbar {
    align-items: stretch;
    flex-wrap: wrap;
  }
  .queue-date-filter {
    flex: 1;
  }
  .queue-date-filter :deep(.t-date-picker) {
    width: 100%;
  }
  .queue-status-filter {
    width: calc(50% - 5px);
  }
  .queue-summary {
    grid-template-columns: repeat(2, 1fr);
  }
  .queue-summary p {
    grid-column: 1 / -1;
  }
  .task-card-header,
  .task-card-footer {
    align-items: flex-start;
    flex-direction: column;
  }
  .task-actions {
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
