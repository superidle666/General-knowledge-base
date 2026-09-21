<!--
 * @FilePath: frontend/src/components/user/UserTopbar.vue
 * @Date: 2026-09-07
 * @Description: 用户端顶部导航，展示当前知识库、登录状态、主题切换和退出入口。
 -->
<script setup lang="ts">
import { LightbulbIcon, LogoutIcon, MoonIcon, ViewListIcon } from 'tdesign-icons-vue-next'
const props = defineProps<{ darkMode: boolean; activeLibrary: string; liveData: boolean; loggedIn: boolean; displayName: string; roleLabel?: string; avatarText: string }>()
const emit = defineEmits<{ (event: 'open-menu'): void; (event: 'toggle-theme'): void; (event: 'login'): void; (event: 'logout'): void }>()
</script>
<template>
  <header class="topbar">
    <div class="topbar-left"><t-button variant="text" shape="square" class="mobile-menu" @click="emit('open-menu')"><ViewListIcon /></t-button><span class="workspace-label">用户端</span><span class="divider"></span><span class="library-hint"><span class="library-dot"></span>{{ props.activeLibrary }}</span></div>
    <div class="topbar-right"><span class="mock-badge"><span class="pulse-dot"></span>{{ props.liveData ? '实时数据' : '等待连接' }}</span><div class="topbar-account"><strong>{{ props.displayName }}</strong><span>{{ props.roleLabel || (props.loggedIn ? '已登录用户' : '匿名体验用户') }}</span></div><div class="avatar">{{ props.avatarText }}</div><t-button v-if="props.loggedIn" variant="text" shape="square" class="user-top-theme" :title="props.darkMode ? '切换浅色模式' : '切换深色模式'" @click="emit('toggle-theme')"><MoonIcon v-if="!props.darkMode" /><LightbulbIcon v-else /></t-button><t-button v-else variant="outline" size="small" @click="emit('login')">登录</t-button><t-button v-if="props.loggedIn" variant="text" shape="square" class="user-top-logout" aria-label="退出登录" title="退出登录" @click="emit('logout')"><LogoutIcon /></t-button></div>
  </header>
</template>
