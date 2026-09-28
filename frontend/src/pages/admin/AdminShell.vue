<!--
 * @FilePath: @/pages/admin/AdminShell.vue
 * @Author: 项目维护者
 * @Date: 2026-09-28
 * @Description: 管理端布局壳：侧栏、顶栏与会话维护，模块内容由子路由渲染。
 * @BusinessRule: 系统设置仅对 admin 角色可见；侧栏统计数由子页面写入 adminCounts store。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { ChatIcon, DashboardIcon, FileIcon, SettingIcon, UsergroupIcon } from 'tdesign-icons-vue-next'
import AdminHeader from '../../components/admin/AdminHeader.vue'
import AdminSidebar from '../../components/admin/AdminSidebar.vue'
import AdminLayout from '../../layouts/AdminLayout.vue'
import { STORAGE_KEY, USER_ROLE } from '../../constants'
import { useUserStore } from '../../store/modules/user'
import { useAdminCountsStore } from '../../store/modules/adminCounts'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const countsStore = useAdminCountsStore()
const { userInfo: account } = storeToRefs(userStore)
const { counts } = storeToRefs(countsStore)
const collapsed = ref(false)
const mobileOpen = ref(false)
const userMenuExpanded = ref(true)
const darkMode = ref(false)
const ADMIN_DARK_MODE_KEY = STORAGE_KEY.ADMIN_DARK_MODE

const allNavItems = [
  { key: 'dashboard' as const, label: '仪表盘', icon: DashboardIcon },
  { key: 'libraries' as const, label: '知识库管理', icon: FileIcon },
  { key: 'documents' as const, label: '文档管理', icon: FileIcon },
  { key: 'users' as const, label: '用户管理', icon: UsergroupIcon },
  { key: 'sessions' as const, label: '操作日志', icon: ChatIcon },
  { key: 'settings' as const, label: '系统设置', icon: SettingIcon },
]

const navItems = computed(() =>
  allNavItems.filter((item) => item.key !== 'settings' || account.value?.roles.includes(USER_ROLE.ADMIN)),
)

const pageTitle = computed(() => (route.meta.title as string | undefined) ?? '仪表盘')
const roleLabel = computed(() => (account.value?.roles.includes(USER_ROLE.ADMIN) ? '系统管理员' : '已授权用户'))

async function loadCurrentUser() {
  try {
    await userStore.fetchUserInfo()
  } catch {
    userStore.clearSession()
    await router.replace({ name: 'admin-login' })
  }
}

/**
 * 退出当前管理会话并恢复已选择保存的本机凭据。
 *
 * Note:
 *     登录会话与"记住我"凭据是两类状态，退出不能意外清除用户的登录偏好。
 */
async function logout() {
  await userStore.logout()
  await router.replace({ name: 'admin-login' })
}

function handleExpired() {
  // clearSession 同时重置令牌与用户信息，auth-expired 后登录态只此一个入口清理
  userStore.clearSession()
  void router.replace({ name: 'admin-login' })
}

function toggleAdminTheme() {
  darkMode.value = !darkMode.value
  window.localStorage.setItem(ADMIN_DARK_MODE_KEY, String(darkMode.value))
}

onMounted(() => {
  window.addEventListener('auth-expired', handleExpired)
  darkMode.value = window.localStorage.getItem(ADMIN_DARK_MODE_KEY) === 'true'
  void loadCurrentUser()
})

onBeforeUnmount(() => {
  window.removeEventListener('auth-expired', handleExpired)
})
</script>

<template>
  <AdminLayout :dark-mode="darkMode">
    <AdminSidebar
      :collapsed="collapsed"
      :mobile-open="mobileOpen"
      :user-menu-expanded="userMenuExpanded"
      :nav-items="navItems"
      :user-count="counts.users"
      :role-count="counts.roles"
      :permission-count="counts.permissions"
      :display-name="account?.display_name || account?.username || '已登录用户'"
      :role-label="roleLabel"
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
        <RouterView />
      </div>
    </main>
  </AdminLayout>
</template>

<style scoped>
.admin-main {
  min-width: 0;
  /* 允许 flex 子项收缩，配合 .admin-content 的 overflow 实现内部滚动（顶栏固定） */
  min-height: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
}
.admin-content {
  flex: 1;
  padding: 32px 36px 48px;
  overflow: auto;
}
.admin-backdrop {
  display: none;
}
@media (max-width: 1100px) {
  .admin-content {
    padding-left: 24px;
    padding-right: 24px;
  }
}
@media (max-width: 760px) {
  .admin-content {
    padding: 24px 16px 40px;
  }
  .admin-backdrop {
    display: block;
    position: fixed;
    inset: 0;
    background: var(--overlay);
    z-index: 10;
  }
}
</style>
