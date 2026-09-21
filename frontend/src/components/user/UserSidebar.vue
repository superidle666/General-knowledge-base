<!--
 * @FilePath: frontend/src/components/user/UserSidebar.vue
 * @Date: 2026-09-07
 * @Description: 用户端侧边栏，展示知识库、连续会话和用户操作入口。
 -->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { AddIcon, ChevronDownIcon, ChevronLeftIcon, MoreIcon, SearchIcon, ViewListIcon } from 'tdesign-icons-vue-next'
import type { UserConversation, UserLibrary } from '../../types/user'

const props = defineProps<{
  mobileOpen: boolean
  libraries: UserLibrary[]
  activeLibrary: string
  conversations: UserConversation[]
  searchQuery: string
  displayName: string
  roleLabel: string
  avatarText: string
  libraryExpanded: boolean
}>()
const emit = defineEmits<{
  (event: 'create-conversation'): void
  (event: 'select-library', name: string): void
  (event: 'select-conversation', item: UserConversation): void
  (event: 'rename-conversation', item: UserConversation): void
  (event: 'delete-conversation', item: UserConversation): void
  (event: 'update:search-query', value: string): void
  (event: 'close-mobile'): void
  (event: 'toggle-library'): void
}>()
const openMenuId = ref<string | null>(null)
const filteredConversations = computed(() => {
  const value = props.searchQuery.trim().toLowerCase()
  return value ? props.conversations.filter((item) => item.title.toLowerCase().includes(value)) : props.conversations
})
const groupedConversations = computed(() => {
  const groups = new Map<string, UserConversation[]>()
  for (const item of filteredConversations.value) {
    const group = item.group || '更早'
    groups.set(group, [...(groups.get(group) || []), item])
  }
  return Array.from(groups, ([label, items]) => ({ label, items }))
})
function toggleConversationMenu(itemId: string) {
  openMenuId.value = openMenuId.value === itemId ? null : itemId
}
function renameConversation(item: UserConversation) {
  openMenuId.value = null
  emit('rename-conversation', item)
}
function deleteConversation(item: UserConversation) {
  openMenuId.value = null
  emit('delete-conversation', item)
}
</script>

<template>
  <aside class="sidebar" :class="{ 'is-mobile-open': mobileOpen }">
    <div class="brand-row"><div class="brand-mark">工</div><div class="brand-copy"><strong>工智库</strong><span>设备技术知识平台</span></div><t-button variant="text" shape="square" class="sidebar-close" @click="emit('close-mobile')"><ChevronLeftIcon /></t-button></div>
    <div class="sidebar-content">
      <nav class="nav-list"><div class="nav-label">工作台</div><button class="nav-item active" @click="emit('toggle-library')"><ViewListIcon /><span>知识库</span><span class="nav-count">{{ libraries.length }}</span><ChevronDownIcon class="nav-chevron" :class="{ rotated: libraryExpanded }" /></button><div v-if="libraryExpanded" class="library-list"><button v-for="library in libraries" :key="library.name" class="library-item" :class="{ selected: activeLibrary === library.name }" @click.stop="emit('select-library', library.name)"><span class="library-dot" :style="{ background: library.color }"></span><span>{{ library.name }}</span><small>{{ library.count }}</small></button></div><div class="search-nav"><SearchIcon /><input :value="searchQuery" placeholder="搜索会话" @input="emit('update:search-query', ($event.target as HTMLInputElement).value)" /></div></nav>
      <div class="conversation-section"><div class="section-heading"><span>聊天记录</span><t-button class="new-chat-button" @click="emit('create-conversation')"><AddIcon />新建咨询</t-button></div><div v-if="groupedConversations.length" class="conversation-list"><section v-for="group in groupedConversations" :key="group.label" class="history-group"><div class="history-group-title">{{ group.label }}</div><div v-for="item in group.items" :key="item.id" class="conversation-row"><button class="conversation-item" :class="{ selected: item.active }" @click="emit('select-conversation', item)"><span class="conversation-dot"></span><span class="conversation-title">{{ item.title }}</span><span class="conversation-time">{{ item.time }}</span></button><button class="conversation-more" title="更多操作" @click.stop="toggleConversationMenu(item.id)"><MoreIcon /></button><div v-if="openMenuId === item.id" class="conversation-menu"><button @click.stop="renameConversation(item)">重命名</button><button class="danger" @click.stop="deleteConversation(item)">删除</button></div></div></section></div><div v-else class="conversation-empty">暂无聊天记录</div></div>
    </div>
    <div class="sidebar-footer"><div class="user-card"><div class="avatar small">{{ avatarText }}</div><div class="user-copy"><strong>{{ displayName }}</strong><span>{{ roleLabel }}</span></div></div></div>
  </aside>
</template>
