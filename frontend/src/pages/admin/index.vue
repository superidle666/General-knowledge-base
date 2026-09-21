<!--
 * @FilePath: @/pages/admin/index.vue
 * @Author: 项目维护者
 * @Date: 2026-09-02
 * @Description: 管理端入口，负责登录、会话恢复与后台模块切换。
 * @BusinessRule: 系统设置仅对 admin 角色可见；“记住我”仅控制浏览器登录会话持久化。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import {
  ChatIcon,
  DashboardIcon,
  FileIcon,
  SettingIcon,
  UsergroupIcon,
  UserIcon,
  LockOnIcon,
} from 'tdesign-icons-vue-next'
import AdminHeader from '../../components/admin/AdminHeader.vue'
import AdminSidebar from '../../components/admin/AdminSidebar.vue'
import AdminLayout from '../../layouts/AdminLayout.vue'
import DashboardPage from './dashboard/index.vue'
import DocumentsPage from './documents/index.vue'
import LibrariesPage from './libraries/index.vue'
import PermissionsPage from './permissions/index.vue'
import RolesPage from './roles/index.vue'
import SessionsPage from './sessions/index.vue'
import SettingsPage from './settings/index.vue'
import UsersPage from './users/index.vue'
import { USER_ROLE } from '../../constants'
import { useUserStore } from '../../store/modules/user'

type NavKey = 'dashboard' | 'libraries' | 'documents' | 'users' | 'roles' | 'permissions' | 'sessions' | 'settings'

type AdminCounts = {
  documents: number
  users: number
  roles: number
  permissions: number
}

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const { userInfo: account, isLoggedIn: isAuthenticated } = storeToRefs(userStore)
const username = ref('')
const password = ref('')
const loginError = ref('')
const loginLoading = ref(false)
const rememberLogin = ref(true)
const activeNav = ref<NavKey>('dashboard')
const selectedLibraryId = ref<number>()
const documentFilterLibraryId = ref<number>()
const uploadLibraryId = ref<number>()
const uploadRequestId = ref(0)
const collapsed = ref(false)
const mobileOpen = ref(false)
const userMenuExpanded = ref(true)
const darkMode = ref(false)
const ADMIN_DARK_MODE_KEY = 'kb-admin-dark-mode'
const counts = ref<AdminCounts>({ documents: 0, users: 0, roles: 0, permissions: 0 })

const allNavItems = [
  { key: 'dashboard' as const, label: '仪表盘', icon: DashboardIcon },
  { key: 'libraries' as const, label: '知识库管理', icon: FileIcon },
  { key: 'documents' as const, label: '文档管理', icon: FileIcon },
  { key: 'users' as const, label: '用户管理', icon: UsergroupIcon },
  { key: 'sessions' as const, label: '操作日志', icon: ChatIcon },
  { key: 'settings' as const, label: '系统设置', icon: SettingIcon },
]

const navItems = computed(() => allNavItems.filter((item) => item.key !== 'settings' || account.value?.roles.includes(USER_ROLE.ADMIN)))

const pageTitle = computed(() => navItems.value.find((item) => item.key === activeNav.value)?.label ?? '仪表盘')
const roleLabel = computed(() => account.value?.roles.includes(USER_ROLE.ADMIN) ? '系统管理员' : '已授权用户')

async function loadCurrentUser() {
  try {
    await userStore.fetchUserInfo()
  } catch {
    userStore.clearToken()
    await router.replace({ name: 'admin-login' })
  }
}

async function login() {
  if (loginLoading.value) return
  loginError.value = ''
  if (!username.value.trim() || !password.value) {
    loginError.value = '请输入账号和密码'
    return
  }
  loginLoading.value = true
  try {
    await userStore.login(username.value.trim(), password.value, rememberLogin.value)
    password.value = ''
    await router.replace({ name: 'admin' })
  } catch (error) {
    loginError.value = error instanceof Error ? error.message : '登录失败'
  } finally {
    loginLoading.value = false
  }
}

/**
 * 退出当前管理会话并恢复已选择保存的本机凭据。
 *
 * Note:
 *     登录会话与“记住我”凭据是两类状态，退出不能意外清除用户的登录偏好。
 */
async function logout() {
  await userStore.logout()
  loginError.value = ''
  username.value = ''
  password.value = ''
  rememberLogin.value = true
  await router.replace({ name: 'admin-login' })
}

function handleExpired() {
  userStore.clearToken()
  userStore.setUserInfo(null)
  void router.replace({ name: 'admin-login' })
}

function navigate(key: NavKey, libraryId?: number) {
  activeNav.value = key
  if (key === 'libraries') selectedLibraryId.value = libraryId || undefined
  if (key === 'documents' && !libraryId) {
    documentFilterLibraryId.value = undefined
    uploadLibraryId.value = undefined
  }
  mobileOpen.value = false
}

function viewLibraryDocuments(libraryId: number) {
  documentFilterLibraryId.value = libraryId
  uploadLibraryId.value = undefined
  activeNav.value = 'documents'
  mobileOpen.value = false
}

function importLibraryDocuments(libraryId: number) {
  documentFilterLibraryId.value = undefined
  uploadLibraryId.value = libraryId
  uploadRequestId.value += 1
  activeNav.value = 'documents'
  mobileOpen.value = false
}

function updateCounts(nextCounts: Partial<AdminCounts>) {
  counts.value = { ...counts.value, ...nextCounts }
}

function toggleAdminTheme() {
  darkMode.value = !darkMode.value
  window.localStorage.setItem(ADMIN_DARK_MODE_KEY, String(darkMode.value))
}

onMounted(() => {
  window.addEventListener('auth-expired', handleExpired)
  darkMode.value = window.localStorage.getItem(ADMIN_DARK_MODE_KEY) === 'true'
  if (route.name === 'admin' && isAuthenticated.value) void loadCurrentUser()
})

onBeforeUnmount(() => {
  window.removeEventListener('auth-expired', handleExpired)
})
</script>

<template>
  <div v-if="route.name === 'admin-login' || !isAuthenticated" class="admin-login-page">
    <div class="login-brand">
      <div class="admin-mark">工</div>
      <div>
        <strong>工智库</strong>
        <span>管理控制台</span>
      </div>
    </div>

    <div class="login-card">
      <div class="eyebrow">ADMIN CONSOLE</div>
      <h1>登录管理端</h1>
      <p class="login-subtitle">使用已授权账号登录后管理知识库内容、用户与权限。</p>

      <t-form class="login-form" autocomplete="off" @submit="login">
        <div class="login-field">
          <t-input
            v-model="username"
            name="admin-login-account"
            autocomplete="off"
            placeholder="账号"
            clearable
            autofocus
          >
            <template #prefix-icon><UserIcon /></template>
          </t-input>
        </div>
        <div class="login-field">
          <t-input
            v-model="password"
            name="password"
            type="password"
            autocomplete="new-password"
            placeholder="登录密码"
            clearable
            @enter="login"
          >
            <template #prefix-icon><LockOnIcon /></template>
          </t-input>
        </div>
        <div class="login-options">
          <t-checkbox v-model="rememberLogin">记住我</t-checkbox>
        </div>
        <div v-if="loginError" class="login-error" role="alert">{{ loginError }}</div>
        <t-button theme="primary" block size="large" type="submit" :loading="loginLoading">
          登录管理端
        </t-button>
      </t-form>
    </div>

    <p class="login-footer">工智库 · 设备技术知识平台</p>
  </div>

  <AdminLayout v-else :dark-mode="darkMode">
    <AdminSidebar
      :collapsed="collapsed"
      :mobile-open="mobileOpen"
      :active-nav="activeNav"
      :user-menu-expanded="userMenuExpanded"
      :nav-items="navItems"
      :user-count="counts.users"
      :role-count="counts.roles"
      :permission-count="counts.permissions"
      :display-name="account?.display_name || account?.username || '已登录用户'"
      :role-label="roleLabel"
      @navigate="navigate"
      @toggle-users="userMenuExpanded = !userMenuExpanded"
      @update:mobile-open="mobileOpen = $event"
    />
    <div v-if="mobileOpen" class="admin-backdrop" @click="mobileOpen = false" />

    <main class="admin-main">
      <AdminHeader
        :title="pageTitle"
        :display-name="account?.display_name || account?.username || '已登录用户'"
        :role-label="roleLabel"
        :dark-mode="darkMode"
        @open-menu="mobileOpen = true"
        @logout="logout"
        @toggle-theme="toggleAdminTheme"
      />
      <div class="admin-content">
        <DashboardPage v-if="activeNav === 'dashboard'" @navigate="navigate" @update-counts="updateCounts" />
        <LibrariesPage v-else-if="activeNav === 'libraries'" :selected-library-id="selectedLibraryId" @update-counts="updateCounts" @view-documents="viewLibraryDocuments" @import-documents="importLibraryDocuments" />
        <DocumentsPage v-else-if="activeNav === 'documents'" :filter-library-id="documentFilterLibraryId" :upload-library-id="uploadLibraryId" :upload-request-id="uploadRequestId" @navigate="navigate" @update-counts="updateCounts" />
        <UsersPage v-else-if="activeNav === 'users'" @update-counts="updateCounts" />
        <RolesPage v-else-if="activeNav === 'roles'" @update-counts="updateCounts" />
        <PermissionsPage v-else-if="activeNav === 'permissions'" @update-counts="updateCounts" />
        <SessionsPage v-else-if="activeNav === 'sessions'" />
        <SettingsPage v-else />
      </div>
    </main>
  </AdminLayout>
</template>

<style scoped>
.admin-login-page { min-height: 100vh; display: grid; place-items: center; align-content: center; gap: 18px; padding: 24px; background: #f7f8fa; color: #1f2937; }
.login-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 5px; }
.login-brand > div:last-child { display: grid; gap: 2px; }
.login-brand strong { font-size: 17px; }
.login-brand span { color: #6b7280; font-size: 11px; }
.login-card { box-sizing: border-box; width: min(100%, 420px); background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 32px; box-shadow: 0 12px 35px rgb(15 23 42 / 7%); }
.login-card h1 { font-size: 24px; margin: 8px 0 6px; }
.login-subtitle { color: #6b7280; font-size: 13px; line-height: 21px; margin: 0 0 26px; }
.login-card .login-form { display: grid; gap: 16px; }
.login-field { min-width: 0; }
.login-field .t-input { box-sizing: border-box; width: 100%; min-width: 0; }
.login-field .t-input__inner { font-size: 14px; }
.login-field .t-input__prefix-icon { color: #8b95a7; }
.login-options { display: flex; align-items: center; min-height: 20px; }
.login-options .t-checkbox__label { color: #374151; font-size: 13px; }
.login-error { min-height: 38px; box-sizing: border-box; display: flex; align-items: center; color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; border-radius: 4px; padding: 8px 10px; font-size: 12px; line-height: 18px; }
.login-card .t-button { margin-top: 2px; }
.login-footer { color: #9ca3af; font-size: 11px; }
.admin-main { min-width: 0; flex: 1; display: flex; flex-direction: column; }
.admin-content { flex: 1; padding: 32px 36px 48px; overflow: auto; }
.admin-backdrop { display: none; }
@media (max-width: 1100px) { .admin-content { padding-left: 24px; padding-right: 24px; } }
@media (max-width: 760px) {
  .admin-content { padding: 24px 16px 40px; }
  .admin-backdrop { display: block; position: fixed; inset: 0; background: rgb(15 23 42 / 35%); z-index: 10; }
}
@media (max-width: 480px) { .login-card { padding: 24px 20px; } }
</style>

