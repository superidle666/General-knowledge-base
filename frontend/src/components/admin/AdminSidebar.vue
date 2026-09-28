<!--
 * @FilePath: @/components/admin/AdminSidebar.vue
 * @Author: 项目维护者
 * @Date: 2026-08-31
 * @Description: 管理端侧栏导航，包含用户、角色和权限子菜单
 * @BusinessRule: 用户管理展开项共享同一权限管理入口
 -->
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChevronDownIcon, CloseIcon } from 'tdesign-icons-vue-next'
import type { Component } from 'vue'

type NavKey = 'dashboard' | 'libraries' | 'documents' | 'users' | 'roles' | 'permissions' | 'sessions' | 'settings'

type NavItem = {
  key: NavKey
  label: string
  icon: Component
}

const props = defineProps<{
  collapsed: boolean
  mobileOpen: boolean
  userMenuExpanded: boolean
  navItems: NavItem[]
  userCount: number
  roleCount: number
  permissionCount: number
  displayName: string
  roleLabel: string
}>()

const emit = defineEmits<{
  (event: 'toggle-users', target?: NavKey): void
  (event: 'update:mobile-open', value: boolean): void
}>()

const route = useRoute()
const router = useRouter()

/** 当前模块由子路由名派生（admin-documents → documents）。 */
const activeNav = computed(() => String(route.name ?? '').replace(/^admin-/, '') as NavKey)

/** 导航到模块子路由；非展开项选中后收起移动抽屉，“用户管理”保持原展开切换语义。 */
function handleNav(key: NavKey) {
  void router.push({ name: `admin-${key}` })
  if (key === 'users') {
    emit('toggle-users', key)
    return
  }
  if (props.mobileOpen) emit('update:mobile-open', false)
}
</script>

<template>
  <aside class="admin-sidebar" :class="{ collapsed: props.collapsed, open: props.mobileOpen }">
    <div class="admin-brand">
      <div class="admin-mark">工</div>
      <div v-if="!props.collapsed" class="admin-brand-copy">
        <strong>工智库</strong>
        <span>管理控制台</span>
      </div>
      <button class="mobile-close" aria-label="关闭导航" @click="emit('update:mobile-open', false)">
        <CloseIcon />
      </button>
    </div>

    <nav class="admin-nav">
      <div v-if="!props.collapsed" class="admin-nav-label">管理中心</div>
      <template v-for="item in props.navItems" :key="item.key">
        <button class="admin-nav-item" :class="{ active: activeNav === item.key }" @click="handleNav(item.key)">
          <component :is="item.icon" />
          <span v-if="!props.collapsed">{{ item.label }}</span>
          <ChevronDownIcon
            v-if="!props.collapsed && item.key === 'users'"
            class="user-nav-chevron"
            :class="{ expanded: props.userMenuExpanded }"
          />
        </button>

        <div v-if="!props.collapsed && item.key === 'users' && props.userMenuExpanded" class="admin-subnav">
          <button :class="{ active: activeNav === 'users' }" @click="handleNav('users')">
            用户列表
            <span>{{ props.userCount }}</span>
          </button>
          <button :class="{ active: activeNav === 'roles' }" @click="handleNav('roles')">
            角色管理
            <span>{{ props.roleCount }}</span>
          </button>
          <button :class="{ active: activeNav === 'permissions' }" @click="handleNav('permissions')">
            权限管理
            <span>{{ props.permissionCount }}</span>
          </button>
        </div>
      </template>
    </nav>

    <div class="admin-sidebar-bottom">
      <div class="admin-profile">
        <div class="avatar small admin-avatar">{{ props.displayName.slice(0, 1) }}</div>
        <div v-if="!props.collapsed">
          <strong>{{ props.displayName }}</strong>
          <span>{{ props.roleLabel }}</span>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.admin-sidebar {
  width: 232px;
  flex: 0 0 232px;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  transition:
    width 0.2s,
    transform 0.2s;
  z-index: 20;
  position: sticky;
  top: 0;
  align-self: flex-start;
  height: 100vh;
  overflow: hidden;
}
.admin-sidebar.collapsed {
  width: 70px;
  flex-basis: 70px;
}
.admin-brand {
  height: 70px;
  padding: 0 18px;
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid var(--border);
}
.admin-brand-copy {
  display: grid;
  gap: 2px;
}
.admin-brand-copy strong {
  font-size: 15px;
}
.admin-brand-copy span {
  color: var(--muted);
  font-size: 10px;
}
.admin-nav {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 22px 12px;
}
.admin-nav-label {
  color: var(--weak);
  font-size: 11px;
  padding: 0 12px 10px;
}
.admin-nav-item {
  width: 100%;
  height: 42px;
  border: 0;
  background: transparent;
  color: var(--muted);
  border-radius: 5px;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 0 12px;
  text-align: left;
  font-size: 13px;
  margin-bottom: 3px;
}
.admin-nav-item:hover,
.admin-nav-item.active {
  color: var(--primary);
  background: var(--primary-weak);
}
.admin-nav-item.active {
  font-weight: 600;
}
.admin-nav-item svg {
  width: 17px;
  flex: 0 0 auto;
}
.admin-nav-count {
  margin-left: auto;
  color: var(--weak);
  font-size: 10px;
}
.admin-sidebar-bottom {
  border-top: 1px solid var(--border);
  padding: 14px 12px;
  flex-shrink: 0;
}
.admin-profile {
  display: flex;
  align-items: center;
  gap: 9px;
}
.admin-profile > div:last-child {
  display: grid;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.admin-profile strong {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.admin-profile span {
  color: var(--weak);
  font-size: 10px;
}
.mobile-close {
  display: none;
  margin-left: auto;
  border: 0;
  background: transparent;
  color: var(--muted);
}
.admin-subnav {
  display: grid;
  gap: 2px;
  padding: 0 0 6px 30px;
}
.admin-subnav button {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border: 0;
  border-left: 1px solid var(--border);
  background: transparent;
  color: var(--muted);
  padding: 7px 10px;
  text-align: left;
  font-size: 11px;
}
.admin-subnav button:hover,
.admin-subnav button.active {
  color: var(--primary);
  border-left-color: var(--primary);
  background: var(--primary-weak);
}
.admin-subnav button span {
  color: var(--weak);
  font-size: 10px;
}
.user-nav-chevron {
  margin-left: auto;
  width: 14px !important;
  transition: transform 0.2s;
}
.user-nav-chevron.expanded {
  transform: rotate(180deg);
}
@media (max-width: 760px) {
  .admin-sidebar {
    position: fixed;
    inset: 0 auto 0 0;
    transform: translateX(-100%);
    box-shadow: var(--shadow-side);
  }
  .admin-sidebar.open {
    transform: translateX(0);
  }
  .admin-sidebar.open .mobile-close {
    display: block;
  }
  .admin-sidebar.collapsed {
    width: 232px;
  }
  .admin-sidebar.collapsed .admin-brand-copy,
  .admin-sidebar.collapsed .admin-nav-label,
  .admin-sidebar.collapsed .admin-nav-item span,
  .admin-sidebar.collapsed .admin-profile > div:last-child {
    display: block;
  }
}
</style>
