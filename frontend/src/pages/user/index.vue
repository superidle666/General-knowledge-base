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
import {
  createQuerySession,
  deleteQuerySession,
  getQuerySessionMessages,
  listQueryLibraries,
  listQuerySessions,
  renameQuerySession,
  type QueryLibrary,
  type QuerySession,
} from '../../api/query'
import { useQueryContext } from '../../composables/useQueryContext'
import { useUserQuery } from '../../composables/useUserQuery'
import { STORAGE_KEY } from '../../constants'
import { useUserStore } from '../../store/modules/user'
import type { QueryChatMessage, UserConversation, UserLibrary, UserMessage, UserSource } from '../../types/user'
import { normalizeSources } from '../../utils/sources'

const ANONYMOUS_QUERY_LIMIT = 3
const ANONYMOUS_QUERY_COUNT_KEY = STORAGE_KEY.USER_ANONYMOUS_QUERY_COUNT
// 首次进入未选择知识库时默认展示的知识库名称
const DEFAULT_LIBRARY_NAME = '个人知识总结'
const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const { isLoggedIn, userInfo } = storeToRefs(userStore)
const darkMode = ref(window.localStorage.getItem(STORAGE_KEY.USER_DARK_MODE) === 'true')
const mobileSidebarOpen = ref(false)
const sidebarCollapsed = ref(window.localStorage.getItem(STORAGE_KEY.USER_SIDEBAR_COLLAPSED) === 'true')
const sourcePanelOpen = ref(false)
const searchQuery = ref('')
const activeLibrary = ref('')
const inputText = ref('')
const libraries = ref<UserLibrary[]>([])
const libraryError = ref('')
const anonymousLimitDialogVisible = ref(false)
const libraryExpanded = ref(true)
// 联网搜索留给用户选择（会引入外部资料，需用户知情）；HyDE 是知识库级策略，由管理端配置
const useWebSearch = ref(false)
const librarySwitchNotice = ref('')
let libraryNoticeTimer: number | undefined
const {
  isLoading: isSending,
  error: queryError,
  sources: querySources,
  submit: submitQuery,
  reset: resetQuery,
  restore: restoreQuery,
} = useUserQuery()
const sessionError = ref('')
const currentSessionId = ref<string | null>(null)
const selectedHistoryItemId = ref<string | null>(null)
const querySessions = ref<QuerySession[]>([])

const messages = ref<UserMessage[]>([])

const selectedLibrary = computed(() => libraries.value.find((item) => item.name === activeLibrary.value) || null)
const activeLibraryName = computed(() => selectedLibrary.value?.name || '未选择知识库')
const queryContext = useQueryContext(() => selectedLibrary.value?.id)
const sources = computed<UserSource[]>(() => normalizeSources(querySources.value))
const conversations = computed<UserConversation[]>(() =>
  querySessions.value
    .slice()
    .sort((left, right) => right.updated_at - left.updated_at)
    .map((session) => ({
      id: session.id,
      libraryId: session.library_id,
      title: session.title || '新建技术咨询',
      time: formatTimestamp(session.updated_at),
      group: getHistoryGroup(session.updated_at),
      active: session.id === currentSessionId.value,
    })),
)
const queryMessage = computed(() => queryError.value || libraryError.value || sessionError.value)
const displayName = computed(() => userInfo.value?.display_name || userInfo.value?.username || '访客')
const roleLabel = computed(() => (isLoggedIn.value ? '已登录用户' : '匿名体验用户'))
const avatarText = computed(() => displayName.value.trim().slice(0, 1) || '访')
// 当前会话已是空白（无消息）时不允许再新建；未登录时保持可点（引导去登录）
const canCreateConversation = computed(() => !isLoggedIn.value || messages.value.length > 0)

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

  const context = queryContext.snapshot(libraryId)
  try {
    const sessionRows = (await listQuerySessions(libraryId)).sort((left, right) => right.updated_at - left.updated_at)
    if (!queryContext.isCurrent(context, libraryId) || !isLoggedIn.value) return

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
    if (!queryContext.isCurrent(context, libraryId) || !isLoggedIn.value) return
    currentSessionId.value = result.session_id
    messages.value = result.messages.map(toUserMessage)
    selectedHistoryItemId.value = messages.value.filter((message) => message.role === 'user').at(-1)?.id || null
    const lastUserMessage = [...result.messages].reverse().find((message) => message.role === 'user')
    const lastAssistantMessage = [...result.messages].reverse().find((message) => message.role === 'assistant')
    restoreQuery({
      library_id: result.library_id,
      session_id: result.session_id,
      query: lastUserMessage?.content || '',
      answer: lastAssistantMessage?.content || '',
      sources: lastAssistantMessage?.sources || [],
      image_urls: lastAssistantMessage?.image_urls || [],
    })
  } catch (error) {
    if (!queryContext.isCurrent(context, libraryId)) return
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
  const context = queryContext.snapshot(libraryId)
  sessionError.value = ''
  try {
    const result = await getQuerySessionMessages(item.id)
    if (!queryContext.isCurrent(context, libraryId) || !isLoggedIn.value) return
    currentSessionId.value = result.session_id
    messages.value = result.messages.map(toUserMessage)
    selectedHistoryItemId.value = messages.value.filter((message) => message.role === 'user').at(-1)?.id || null
    const lastUserMessage = [...result.messages].reverse().find((message) => message.role === 'user')
    const lastAssistantMessage = [...result.messages].reverse().find((message) => message.role === 'assistant')
    restoreQuery({
      library_id: result.library_id,
      session_id: result.session_id,
      query: lastUserMessage?.content || '',
      answer: lastAssistantMessage?.content || '',
      sources: lastAssistantMessage?.sources || [],
      image_urls: lastAssistantMessage?.image_urls || [],
    })
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
    color: library.cover_color || 'var(--primary)',
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
      const defaultLibrary = libraries.value.find((library) => library.name === DEFAULT_LIBRARY_NAME)
      const target = defaultLibrary || libraries.value[0]
      activeLibrary.value = target.name
      libraryError.value = ''
      showLibraryNotice(`当前未选择知识库，将展示默认知识库“${target.name}”`)
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
  // 当前会话已经是空白的新建咨询，再点无效；只有当前会话有内容才允许新建
  if (messages.value.length === 0) return
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
    querySessions.value = querySessions.value.map((session) => (session.id === updated.id ? updated : session))
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

/** 切换侧栏收起/展开（仅桌面端生效），结果持久化以便下次进入保持选择。 */
function toggleSidebarCollapse() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  window.localStorage.setItem(STORAGE_KEY.USER_SIDEBAR_COLLAPSED, String(sidebarCollapsed.value))
}

/** 批量删除已选会话：逐个调后端删除接口；部分失败时已删的仍同步移出列表，再提示错误。 */
async function batchDeleteConversations(items: UserConversation[]) {
  if (items.length === 0) return
  if (!window.confirm(`确定删除选中的 ${items.length} 个会话吗？删除后不可恢复。`)) return
  const deletedIds = new Set<string>()
  try {
    for (const item of items) {
      await deleteQuerySession(item.id)
      deletedIds.add(item.id)
    }
  } catch (error) {
    sessionError.value = error instanceof Error ? error.message : '批量删除会话失败，请稍后重试'
  }
  querySessions.value = querySessions.value.filter((session) => !deletedIds.has(session.id))
  if (currentSessionId.value && deletedIds.has(currentSessionId.value)) {
    invalidateQueryContext()
    currentSessionId.value = null
    selectedHistoryItemId.value = null
    await loadConversationsForCurrentLibrary()
  }
}

/** 在顶部展示一条会自动消失的知识库提示。 */
function showLibraryNotice(text: string) {
  librarySwitchNotice.value = text
  if (libraryNoticeTimer) window.clearTimeout(libraryNoticeTimer)
  libraryNoticeTimer = window.setTimeout(() => {
    librarySwitchNotice.value = ''
  }, 2800)
}

function selectLibrary(name: string) {
  mobileSidebarOpen.value = false
  if (name === activeLibrary.value) return
  invalidateQueryContext()
  activeLibrary.value = name
  libraryError.value = ''
  showLibraryNotice(`已切换到“${name}”，正在加载连续聊天记录`)
  void loadConversationsForCurrentLibrary()
}

/** 使当前问答上下文失效：重置流式请求并清空对话与输入。 */
function invalidateQueryContext() {
  queryContext.invalidate()
  resetQuery()
  messages.value = []
  inputText.value = ''
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
  const context = queryContext.snapshot(libraryId)
  try {
    const result = await submitQuery(
      {
        library_id: libraryId,
        query: text,
        session_id: currentSessionId.value || undefined,
        top_k: 5,
        use_web_search: useWebSearch.value,
      },
      (delta) => {
        if (!queryContext.isCurrent(context, libraryId)) return
        updateAssistantMessage(assistantMessageId, {
          content: `${messages.value.find((message) => message.id === assistantMessageId)?.content || ''}${delta}`,
        })
      },
    )
    if (!result || !queryContext.isCurrent(context, libraryId)) return
    currentSessionId.value = result.session_id || currentSessionId.value
    updateAssistantMessage(assistantMessageId, { content: result.answer, imageUrls: result.image_urls })
    if (result.session_id) {
      // 首条消息发送成功后自动重命名会话（取用户消息前 10 字）
      const currentSession = querySessions.value.find((s) => s.id === result.session_id)
      if (currentSession && (!currentSession.title || currentSession.title === '新建技术咨询')) {
        const autoTitle = text.length > 10 ? text.slice(0, 10) + '...' : text
        try {
          await renameQuerySession(result.session_id, autoTitle)
        } catch {}
      }
      try {
        querySessions.value = (await listQuerySessions(libraryId)).sort(
          (left, right) => right.updated_at - left.updated_at,
        )
      } catch {}
    }
  } catch (error) {
    if (!queryContext.isCurrent(context, libraryId)) return
    inputText.value = text
    updateAssistantMessage(assistantMessageId, {
      content: error instanceof Error ? error.message : '查询失败，请稍后重试',
    })
  } finally {
    if (!queryContext.isCurrent(context, libraryId)) return
    updateAssistantMessage(assistantMessageId, { streaming: false })
  }
}

function updateAssistantMessage(messageId: string, updates: Partial<UserMessage>) {
  const messageIndex = messages.value.findIndex((message) => message.id === messageId)
  if (messageIndex < 0) return
  messages.value[messageIndex] = { ...messages.value[messageIndex], ...updates }
}

function toggleTheme() {
  darkMode.value = !darkMode.value
  window.localStorage.setItem(STORAGE_KEY.USER_DARK_MODE, String(darkMode.value))
}
function useSuggestion(text: string) {
  inputText.value = text
}

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
    <UserSidebar
      :mobile-open="mobileSidebarOpen"
      :collapsed="sidebarCollapsed"
      :libraries="libraries"
      :active-library="activeLibrary"
      :conversations="conversations"
      :search-query="searchQuery"
      :library-expanded="libraryExpanded"
      :display-name="displayName"
      :role-label="roleLabel"
      :avatar-text="avatarText"
      :create-disabled="!canCreateConversation"
      @create-conversation="createConversation"
      @select-library="selectLibrary"
      @select-conversation="selectConversation"
      @rename-conversation="renameConversation"
      @delete-conversation="deleteConversation"
      @batch-delete-conversations="batchDeleteConversations"
      @update:search-query="searchQuery = $event"
      @close-mobile="mobileSidebarOpen = false"
      @toggle-collapse="toggleSidebarCollapse"
      @toggle-library="libraryExpanded = !libraryExpanded"
    />
    <div v-if="mobileSidebarOpen" class="mobile-backdrop" @click="mobileSidebarOpen = false"></div>
    <main class="workspace">
      <div v-if="librarySwitchNotice" class="library-switch-toast" role="status">{{ librarySwitchNotice }}</div>
      <UserTopbar
        :dark-mode="darkMode"
        :active-library="activeLibraryName"
        :live-data="libraries.length > 0"
        :logged-in="isLoggedIn"
        :display-name="displayName"
        :role-label="roleLabel"
        :avatar-text="avatarText"
        @open-menu="mobileSidebarOpen = true"
        @toggle-theme="toggleTheme"
        @login="goToLogin"
        @logout="logout"
      />
      <div class="content-grid" :class="{ 'has-sources': sourcePanelOpen }">
        <UserChatPanel
          :messages="messages"
          :input-text="inputText"
          :is-sending="isSending"
          :source-count="sources.length"
          :use-web-search="useWebSearch"
          @toggle-sources="sourcePanelOpen = !sourcePanelOpen"
          @update:input-text="inputText = $event"
          @update:use-web-search="useWebSearch = $event"
          @send="sendMessage"
          @suggestion="useSuggestion"
        /><UserSourcePanel :open="sourcePanelOpen" :sources="sources" @close="sourcePanelOpen = false" />
      </div>
      <p v-if="queryMessage" class="user-query-error">{{ queryMessage }}</p>
    </main>
    <t-dialog
      v-model:visible="anonymousLimitDialogVisible"
      header="登录后继续咨询"
      confirm-btn="去登录"
      cancel-btn="暂不登录"
      @confirm="confirmLoginAfterLimit"
    >
      <p>匿名体验已使用 3 条消息。登录后可继续使用知识库问答功能。</p>
    </t-dialog>
  </UserLayout>
</template>
<style scoped>
.library-switch-toast {
  /* 相对整个页面顶部居中，而非只在右侧工作区（topbar）内居中 */
  position: fixed;
  top: 12px;
  left: 50%;
  z-index: 30;
  transform: translateX(-50%);
  padding: 8px 14px;
  border: 1px solid var(--primary-border);
  border-radius: 6px;
  background: var(--primary-weak);
  color: var(--primary-deep);
  box-shadow: var(--shadow-glow);
  font-size: 12px;
}
</style>
