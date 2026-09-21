<!-- 管理端操作日志页面。 -->
<!--
 * @FilePath: @/pages/admin/sessions/index.vue
 * @Author: 项目维护者
 * @Date: 2026-09-02
 * @Description: 管理端操作日志查询页面，支持按资源和操作类型筛选。
 * @BusinessRule: 日志资源优先展示业务名称，不暴露仅对数据库排查有意义的内部编号。
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { RefreshIcon } from 'tdesign-icons-vue-next'
import { mysqlApi, type OperationLog } from '../../../api/mysql'
import { formatTime } from '../shared/formatters'
import { PAGINATION } from '../../../constants'
import { usePagination } from '../../../composables'

type LogParams = {
  resourceType?: string
  operation?: string
}

const resourceType = ref('')
const operation = ref('')
const pagination = usePagination<OperationLog, LogParams>(async ({ page, pageSize, resourceType: selectedResource, operation: selectedOperation }) => {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  if (selectedResource) query.set('resource_type', selectedResource)
  if (selectedOperation) query.set('operation', selectedOperation)
  const result = await mysqlApi.listLogs(query.toString())
  return { data: result.items, total: result.total }
}, { initialPageSize: PAGINATION.PAGE_SIZE, immediate: true })
const logs = pagination.data
const total = pagination.total
const page = pagination.currentPage
const pageSize = pagination.pageSize
const loading = pagination.loading
const errorMessage = computed(() => pagination.error.value?.message || '')
const resourceOptions = [
  { label: '全部资源', value: '' },
  { label: '认证会话', value: 'auth_session' },
  { label: '用户', value: 'user' },
  { label: '角色', value: 'role' },
  { label: '权限', value: 'permission' },
  { label: '知识库', value: 'library' },
  { label: '文档', value: 'document' },
  { label: '标签', value: 'tag' },
  { label: '导入任务', value: 'import_task' },
]

const operationOptions = [
  { label: '全部操作', value: '' },
  { label: '登录', value: 'login' },
  { label: '退出登录', value: 'logout' },
  { label: '创建', value: 'create' },
  { label: '更新', value: 'update' },
  { label: '删除', value: 'delete' },
  { label: '恢复', value: 'restore' },
  { label: '启用', value: 'enable' },
  { label: '停用', value: 'disable' },
  { label: '上传', value: 'upload' },
  { label: '入队', value: 'queue' },
  { label: '重试', value: 'retry' },
  { label: '取消', value: 'cancel' },
]

const columns = [
  { colKey: 'operation', title: '操作', width: 120 },
  { colKey: 'resource', title: '资源', width: 170 },
  { colKey: 'actor', title: '操作者', width: 110 },
  { colKey: 'ip', title: 'IP 地址', width: 140 },
  { colKey: 'detail', title: '变更摘要' },
  { colKey: 'time', title: '时间', width: 180 },
]

function operationLabel(value: string) {
  return ({ login: '登录', logout: '退出登录', create: '创建', update: '更新', delete: '删除', restore: '恢复', enable: '启用', disable: '停用', upload: '上传', queue: '入队', retry: '重试', cancel: '取消', update_tags: '更新标签', add_member: '添加成员', remove_member: '移除成员' } as Record<string, string>)[value] || value
}

function resourceLabel(value: string | null) {
  return ({ auth_session: '认证会话', user: '用户', role: '角色', permission: '权限', library: '知识库', document: '文档', tag: '标签', import_task: '导入任务', library_member: '知识库成员', system_setting: '系统设置' } as Record<string, string>)[value || ''] || value || '-'
}

function resourceText(row: OperationLog) {
  const type = resourceLabel(row.resource_type)
  const name = row.resource_display_name
  return name ? type + '：' + name : type
}
/** 将结构化变更信息转换为表格中的简短摘要。 */
function detailText(detail: Record<string, unknown> | null) {
  if (!detail || !Object.keys(detail).length) return '-'
  return Object.entries(detail).map(([key, value]) => key + ': ' + String(value)).join('；')
}

async function loadLogs() {
  try {
    await pagination.fetchData({
      resourceType: resourceType.value,
      operation: operation.value,
    })
  } catch {
    // Error is exposed through the pagination composable for the template.
  }
}

function search() {
  page.value = 1
  void loadLogs()
}

function changePageInfo(pageInfo: { current: number; pageSize: number }) {
  if (pageInfo.pageSize !== pageSize.value) {
    void pagination.onPageSizeChange(pageInfo.pageSize)
    return
  }
  void pagination.onPageChange(pageInfo.current)
}
</script>

<template>
  <section class="admin-module">
    <div class="admin-page-head compact">
      <div>
        <div class="eyebrow">AUDIT LOGS</div>
        <h1>操作日志</h1>
        <p>追踪后台关键操作、操作者和资源变更摘要。</p>
      </div>
      <t-button variant="outline" :loading="loading" @click="loadLogs">
        <RefreshIcon />
        刷新
      </t-button>
    </div>

    <div class="toolbar log-toolbar">
      <t-select v-model="resourceType" :options="resourceOptions" placeholder="资源类型" class="log-filter" @change="search" />
      <t-select v-model="operation" :options="operationOptions" placeholder="操作类型" class="log-filter" @change="search" />
    </div>

    <div v-if="errorMessage" class="admin-error">
      {{ errorMessage }}
      <button @click="loadLogs">重试</button>
    </div>
    <div v-if="loading" class="admin-loading">正在加载操作日志...</div>

    <div class="panel table-panel">
      <t-table :data="logs" :columns="columns" row-key="id" :loading="loading" :pagination="{ current: page, pageSize, total, pageSizeOptions: [...PAGINATION.PAGE_SIZES] }" @page-change="changePageInfo">
        <template #operation="{ row }">
          <t-tag variant="light" theme="primary">{{ operationLabel(row.operation) }}</t-tag>
        </template>
        <template #resource="{ row }">
          <span>{{ resourceText(row) }}</span>
        </template>
        <template #actor="{ row }">{{ row.user_display_name || (row.user_id ? '用户 #' + row.user_id : '-') }}</template>
        <template #ip="{ row }">{{ row.request_ip || '-' }}</template>
        <template #detail="{ row }"><span class="log-detail">{{ detailText(row.detail) }}</span></template>
        <template #time="{ row }">{{ formatTime(row.created_at, true) }}</template>
        <template #totalContent><span class="table-pagination-total">共 {{ total }} 条数据</span></template>
      </t-table>
    </div>
  </section>
</template>

<style scoped>
.log-toolbar { justify-content: flex-start; }
.log-filter { width: 180px; }
.log-detail { color: #6b7280; }

</style>
