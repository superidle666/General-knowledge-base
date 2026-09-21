<!--
 * @FilePath: @/components/admin/AdminSidebar.vue
 * @Author: 项目维护者
 * @Date: 2026-08-31
 * @Description: 管理端侧栏导航，包含用户、角色和权限子菜单
 * @BusinessRule: 用户管理展开项共享同一权限管理入口
 -->
<script setup lang="ts">
import { ChevronDownIcon, CloseIcon } from 'tdesign-icons-vue-next'
import type { Component } from 'vue'

type NavKey =
  | 'dashboard'
  | 'libraries'
  | 'documents'
  | 'users'
  | 'roles'
  | 'permissions'
  | 'sessions'
  | 'settings'

type NavItem = {
  key: NavKey
  label: string
  icon: Component
}

const props = defineProps<{
  collapsed: boolean
  mobileOpen: boolean
  activeNav: NavKey
  userMenuExpanded: boolean
  navItems: NavItem[]
  userCount: number
  roleCount: number
  permissionCount: number
  displayName: string
  roleLabel: string
}>()

const emit = defineEmits<{
  (event: 'navigate', key: NavKey): void
  (event: 'toggle-users'): void
  (event: 'update:mobile-open', value: boolean): void
}>()

/** 触发模块导航，并由父组件统一维护当前路由状态。 */
function handleNav(key: NavKey) {
  if (key === 'users') {
    emit('navigate', 'users')

    if (!props.collapsed) {
      emit('toggle-users')
    }

    return
  }

  emit('navigate', key)
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
      <button
        class="mobile-close"
        aria-label="关闭导航"
        @click="emit('update:mobile-open', false)"
      >
        <CloseIcon />
      </button>
    </div>

    <nav class="admin-nav">
      <div v-if="!props.collapsed" class="admin-nav-label">管理中心</div>
      <template v-for="item in props.navItems" :key="item.key">
        <button
          class="admin-nav-item"
          :class="{ active: props.activeNav === item.key }"
          @click="handleNav(item.key)"
        >
          <component :is="item.icon" />
          <span v-if="!props.collapsed">{{ item.label }}</span>
          <ChevronDownIcon
            v-if="!props.collapsed && item.key === 'users'"
            class="user-nav-chevron"
            :class="{ expanded: props.userMenuExpanded }"
          />
        </button>

        <div
          v-if="!props.collapsed && item.key === 'users' && props.userMenuExpanded"
          class="admin-subnav"
        >
          <button
            :class="{ active: props.activeNav === 'users' }"
            @click="emit('navigate', 'users')"
          >
            用户列表
            <span>{{ props.userCount }}</span>
          </button>
          <button
            :class="{ active: props.activeNav === 'roles' }"
            @click="emit('navigate', 'roles')"
          >
            角色管理
            <span>{{ props.roleCount }}</span>
          </button>
          <button
            :class="{ active: props.activeNav === 'permissions' }"
            @click="emit('navigate', 'permissions')"
          >
            权限管理
            <span>{{ props.permissionCount }}</span>
          </button>
        </div>
      </template>
    </nav>

    <div class="admin-sidebar-bottom">
      <div class="admin-profile">
        <div class="avatar admin-avatar">{{ props.displayName.slice(0, 1) }}</div>
        <div v-if="!props.collapsed">
          <strong>{{ props.displayName }}</strong>
          <span>{{ props.roleLabel }}</span>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.admin-sidebar { width: 232px; flex: 0 0 232px; background: #fff; border-right: 1px solid #e5e7eb; display: flex; flex-direction: column; transition: width .2s, transform .2s; z-index: 20; position: sticky; top: 0; align-self: flex-start; height: 100vh; overflow: hidden; }
.admin-sidebar.collapsed { width: 70px; flex-basis: 70px; }
.admin-brand { height: 70px; padding: 0 18px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid #e5e7eb; }
.admin-brand-copy { display: grid; gap: 2px; }
.admin-brand-copy strong { font-size: 15px; }
.admin-brand-copy span { color: #6b7280; font-size: 10px; }
.admin-nav { flex: 1; min-height: 0; overflow-y: auto; padding: 22px 12px; }
.admin-nav-label { color: #9ca3af; font-size: 11px; padding: 0 12px 10px; }
.admin-nav-item { width: 100%; height: 42px; border: 0; background: transparent; color: #6b7280; border-radius: 5px; display: flex; align-items: center; gap: 11px; padding: 0 12px; text-align: left; font-size: 13px; margin-bottom: 3px; }
.admin-nav-item:hover, .admin-nav-item.active { color: #2563eb; background: #eff6ff; }
.admin-nav-item.active { font-weight: 600; }
.admin-nav-item svg { width: 17px; flex: 0 0 auto; }
.admin-nav-count { margin-left: auto; color: #9ca3af; font-size: 10px; }
.admin-sidebar-bottom { border-top: 1px solid #e5e7eb; padding: 14px 12px; flex-shrink: 0; }
.admin-profile { display: flex; align-items: center; gap: 9px; margin-bottom: 13px; }
.admin-profile > div:last-child { display: grid; gap: 2px; min-width: 0; }
.admin-profile strong { font-size: 12px; white-space: nowrap; }
.admin-profile span { color: #9ca3af; font-size: 10px; }
.mobile-close { display: none; margin-left: auto; border: 0; background: transparent; color: #6b7280; }
.admin-subnav { display: grid; gap: 2px; padding: 0 0 6px 30px; }
.admin-subnav button { display: flex; align-items: center; justify-content: space-between; border: 0; border-left: 1px solid #e5e7eb; background: transparent; color: #6b7280; padding: 7px 10px; text-align: left; font-size: 11px; }
.admin-subnav button:hover, .admin-subnav button.active { color: #2563eb; border-left-color: #2563eb; background: #eff6ff; }
.admin-subnav button span { color: #9ca3af; font-size: 10px; }
.user-nav-chevron { margin-left: auto; width: 14px !important; transition: transform .2s; }
.user-nav-chevron.expanded { transform: rotate(180deg); }
@media (max-width: 760px) {
  .admin-sidebar { position: fixed; inset: 0 auto 0 0; transform: translateX(-100%); box-shadow: 8px 0 25px rgb(15 23 42 / 12%); }
  .admin-sidebar.open { transform: translateX(0); }
  .admin-sidebar.open .mobile-close { display: block; }
  .admin-sidebar.collapsed { width: 232px; }
  .admin-sidebar.collapsed .admin-brand-copy,
  .admin-sidebar.collapsed .admin-nav-label,
  .admin-sidebar.collapsed .admin-nav-item span,
  .admin-sidebar.collapsed .admin-profile > div:last-child { display: block; }
}
</style>
