<!--
 * @FilePath: @/pages/admin/permissions/index.vue
 * @Author: 项目维护者
 * @Date: 2026-09-02
 * @Description: 权限注册表展示与启停页面。
 * @BusinessRule: 权限编码由代码注册表维护，页面只允许调整启用状态，不能自由新增或删除。
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { CheckCircleIcon, ChevronDownIcon, ChevronRightIcon } from 'tdesign-icons-vue-next'
import { mysqlApi, type Permission } from '../../../api/mysql'
import { PAGINATION } from '../../../constants'
import { useRequest } from '../../../composables'
import { PERMISSION_STATUS_META, getStatusMeta, type StatusMeta } from '../shared/statusMeta'
import { useAdminCountsStore } from '../../../store/modules/adminCounts'

const countsStore = useAdminCountsStore()

const expandedParentIds = ref<number[]>([])
const savingPermissionId = ref<number>()
const tablePage = ref(1)
const tablePageSize = ref<number>(PAGINATION.PAGE_SIZE)

const parentPermissions = computed(() => permissions.value.filter((permission) => permission.parent_id === null))
// NOTE: 子权限默认收起，避免注册表较大时影响日常查找与角色分配。
const visiblePermissions = computed(() => {
  const childrenByParent = new Map<number, Permission[]>()
  for (const permission of permissions.value) {
    if (permission.parent_id !== null) {
      childrenByParent.set(permission.parent_id, [...(childrenByParent.get(permission.parent_id) || []), permission])
    }
  }
  return parentPermissions.value.flatMap((parent) => [
    parent,
    ...(expandedParentIds.value.includes(parent.id) ? childrenByParent.get(parent.id) || [] : []),
  ])
})

const tablePagination = computed(() => ({
  current: tablePage.value,
  pageSize: tablePageSize.value,
  total: visiblePermissions.value.length,
  pageSizeOptions: [...PAGINATION.PAGE_SIZES],
}))

function handlePageChange(pageInfo: { current: number; pageSize: number }) {
  tablePage.value = pageInfo.current
  tablePageSize.value = pageInfo.pageSize
}

const columns = [
  { colKey: 'name', title: '权限名称' },
  { colKey: 'code', title: '权限编码', width: 240 },
  { colKey: 'status', title: '状态', width: 120, align: 'center' },
  { colKey: 'op', title: '操作', width: 120, align: 'center' },
]

/** 加载权限注册表：重置展开状态与页码，并同步侧栏统计。 */
const permissionsRequest = useRequest(
  async () => {
    const data = await mysqlApi.listPermissions()
    expandedParentIds.value = []
    tablePage.value = 1
    countsStore.updateCounts({ permissions: data.length })
    return data
  },
  { immediate: true },
)
const permissions = computed(() => permissionsRequest.data.value ?? [])
const loading = permissionsRequest.loading
const mutationError = ref('')
const errorMessage = computed(() => mutationError.value || permissionsRequest.error.value?.message || '')

function isParent(permission: Permission) {
  return permission.parent_id === null
}

function childrenOf(parent: Permission) {
  return permissions.value.filter((permission) => permission.parent_id === parent.id)
}

/** 权限行状态元数据。 */
function permissionStatusMeta(row: Permission): StatusMeta {
  return getStatusMeta(PERMISSION_STATUS_META, row.status)
}

function toggleExpanded(permission: Permission) {
  if (!isParent(permission)) return
  expandedParentIds.value = expandedParentIds.value.includes(permission.id)
    ? expandedParentIds.value.filter((id) => id !== permission.id)
    : [...expandedParentIds.value, permission.id]
}

/** 启用或停用注册权限；后端会写入独立的启用或停用审计操作。 */
async function togglePermissionStatus(row: Permission) {
  if (savingPermissionId.value) return
  savingPermissionId.value = row.id
  try {
    await mysqlApi.updatePermission(row.id, { status: row.status === 'active' ? 'disabled' : 'active' })
    await loadPermissions()
  } catch (error) {
    mutationError.value = error instanceof Error ? error.message : '权限状态更新失败'
  } finally {
    savingPermissionId.value = undefined
  }
}

/** 重新加载权限注册表；加载错误由 errorMessage 统一展示。 */
async function loadPermissions() {
  mutationError.value = ''
  try {
    await permissionsRequest.execute()
  } catch {
    // 加载错误通过 permissionsRequest.error 在模板中展示
  }
}
</script>

<template>
  <section class="admin-module">
    <div class="admin-page-head compact">
      <div>
        <div class="eyebrow">ACCESS CONTROL</div>
        <h1>权限管理</h1>
        <p>系统权限由代码注册表维护，可在此查看层级并调整启用状态。</p>
      </div>
    </div>

    <div v-if="errorMessage" class="admin-error">
      {{ errorMessage }}
      <button @click="loadPermissions">重试</button>
    </div>
    <div v-if="loading" class="admin-loading">正在加载权限数据...</div>

    <div class="panel table-panel">
      <t-table
        :data="visiblePermissions"
        :columns="columns"
        row-key="id"
        :pagination="tablePagination"
        @page-change="handlePageChange"
      >
        <template #name="{ row }">
          <div class="permission-name" :class="{ 'permission-name--child': !isParent(row) }">
            <t-button
              v-if="isParent(row)"
              variant="text"
              shape="square"
              size="small"
              :aria-label="expandedParentIds.includes(row.id) ? '收起子权限' : '展开子权限'"
              @click="toggleExpanded(row)"
            >
              <ChevronDownIcon v-if="expandedParentIds.includes(row.id)" />
              <ChevronRightIcon v-else />
            </t-button>
            <span v-else class="permission-indent" />
            <CheckCircleIcon />
            <strong v-if="isParent(row)">{{ row.name }}</strong>
            <span v-else>{{ row.name }}</span>
            <t-tag v-if="isParent(row)" size="small" variant="light"> {{ childrenOf(row).length }} 项子权限 </t-tag>
          </div>
        </template>
        <template #code="{ row }">
          <code>{{ row.code }}</code>
        </template>
        <template #status="{ row }">
          <t-tag :theme="permissionStatusMeta(row).theme" variant="light">
            {{ permissionStatusMeta(row).label }}
          </t-tag>
        </template>
        <template #op="{ row }">
          <div class="table-actions">
            <t-button
              variant="text"
              :theme="row.status === 'active' ? 'danger' : 'success'"
              size="small"
              :loading="savingPermissionId === row.id"
              @click="togglePermissionStatus(row)"
            >
              {{ row.status === 'active' ? '停用' : '启用' }}
            </t-button>
          </div>
        </template>
        <template #totalContent
          ><span class="table-pagination-total">共 {{ visiblePermissions.length }} 条数据</span></template
        >
      </t-table>
    </div>
  </section>
</template>

<style scoped>
.permission-name {
  display: flex;
  align-items: center;
  gap: 8px;
}

.permission-name--child {
  padding-left: 32px;
}

.permission-indent {
  width: 24px;
}

.permission-name :deep(.t-button) {
  flex: 0 0 auto;
}
</style>
