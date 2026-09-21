<!-- 用户端问答工作台页面。 -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import UserLayout from '../../layouts/UserLayout.vue'
import UserSidebar from '../../components/user/UserSidebar.vue'
import UserTopbar from '../../components/user/UserTopbar.vue'
import UserChatPanel from '../../components/user/UserChatPanel.vue'
import UserSourcePanel from '../../components/user/UserSourcePanel.vue'
import { createQuerySession, deleteQuerySession, getQuerySessionMessages, listQueryLibraries, listQuerySessions, renameQuerySession, type QueryLibrary, type QuerySession } from '../../api/query'
import { useUserQuery } from '../../composables/useUserQuery'
import { useUserStore } from '../../store/modules/user'
import type { QueryChatMessage, UserConversation, UserLibrary, UserMessage, UserSource } from '../../types/user'

const ANONYMOUS_QUERY_LIMIT = 3
const ANONYMOUS_QUERY_COUNT_KEY = 'kb-user-anonymous-query-count'
const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const { isLoggedIn, userInfo } = storeToRefs(userStore)
const darkMode = ref(false)
const mobileSidebarOpen = ref(false)
const sourcePanelOpen = ref(true)
const searchQuery = ref('')
const activeLibrary = ref('')
const inputText = ref('')
const libraries = ref<UserLibrary[]>([])
const libraryError = ref('')
const anonymousLimitDialogVisible = ref(false)
const libraryExpanded = ref(false)
const librarySwitchNotice = ref('')
let libraryNoticeTimer: number | undefined
const { isLoading: isSending, error: queryError, sources: querySources, submit: submitQuery, reset: resetQuery, restore: restoreQuery } = useUserQuery()
const queryContextVersion = ref(0)
const sessionError = ref('')
const currentSessionId = ref<string | null>(null)
const selectedHistoryItemId = ref<string | null>(null)
const querySessions = ref<QuerySession[]>([])

const messages = ref<UserMessage[]>([])

const selectedLibrary = computed(() => libraries.value.find((item) => item.name === activeLibrary.value) || null)
const activeLibraryName = computed(() => selectedLibrary.value?.name || '未选择知识库')
const sources = computed<UserSource[]>(() => querySources.value.map((source, index) => {
  const score = source.score === null ? null : source.score > 1 ? source.score : source.score * 100
  return {
    id: String(source.chunk_id ?? `${source.document_id ?? 'source'}-${index}`),
    title: source.document_title || source.section_title || '未命名文档',
    type: source.page_number ? `知识库文档 · 第 ${source.page_number} 页` : '知识库文档',
    score: score === null ? '--' : `${Math.round(score)}%`,
    color: ['#2563eb', '#0f9d78', '#c47f1d'][index % 3],
    content: source.content,
    documentId: source.document_id,
    chunkId: source.chunk_id,
    chunkIndex: source.chunk_index,
    pageNumber: source.page_number,
    sectionTitle: source.section_title,
    parentTitle: source.parent_title,
  }
}))
const chatTitle = computed(() => messages.value.slice().reverse().find((message) => message.role === 'user')?.content || '开始一次新的技术咨询')
const conversations = computed<UserConversation[]>(() => querySessions.value
  .slice()
  .sort((left, right) => right.updated_at - left.updated_at)
  .map((session) => ({
    id: session.id,
    libraryId: session.library_id,
    title: session.title || '新建技术咨询',
    time: formatTimestamp(session.updated_at),
    group: getHistoryGroup(session.updated_at),
    active: session.id === currentSessionId.value,
  })))
const queryMessage = computed(() => queryError.value || libraryError.value || sessionError.value)
const displayName = computed(() => userInfo.value?.display_name || userInfo.value?.username || '访客')
const roleLabel = computed(() => isLoggedIn.value ? '已登录用户' : '匿名体验用户')
const avatarText = computed(() => displayName.value.trim().slice(0, 1) || '访')

function getAnonymousQueryCount() {
  const storedCount = Number.parseInt(window.localStorage.getItem(ANONYMOUS_QUERY_COUNT_KEY) || '0', 10)
  return Number.isFinite(storedCount) && storedCount >= 0 ? storedCount : 0
}

function consumeAnonymousQuery() {
  window.localStorage.setItem(ANONYMOUS_QUERY_COUNT_KEY, String(getAnonymousQueryCount() + 1))
}

function goToLogin() {
  void router.push({ name: 'user-login', query: { redirect: route.fullPath, reason: 'anonymous-limit' } })
}

function handleAnonymousLimit() {
  anonymousLimitDialogVisible.value = true
}

function confirmLoginAfterLimit() {
  anonymousLimitDialogVisible.value = false
  goToLogin()
}

function formatTimestamp(timestamp: number) {
  if (!timestamp) return '刚刚'
  const date = new Date(timestamp * 1000)
  const now = new Date()
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}

function getHistoryGroup(timestamp?: number) {
  if (!timestamp) return '今天'
  const date = new Date(timestamp * 1000)
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const dayStart = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
  const dayDistance = Math.floor((startOfToday - dayStart) / 86400000)
  if (dayDistance <= 0) return '今天'
  if (dayDistance === 1) return '昨天'
  if (dayDistance < 3) return '三天内'
  if (dayDistance < 30) return '一月内'
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`
}

function toUserMessage(message: QueryChatMessage): UserMessage {
  return {
    id: `history-message-${message.id}`,
    role: message.role === 'user' ? 'user' : 'assistant',
    content: message.content,
    time: formatTimestamp(message.created_at),
    timestamp: message.created_at,
    imageUrls: message.image_urls,
  }
}

async function loadConversationsForCurrentLibrary() {
  const libraryId = selectedLibrary.value?.id
  sessionError.value = ''
  if (!isLoggedIn.value || !libraryId) {
    currentSessionId.value = null
    querySessions.value = []
    messages.value = []
    return
  }

  const contextVersion = queryContextVersion.value
  try {
    const sessionRows = (await listQuerySessions(libraryId)).sort((left, right) => right.updated_at - left.updated_at)
    if (contextVersion !== queryContextVersion.value || selectedLibrary.value?.id !== libraryId || !isLoggedIn.value) return

    querySessions.value = sessionRows
    const session = sessionRows[0]
    currentSessionId.value = session?.id || null
    if (!session) {
      querySessions.value = []
      messages.value = []
      restoreQuery({ library_id: libraryId, session_id: null, query: '', answer: '', sources: [], image_urls: [] })
      return
    }
    const result = await getQuerySessionMessages(session.id)
    if (contextVersion !== queryContextVersion.value || selectedLibrary.value?.id !== libraryId || !isLoggedIn.value) return
    currentSessionId.value = result.session_id
    messages.value = result.messages.map(toUserMessage)
    selectedHistoryItemId.value = messages.value.filter((message) => message.role === 'user').at(-1)?.id || null
    const lastUserMessage = [...result.messages].reverse().find((message) => message.role === 'user')
    const lastAssistantMessage = [...result.messages].reverse().find((message) => message.role === 'assistant')
    restoreQuery({ library_id: result.library_id, session_id: result.session_id, query: lastUserMessage?.content || '', answer: lastAssistantMessage?.content || '', sources: lastAssistantMessage?.sources || [], image_urls: lastAssistantMessage?.image_urls || [] })
  } catch (error) {
    if (contextVersion !== queryContextVersion.value) return
    sessionError.value = error instanceof Error ? error.message : '历史会话加载失败，请稍后重试'
    currentSessionId.value = null
    querySessions.value = []
    messages.value = []
  }
}

async function restoreConversation(item: UserConversation) {
  if (item.id === currentSessionId.value) {
    mobileSidebarOpen.value = false
    return
  }
  const libraryId = selectedLibrary.value?.id
  if (!libraryId) return
  const contextVersion = queryContextVersion.value
  sessionError.value = ''
  try {
    const result = await getQuerySessionMessages(item.id)
    if (contextVersion !== queryContextVersion.value || selectedLibrary.value?.id !== libraryId || !isLoggedIn.value) return
    currentSessionId.value = result.session_id
    messages.value = result.messages.map(toUserMessage)
    selectedHistoryItemId.value = messages.value.filter((message) => message.role === 'user').at(-1)?.id || null
    const lastUserMessage = [...result.messages].reverse().find((message) => message.role === 'user')
    const lastAssistantMessage = [...result.messages].reverse().find((message) => message.role === 'assistant')
    restoreQuery({ library_id: result.library_id, session_id: result.session_id, query: lastUserMessage?.content || '', answer: lastAssistantMessage?.content || '', sources: lastAssistantMessage?.sources || [], image_urls: lastAssistantMessage?.image_urls || [] })
    mobileSidebarOpen.value = false
  } catch (error) {
    sessionError.value = error instanceof Error ? error.message : '历史会话加载失败，请稍后重试'
  }
}

async function logout() {
  invalidateQueryContext()
  try {
    await userStore.logout()
  } finally {
    messages.value = []
    currentSessionId.value = null
    selectedHistoryItemId.value = null
    querySessions.value = []
    sessionError.value = ''
    librarySwitchNotice.value = ''
    mobileSidebarOpen.value = false
  }
}

function toUserLibrary(library: QueryLibrary): UserLibrary {
  return {
    id: library.id,
    name: library.name,
    count: library.document_count,
    color: library.cover_color || '#2563eb',
    status: library.status,
    updatedAt: library.updated_at,
  }
}

async function loadLibraries() {
  try {
    const libraryRows = await listQueryLibraries()
    libraries.value = libraryRows.filter((library) => library.status === 'active').map(toUserLibrary)
    if (!libraries.value.length) {
      activeLibrary.value = ''
      libraryError.value = '当前没有可用的知识库，请联系管理员启用知识库并完成文档导入后再试'
      invalidateQueryContext()
      return
    }
    if (!selectedLibrary.value) {
      activeLibrary.value = libraries.value[0].name
      libraryError.value = ''
    }
    invalidateQueryContext()
    await loadConversationsForCurrentLibrary()
  } catch (error) {
    libraryError.value = error instanceof Error ? error.message : '知识库加载失败，请稍后重试'
  }
}

async function createConversation() {
  if (!isLoggedIn.value) {
    goToLogin()
    return
  }
  const libraryId = selectedLibrary.value?.id
  if (!libraryId) return
  try {
    const session = await createQuerySession(libraryId)
    invalidateQueryContext()
    currentSessionId.value = session.id
    selectedHistoryItemId.value = null
    querySessions.value = [session, ...querySessions.value.filter((item) => item.id !== session.id)]
    restoreQuery({ library_id: libraryId, session_id: session.id, query: '', answer: '', sources: [], image_urls: [] })
    mobileSidebarOpen.value = false
  } catch (error) {
    sessionError.value = error instanceof Error ? error.message : '新建会话失败，请稍后重试'
  }
}

async function renameConversation(item: UserConversation) {
  const title = window.prompt('请输入新的会话名称', item.title)?.trim()
  if (!title || title === item.title) return
  try {
    const updated = await renameQuerySession(item.id, title)
    querySessions.value = querySessions.value.map((session) => session.id === updated.id ? updated : session)
  } catch (error) {
    sessionError.value = error instanceof Error ? error.message : '会话重命名失败，请稍后重试'
  }
}

async function deleteConversation(item: UserConversation) {
  if (!window.confirm(`确定删除会话“${item.title}”吗？`)) return
  try {
    await deleteQuerySession(item.id)
    querySessions.value = querySessions.value.filter((session) => session.id !== item.id)
    if (item.id === currentSessionId.value) {
      invalidateQueryContext()
      currentSessionId.value = null
      selectedHistoryItemId.value = null
      await loadConversationsForCurrentLibrary()
    }
  } catch (error) {
    sessionError.value = error instanceof Error ? error.message : '删除会话失败，请稍后重试'
  }
}
function selectConversation(item: UserConversation) {
  void restoreConversation(item)
}

function selectLibrary(name: string) {
  mobileSidebarOpen.value = false
  if (name === activeLibrary.value) return
  invalidateQueryContext()
  activeLibrary.value = name
  libraryError.value = ''
  librarySwitchNotice.value = `已切换到“${name}”，正在加载连续聊天记录`
  if (libraryNoticeTimer) window.clearTimeout(libraryNoticeTimer)
  libraryNoticeTimer = window.setTimeout(() => { librarySwitchNotice.value = '' }, 2800)
  void loadConversationsForCurrentLibrary()
}

function invalidateQueryContext() {
  queryContextVersion.value += 1
  resetQuery()
  messages.value = []
  inputText.value = ''
}

function currentQueryContext(libraryId: number) {
  return `${queryContextVersion.value}:${libraryId}`
}

function isCurrentQueryContext(context: string, libraryId: number) {
  return selectedLibrary.value?.id === libraryId && currentQueryContext(libraryId) === context
}

async function sendMessage() {
  const text = inputText.value.trim()
  if (!text || isSending.value) return
  if (!isLoggedIn.value && getAnonymousQueryCount() >= ANONYMOUS_QUERY_LIMIT) {
    handleAnonymousLimit()
    return
  }
  const libraryId = selectedLibrary.value?.id
  if (!libraryId) {
    libraryError.value = '当前没有可用的知识库，请联系管理员启用知识库并完成文档导入后再试'
    return
  }
  libraryError.value = ''
  if (!isLoggedIn.value) consumeAnonymousQuery()
  const userMessageId = `local-message-${Date.now()}`
  messages.value.push({ id: userMessageId, role: 'user', content: text, time: '现在', timestamp: Date.now() / 1000 })
  selectedHistoryItemId.value = userMessageId
  inputText.value = ''
  const assistantMessageId = `local-message-${Date.now()}-assistant`
  messages.value.push({ id: assistantMessageId, role: 'assistant', content: '', time: '现在', streaming: true })
  const context = currentQueryContext(libraryId)
  try {
    const result = await submitQuery({
      library_id: libraryId,
      query: text,
      session_id: currentSessionId.value || undefined,
      top_k: 5,
      use_hyde: false,
      use_web_search: false,
    }, (delta) => {
      if (!isCurrentQueryContext(context, libraryId)) return
      updateAssistantMessage(assistantMessageId, {
        content: `${messages.value.find((message) => message.id === assistantMessageId)?.content || ''}${delta}`,
      })
    })
    if (!result || !isCurrentQueryContext(context, libraryId)) return
    currentSessionId.value = result.session_id || currentSessionId.value
    updateAssistantMessage(assistantMessageId, { content: result.answer, imageUrls: result.image_urls })
    if (result.session_id) {
      try {
        querySessions.value = (await listQuerySessions(libraryId)).sort((left, right) => right.updated_at - left.updated_at)
      } catch {}
    }
  } catch (error) {
    if (!isCurrentQueryContext(context, libraryId)) return
    inputText.value = text
    updateAssistantMessage(assistantMessageId, {
      content: error instanceof Error ? error.message : '查询失败，请稍后重试',
    })
  } finally {
    if (!isCurrentQueryContext(context, libraryId)) return
    updateAssistantMessage(assistantMessageId, { streaming: false })
  }
}

function updateAssistantMessage(messageId: string, updates: Partial<UserMessage>) {
  const messageIndex = messages.value.findIndex((message) => message.id === messageId)
  if (messageIndex < 0) return
  messages.value[messageIndex] = { ...messages.value[messageIndex], ...updates }
}

function toggleTheme() { darkMode.value = !darkMode.value }
function useSuggestion(text: string) { inputText.value = text }

watch(isLoggedIn, (loggedIn, previouslyLoggedIn) => {
  if (loggedIn === previouslyLoggedIn) return
  invalidateQueryContext()
  sessionError.value = ''
  if (loggedIn) {
    void loadConversationsForCurrentLibrary()
  } else {
    messages.value = []
    currentSessionId.value = null
    selectedHistoryItemId.value = null
  }
})

onMounted(loadLibraries)
</script>

<template>
  <UserLayout :dark-mode="darkMode">
    <UserSidebar :mobile-open="mobileSidebarOpen" :libraries="libraries" :active-library="activeLibrary" :conversations="conversations" :search-query="searchQuery" :library-expanded="libraryExpanded" :display-name="displayName" :role-label="roleLabel" :avatar-text="avatarText" @create-conversation="createConversation" @select-library="selectLibrary" @select-conversation="selectConversation" @rename-conversation="renameConversation" @delete-conversation="deleteConversation" @update:search-query="searchQuery = $event" @close-mobile="mobileSidebarOpen = false" @toggle-library="libraryExpanded = !libraryExpanded" />
    <div v-if="mobileSidebarOpen" class="mobile-backdrop" @click="mobileSidebarOpen = false"></div>
    <main class="workspace"><div v-if="librarySwitchNotice" class="library-switch-toast" role="status">{{ librarySwitchNotice }}</div><UserTopbar :dark-mode="darkMode" :active-library="activeLibraryName" :live-data="libraries.length > 0" :logged-in="isLoggedIn" :display-name="displayName" :role-label="roleLabel" :avatar-text="avatarText" @open-menu="mobileSidebarOpen = true" @toggle-theme="toggleTheme" @login="goToLogin" @logout="logout" /><div class="content-grid"><UserChatPanel :messages="messages" :input-text="inputText" :is-sending="isSending" :source-panel-open="sourcePanelOpen" :title="chatTitle" :source-count="sources.length" @toggle-sources="sourcePanelOpen = !sourcePanelOpen" @update:input-text="inputText = $event" @send="sendMessage" @suggestion="useSuggestion" /><UserSourcePanel :open="sourcePanelOpen" :active-library="activeLibraryName" :sources="sources" :document-count="selectedLibrary?.count || 0" :updated-at="selectedLibrary?.updatedAt || null" @close="sourcePanelOpen = false" /></div><p v-if="queryMessage" class="user-query-error">{{ queryMessage }}</p></main>
    <t-dialog v-model:visible="anonymousLimitDialogVisible" header="登录后继续咨询" confirm-btn="去登录" cancel-btn="暂不登录" @confirm="confirmLoginAfterLimit">
      <p>匿名体验已使用 3 条消息。登录后可继续使用知识库问答功能。</p>
    </t-dialog>
  </UserLayout>
</template>
<style scoped>
.library-switch-toast { position: absolute; top: 12px; left: 50%; z-index: 30; transform: translateX(-50%); padding: 8px 14px; border: 1px solid #bfdbfe; border-radius: 6px; background: #eff6ff; color: #1d4ed8; box-shadow: 0 8px 20px rgb(37 99 235 / 12%); font-size: 12px; }
</style>
