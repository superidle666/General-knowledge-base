<!--
 * @FilePath: @/components/admin/AdminHeader.vue
 * @Author: 项目维护者
 * @Date: 2026-08-31
 * @Description: 管理端顶部栏，展示当前模块标题和移动端菜单入口
 -->
<script setup lang="ts">
import { ChevronRightIcon, LightbulbIcon, MenuIcon, MoonIcon, LogoutIcon } from 'tdesign-icons-vue-next'

const props = defineProps<{ title: string; displayName: string; roleLabel: string; darkMode: boolean }>()

const emit = defineEmits<{
  (event: 'open-menu'): void
  (event: 'logout'): void
  (event: 'toggle-theme'): void
}>()
</script>

<template>
  <header class="admin-topbar">
    <div class="admin-top-left">
      <t-button
        variant="text"
        shape="square"
        class="admin-mobile-menu"
        aria-label="打开导航"
        @click="emit('open-menu')"
      >
        <MenuIcon />
      </t-button>
      <span class="admin-breadcrumb">管理端</span>
      <ChevronRightIcon />
      <strong>{{ title }}</strong>
    </div>
    <div class="admin-top-right">
      <span class="admin-env">
        <span class="pulse-dot" />
        已认证
      </span>
      <div class="admin-top-account">
        <strong>{{ props.displayName }}</strong>
        <span>{{ props.roleLabel || '已登录用户' }}</span>
      </div>
      <div class="avatar admin-avatar">{{ props.displayName.slice(0, 1) }}</div>
      <t-tooltip :content="props.darkMode ? '切换浅色模式' : '切换夜间模式'">
        <t-button
          variant="text"
          shape="square"
          class="admin-top-theme"
          :aria-label="props.darkMode ? '切换浅色模式' : '切换夜间模式'"
          @click="emit('toggle-theme')"
        >
          <LightbulbIcon v-if="props.darkMode" />
          <MoonIcon v-else />
        </t-button>
      </t-tooltip>
      <t-tooltip content="退出登录">
        <t-button variant="text" shape="square" class="admin-top-logout" aria-label="退出登录" @click="emit('logout')">
          <LogoutIcon />
        </t-button>
      </t-tooltip>
    </div>
  </header>
</template>

<style scoped>
.admin-topbar {
  /* 与侧栏品牌区 70px 等高，保证两条下边框对齐 */
  height: 70px;
  flex: 0 0 70px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 0 30px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.admin-top-left,
.admin-top-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.admin-top-left svg {
  width: 15px;
  color: var(--weak);
}
.admin-breadcrumb {
  color: var(--muted);
  font-size: 12px;
}
.admin-top-left strong {
  font-size: 13px;
}
.admin-env {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--success);
  background: var(--success-bg);
  border: 1px solid var(--success-border);
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 11px;
}
.admin-env .pulse-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
  background: var(--success-strong);
}
.admin-top-account {
  display: grid;
  gap: 2px;
  text-align: right;
  min-width: 0;
}
.admin-top-account strong {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 130px;
}
.admin-top-account span {
  color: var(--weak);
  font-size: 10px;
}
.admin-top-logout {
  color: var(--muted);
}
.admin-top-logout:hover {
  color: var(--danger);
  background: var(--danger-bg);
}
.admin-top-logout svg {
  width: 17px;
}
.admin-top-theme {
  color: var(--muted);
}
.admin-top-theme:hover {
  color: var(--primary);
  background: var(--primary-weak);
}
.admin-mobile-menu {
  display: none;
}
@media (max-width: 760px) {
  .admin-topbar {
    padding: 0 16px;
  }
  .admin-mobile-menu {
    display: inline-flex;
  }
  .admin-env {
    display: none;
  }
}
</style>
