<!-- 文档导入任务：按创建日期查看任务、处理失败重试、取消和清理终态记录。 -->
<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { mysqlApi, type ImportTask } from '../../../api/mysql'
import { TASK_STATUS } from '../../../constants'
import { formatDate } from '../../../utils/date'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'error', message: string): void
  (event: 'updated'): void
  (event: 'open-document', documentId: number): void
}>()

const selectedDate = ref(getLocalDateValue())
const selectedStatus = ref('')
const tasks = ref<ImportTask[]>([])
const loading = ref(false)
const activeTaskId = ref<number>()
let refreshTimer: ReturnType<typeof setInterval> | undefined
let lastTaskFingerprint = ''

const statusOptions = [
  { label: '全部状态', value: '' },
  { label: '排队中', value: 'queued' },
  { label: '处理中', value: 'processing' },
  { label: '已完成', value: 'completed' },
  { label: '失败', value: 'failed' },
  { label: '已取消', value: 'cancelled' },
]

const taskSummary = computed(() => ({
  total: tasks.value.length,
  active: tasks.value.filter((task) => task.status === 'queued' || task.status === 'processing').length,
  failed: tasks.value.filter((task) => task.status === 'failed').length,
}))

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

function taskStatusLabel(status: string) {
  return ({ queued: '排队中', processing: '处理中', completed: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[status] || status
}

function taskStepLabel(step: string | null) {
  return ({
    queued: '等待 Worker',
    queue_pending: '等待重新入队',
    recovered_after_worker_restart: 'Worker 重启后恢复',
    worker_claimed: 'Worker 已接收',
    node_entry: '检查文件',
    node_pdf_to_md: '解析 PDF',
    node_md_img: '处理图片和 OCR',
    node_document_split: '切分文档',
    node_entity_recognition_disabled: '跳过实体识别',
    node_entity_recognition_optional: '智能识别实体',
    node_entity_recognition_required: '强制识别实体',
    node_bge_embedding: '生成向量',
    node_import_milvus: '写入向量库',
    node_persist_chunks: '保存 Chunk',
    worker_failed: 'Worker 失败',
    worker_cancelled: '已取消',
  } as Record<string, string>)[step || ''] || '处理中'
}

function getTaskTheme(status: string) {
  if (status === TASK_STATUS.COMPLETED) return 'success'
  if (status === TASK_STATUS.FAILED) return 'danger'
  if (status === 'cancelled') return 'default'
  return 'warning'
}

function canDelete(task: ImportTask) {
  return !['queued', 'processing'].includes(task.status)
}

async function loadTasks() {
  loading.value = true
  try {
    const nextTasks = await mysqlApi.listImportTasks(buildQuery())
    const fingerprint = nextTasks.map((task) => `${task.id}:${task.status}:${task.progress}:${task.updated_at}`).join('|')
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

async function deleteTask(task: ImportTask) {
  if (!window.confirm(`确定删除“${task.document_title || `文档 #${task.document_id}`}”的导入任务记录吗？这不会删除原文档和已入库的知识。`)) return
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

watch(() => props.visible, (visible) => {
  if (visible) {
    void loadTasks()
    startPolling()
  } else {
    stopPolling()
  }
}, { immediate: true })

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
          <t-date-picker id="import-task-date" v-model="selectedDate" value-type="YYYY-MM-DD" format="YYYY-MM-DD" :clearable="false" />
        </div>
        <t-select v-model="selectedStatus" :options="statusOptions" class="queue-status-filter" />
        <t-button variant="outline" :loading="loading" @click="loadTasks">刷新</t-button>
      </div>

      <div class="queue-summary" aria-label="当天导入任务统计">
        <div><strong>{{ taskSummary.total }}</strong><span>当天任务</span></div>
        <div><strong>{{ taskSummary.active }}</strong><span>进行中</span></div>
        <div><strong :class="{ danger: taskSummary.failed > 0 }">{{ taskSummary.failed }}</strong><span>失败待处理</span></div>
        <p>展示任务提交日期；处理中的任务每 5 秒自动刷新。</p>
      </div>

      <div v-if="loading && !tasks.length" class="queue-state">正在加载导入任务...</div>
      <div v-else-if="!tasks.length" class="queue-state">{{ selectedDate }} 暂无导入任务</div>
      <div v-else class="task-list">
        <article v-for="task in tasks" :key="task.id" class="task-card">
          <div class="task-card-header">
            <div class="task-title">
              <strong :title="task.document_title || `文档 #${task.document_id}`">{{ task.document_title || `文档 #${task.document_id}` }}</strong>
              <span>{{ task.library_name || `知识库 #${task.library_id}` }}</span>
            </div>
            <t-tag :theme="getTaskTheme(task.status)" variant="light">{{ taskStatusLabel(task.status) }}</t-tag>
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
              <t-button v-if="task.chunk_count" variant="text" size="small" @click="emit('open-document', task.document_id)">查看 Chunk</t-button>
              <t-button v-if="task.status === TASK_STATUS.FAILED" variant="text" size="small" :loading="activeTaskId === task.id" @click="retryTask(task)">重试</t-button>
              <t-button v-if="task.status === 'queued' || task.status === TASK_STATUS.PROCESSING" variant="text" theme="danger" size="small" :loading="activeTaskId === task.id" @click="cancelTask(task)">取消</t-button>
              <t-button v-if="canDelete(task)" variant="text" theme="danger" size="small" :loading="activeTaskId === task.id" @click="deleteTask(task)">删除记录</t-button>
            </div>
          </div>
        </article>
      </div>
    </div>
  </t-dialog>
</template>

<style scoped>
.import-queue { display: grid; gap: 16px; min-height: 420px; }
.queue-toolbar { display: flex; align-items: end; gap: 10px; padding-bottom: 14px; border-bottom: 1px solid #e2e8f0; }
.queue-date-filter { display: grid; gap: 6px; }
.queue-date-filter label { color: #64748b; font-size: 12px; font-weight: 600; }
.queue-status-filter { width: 130px; }
.queue-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 110px)) minmax(0, 1fr); gap: 10px; align-items: stretch; }
.queue-summary > div { display: grid; align-content: center; gap: 2px; min-height: 68px; padding: 10px 12px; border: 1px solid #dbeafe; border-radius: 8px; background: #f8fbff; }
.queue-summary strong { color: #1d4ed8; font-size: 22px; line-height: 1; }
.queue-summary strong.danger { color: #dc2626; }
.queue-summary span { color: #64748b; font-size: 11px; }
.queue-summary p { align-self: center; margin: 0; color: #94a3b8; font-size: 12px; line-height: 20px; }
.queue-state { display: grid; min-height: 250px; place-items: center; color: #94a3b8; font-size: 13px; }
.task-list { display: grid; gap: 10px; max-height: 510px; overflow: auto; padding-right: 4px; }
.task-card { display: grid; gap: 11px; padding: 15px 16px; border: 1px solid #e2e8f0; border-radius: 8px; background: #fff; transition: border-color .2s, box-shadow .2s; }
.task-card:hover { border-color: #bfdbfe; box-shadow: 0 5px 16px rgb(37 99 235 / 7%); }
.task-card-header, .task-card-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.task-title { display: grid; min-width: 0; gap: 4px; }
.task-title strong { overflow: hidden; color: #1e293b; font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }
.task-title span, .task-step { color: #64748b; font-size: 12px; }
.task-meta { display: flex; flex-wrap: wrap; gap: 6px 16px; color: #94a3b8; font-size: 11px; }
.task-error { margin: 0; padding: 8px 10px; color: #b91c1c; background: #fef2f2; border-radius: 5px; font-size: 12px; line-height: 18px; white-space: pre-wrap; word-break: break-word; }
.task-actions { display: flex; align-items: center; gap: 4px; }
@media (max-width: 700px) {
  .queue-toolbar { align-items: stretch; flex-wrap: wrap; }
  .queue-date-filter { flex: 1; }
  .queue-date-filter :deep(.t-date-picker) { width: 100%; }
  .queue-status-filter { width: calc(50% - 5px); }
  .queue-summary { grid-template-columns: repeat(3, 1fr); }
  .queue-summary p { grid-column: 1 / -1; }
  .task-card-header, .task-card-footer { align-items: flex-start; flex-direction: column; }
  .task-actions { width: 100%; justify-content: flex-end; }
}
</style>
