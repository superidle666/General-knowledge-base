<!--
 * @FilePath: @/pages/admin/roles/index.vue
 * @Author: 项目维护者
 * @Date: 2026-09-02
 * @Description: 角色管理页面，支持角色维护、软删除恢复与权限分配。
 * @BusinessRule: 管理权限勾选时同步子权限；系统管理员角色不可在页面内修改或删除。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { AddIcon } from 'tdesign-icons-vue-next'
import { mysqlApi, type Permission, type Role } from '../../../api/mysql'
import { PAGINATION } from '../../../constants'
import { isSystemAdminRole } from '../shared/formatters'

type RoleForm = {
  code: string
  name: string
  description: string
  permission_codes: string[]
  status: 'active' | 'disabled'
}

type PermissionGroup = {
  parent: Permission
  children: Permission[]
}

const emit = defineEmits<{
  (event: 'update-counts', counts: { roles: number; permissions: number }): void
}>()

const roles = ref<Role[]>([])
const permissions = ref<Permission[]>([])
const loading = ref(false)
const showDeleted = ref(false)
const deletingRoleId = ref<number>()
const errorMessage = ref('')
const createError = ref('')
const editError = ref('')
const creatingRole = ref(false)
const savingRole = ref(false)
const showCreateDialog = ref(false)
const showEditDialog = ref(false)
const editingRoleId = ref<number>()
const newRole = ref<RoleForm>(createNewRole())
const editRole = ref<Omit<RoleForm, 'code'>>(createEditRole())
const tablePage = ref(1)
const tablePageSize = ref<number>(PAGINATION.PAGE_SIZE)

const columns = [
  { colKey: 'name', title: '角色名称' },
  { colKey: 'code', title: '编码', width: 160 },
  { colKey: 'description', title: '描述' },
  { colKey: 'permissions', title: '权限数量', width: 120, align: 'center' },
  { colKey: 'status', title: '状态', width: 100, align: 'center' },
  { colKey: 'op', title: '操作', width: 220, align: 'center' },
]

const permissionGroups = computed<PermissionGroup[]>(() =>
  permissions.value
    .filter((permission) => permission.parent_id === null)
    .map((parent) => ({
      parent,
      children: permissions.value.filter((permission) => permission.parent_id === parent.id),
    })),
)

const tablePagination = computed(() => ({
  current: tablePage.value,
  pageSize: tablePageSize.value,
  total: roles.value.length,
  pageSizeOptions: [...PAGINATION.PAGE_SIZES],
}))

function handlePageChange(pageInfo: { current: number; pageSize: number }) {
  tablePage.value = pageInfo.current
  tablePageSize.value = pageInfo.pageSize
}

function createNewRole(): RoleForm {
  return { code: '', name: '', description: '', permission_codes: [], status: 'active' }
}

function createEditRole(): Omit<RoleForm, 'code'> {
  return { name: '', description: '', permission_codes: [], status: 'active' }
}

function openCreateDialog() {
  newRole.value = createNewRole()
  createError.value = ''
  showCreateDialog.value = true
}

function validateNewRole() {
  const code = newRole.value.code.trim()
  if (!code) return '请输入角色编码。'
  if (!/^[a-z][a-z0-9_-]{1,63}$/.test(code)) {
    return '角色编码需以小写字母开头，仅可包含小写字母、数字、下划线或连字符。'
  }
  return newRole.value.name.trim() ? '' : '请输入角色名称。'
}

function validateEditRole() {
  return editRole.value.name.trim() ? '' : '请输入角色名称。'
}

function getStatusTheme(row: Role) {
  if (row.deleted_at) return 'danger'
  return row.status === 'active' ? 'success' : 'default'
}

function statusLabel(row: Role) {
  if (row.deleted_at) return '已删除'
  return row.status === 'active' ? '已启用' : '已停用'
}

function parentChecked(form: { permission_codes: string[] }, group: PermissionGroup) {
  return [group.parent, ...group.children].every((permission) => form.permission_codes.includes(permission.code))
}

function parentIndeterminate(form: { permission_codes: string[] }, group: PermissionGroup) {
  const selected = [group.parent, ...group.children]
    .filter((permission) => form.permission_codes.includes(permission.code)).length
  return selected > 0 && selected < group.children.length + 1
}

function childChecked(form: { permission_codes: string[] }, child: Permission) {
  return form.permission_codes.includes(child.code)
}

// NOTE: 父级“管理权限”代表整个模块，选中或取消时必须同步全部子权限。
function toggleParent(form: { permission_codes: string[] }, group: PermissionGroup, checked: unknown) {
  const groupCodes = [group.parent, ...group.children].map((permission) => permission.code)
  const selected = new Set(form.permission_codes)
  if (checked === true) groupCodes.forEach((code) => selected.add(code))
  else groupCodes.forEach((code) => selected.delete(code))
  form.permission_codes = [...selected]
}

function toggleChild(form: { permission_codes: string[] }, group: PermissionGroup, child: Permission, checked: unknown) {
  const selected = new Set(form.permission_codes)
  if (checked === true) selected.add(child.code)
  else selected.delete(child.code)

  const allChildrenSelected = group.children.every((item) => selected.has(item.code))
  if (allChildrenSelected) selected.add(group.parent.code)
  else selected.delete(group.parent.code)
  form.permission_codes = [...selected]
}

// NOTE: 系统管理员角色必须保持可用，避免后台失去唯一管理入口。
async function toggleRoleStatus(row: Role) {
  if (isSystemAdminRole(row.code) || savingRole.value) return
  savingRole.value = true
  try {
    await mysqlApi.updateRole(row.id, { status: row.status === 'active' ? 'disabled' : 'active' })
    await loadRoles()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '角色状态更新失败'
  } finally {
    savingRole.value = false
  }
}

async function deleteRole(row: Role) {
  if (isSystemAdminRole(row.code) || deletingRoleId.value) return
  if (!window.confirm('确定删除该角色吗？')) return
  deletingRoleId.value = row.id
  try {
    await mysqlApi.deleteRole(row.id)
    await loadRoles()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '角色删除失败'
  } finally {
    deletingRoleId.value = undefined
  }
}

async function restoreRole(row: Role) {
  if (deletingRoleId.value) return
  deletingRoleId.value = row.id
  try {
    await mysqlApi.restoreRole(row.id)
    await loadRoles()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '角色恢复失败'
  } finally {
    deletingRoleId.value = undefined
  }
}

async function loadRoles() {
  loading.value = true
  errorMessage.value = ''
  try {
    const [roleData, permissionData] = await Promise.all([
      mysqlApi.listRoles(showDeleted.value ? 'include_deleted=true' : ''),
      mysqlApi.listPermissions(),
    ])
    roles.value = roleData.sort((left, right) => Number(isSystemAdminRole(right.code)) - Number(isSystemAdminRole(left.code)))
    permissions.value = permissionData
    emit('update-counts', { roles: roles.value.length, permissions: permissions.value.length })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '角色数据加载失败'
  } finally {
    loading.value = false
  }
}

async function createRole() {
  createError.value = validateNewRole()
  if (createError.value) return
  creatingRole.value = true
  try {
    await mysqlApi.createRole({
      ...newRole.value,
      code: newRole.value.code.trim(),
      name: newRole.value.name.trim(),
      description: newRole.value.description.trim() || null,
    })
    showCreateDialog.value = false
    await loadRoles()
  } catch (error) {
    createError.value = error instanceof Error ? error.message : '角色创建失败'
  } finally {
    creatingRole.value = false
  }
}

function openEditDialog(row: Role) {
  if (isSystemAdminRole(row.code)) return
  editingRoleId.value = row.id
  editError.value = ''
  editRole.value = {
    name: row.name,
    description: row.description || '',
    permission_codes: [...row.permissions],
    status: row.status,
  }
  showEditDialog.value = true
}

async function saveRole() {
  if (!editingRoleId.value) return
  editError.value = validateEditRole()
  if (editError.value) return
  savingRole.value = true
  try {
    await mysqlApi.updateRole(editingRoleId.value, {
      ...editRole.value,
      name: editRole.value.name.trim(),
      description: editRole.value.description.trim() || null,
    })
    showEditDialog.value = false
    await loadRoles()
  } catch (error) {
    editError.value = error instanceof Error ? error.message : '角色更新失败'
  } finally {
    savingRole.value = false
  }
}

onMounted(() => {
  void loadRoles()
})
</script>

<template>
  <section class="admin-module">
    <div class="admin-page-head compact">
      <div>
        <div class="eyebrow">ACCESS CONTROL</div>
        <h1>角色管理</h1>
        <p>配置角色名称及其可用权限。</p>
      </div>
      <div class="admin-page-actions">
        <t-button theme="primary" class="create-action" @click="openCreateDialog">
          <AddIcon />
          新增角色
        </t-button>
      </div>
    </div>

    <div class="table-filter-row">
      <t-checkbox v-model="showDeleted" @change="tablePage = 1; loadRoles()">显示已删除</t-checkbox>
    </div>

    <div v-if="errorMessage" class="admin-error">
      {{ errorMessage }}
      <button @click="loadRoles">重试</button>
    </div>
    <div v-if="loading" class="admin-loading">正在加载角色数据...</div>

    <div class="panel table-panel">
      <t-table :data="roles" :columns="columns" row-key="id" :pagination="tablePagination" @page-change="handlePageChange">
        <template #name="{ row }">
          <div class="role-name">
            <div class="role-symbol">{{ row.name.slice(0, 1) }}</div>
            <strong>{{ row.name }}</strong>
          </div>
        </template>
        <template #permissions="{ row }">
          <t-tag theme="primary" variant="light">{{ row.permissions.length }} 项权限</t-tag>
        </template>
        <template #status="{ row }">
          <t-tag :theme="getStatusTheme(row)" variant="light">
            {{ statusLabel(row) }}
          </t-tag>
        </template>
        <template #op="{ row }">
          <div v-if="!isSystemAdminRole(row.code)" class="table-actions table-actions--multiple">
            <t-button variant="text" class="edit-action" size="small" @click="openEditDialog(row)">编辑</t-button>
            <t-button
              variant="text"
              :theme="row.status === 'active' ? 'danger' : 'success'"
              size="small"
              :loading="savingRole"
              @click="toggleRoleStatus(row)"
            >
              {{ row.status === 'active' ? '停用' : '启用' }}
            </t-button>
            <t-button
              v-if="!row.deleted_at"
              variant="text"
              theme="danger"
              size="small"
              :loading="deletingRoleId === row.id"
              @click="deleteRole(row)"
            >删除</t-button>
            <t-button
              v-else
              variant="text"
              theme="success"
              size="small"
              :loading="deletingRoleId === row.id"
              @click="restoreRole(row)"
            >恢复</t-button>
          </div>
          <span v-else class="protected-label">系统内置</span>
        </template>
              <template #totalContent><span class="table-pagination-total">共 {{ roles.length }} 条数据</span></template>
</t-table>
    </div>

    <t-dialog
      v-model:visible="showCreateDialog"
      header="新增角色"
      :confirm-btn="{ content: '创建角色', theme: 'primary', loading: creatingRole }"
      @confirm="createRole"
    >
      <div class="dialog-fields">
        <p v-if="createError" class="dialog-form-error">{{ createError }}</p>
        <t-input v-model="newRole.code" label="角色编码" placeholder="例如：reviewer" />
        <t-input v-model="newRole.name" label="角色名称" placeholder="例如：审核员" />
        <t-input v-model="newRole.description" label="角色描述" placeholder="可选" />
        <div class="permission-picker">
          <label class="permission-picker__label">权限</label>
          <div v-for="group in permissionGroups" :key="group.parent.id" class="permission-group">
            <t-checkbox
              :checked="parentChecked(newRole, group)"
              :indeterminate="parentIndeterminate(newRole, group)"
              @change="toggleParent(newRole, group, $event)"
            >{{ group.parent.name }}</t-checkbox>
            <div class="permission-group__children">
              <t-checkbox
                v-for="child in group.children"
                :key="child.id"
                :checked="childChecked(newRole, child)"
                @change="toggleChild(newRole, group, child, $event)"
              >{{ child.name }}</t-checkbox>
            </div>
          </div>
        </div>
      </div>
    </t-dialog>

    <t-dialog
      v-model:visible="showEditDialog"
      header="编辑角色"
      :confirm-btn="{ content: '保存', theme: 'primary', loading: savingRole }"
      @confirm="saveRole"
    >
      <div class="dialog-fields">
        <p v-if="editError" class="dialog-form-error">{{ editError }}</p>
        <t-input v-model="editRole.name" label="角色名称" />
        <t-input v-model="editRole.description" label="角色描述" />
        <t-select
          v-model="editRole.status"
          :options="[{ label: '已启用', value: 'active' }, { label: '已停用', value: 'disabled' }]"
          label="状态"
        />
        <div class="permission-picker">
          <label class="permission-picker__label">权限</label>
          <div v-for="group in permissionGroups" :key="group.parent.id" class="permission-group">
            <t-checkbox
              :checked="parentChecked(editRole, group)"
              :indeterminate="parentIndeterminate(editRole, group)"
              @change="toggleParent(editRole, group, $event)"
            >{{ group.parent.name }}</t-checkbox>
            <div class="permission-group__children">
              <t-checkbox
                v-for="child in group.children"
                :key="child.id"
                :checked="childChecked(editRole, child)"
                @change="toggleChild(editRole, group, child, $event)"
              >{{ child.name }}</t-checkbox>
            </div>
          </div>
        </div>
      </div>
    </t-dialog>
  </section>
</template>

<style scoped>
.table-filter-row {
  margin: 12px 0;
}

.permission-picker {
  border: 1px solid var(--td-component-border);
  max-height: 300px;
  overflow: auto;
  padding: 12px;
}

.permission-picker__label {
  display: block;
  margin-bottom: 8px;
  color: var(--td-text-color-secondary);
}

.permission-group + .permission-group {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--td-component-stroke);
}

.permission-group__children {
  display: grid;
  gap: 8px;
  padding: 8px 0 0 28px;
}

.role-name {
  display: flex;
  align-items: center;
  gap: 9px;
}

.role-symbol {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border-radius: 5px;
  color: #2563eb;
  background: #eff6ff;
  font-size: 11px;
  font-weight: 600;
}
.protected-label { display: inline-flex; align-items: center; min-height: 28px; color: #9ca3af; font-size: 11px; font-weight: 400; line-height: 1.4; white-space: nowrap; }
</style>
