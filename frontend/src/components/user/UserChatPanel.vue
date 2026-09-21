<!--
 * @FilePath: frontend/src/components/user/UserChatPanel.vue
 * @Date: 2026-09-07
 * @Description: 用户端问答消息列表和输入区域，支持 Markdown、图片、复制和流式回答。
 * @BusinessRule: 回答图片不参与文本复制，输入框最大高度为 200px。
 -->
<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { marked } from 'marked'
import { CopyIcon, SendIcon, UserIcon } from 'tdesign-icons-vue-next'
import { MessagePlugin } from 'tdesign-vue-next'
import type { UserMessage } from '../../types/user'

const props = defineProps<{ messages: UserMessage[]; inputText: string; isSending: boolean; sourcePanelOpen: boolean; title: string; sourceCount: number }>()
const emit = defineEmits<{ (event: 'toggle-sources'): void; (event: 'update:input-text', value: string): void; (event: 'send'): void }>()

const chatScrollRef = ref<HTMLElement | null>(null)
const inputTextareaRef = ref<HTMLTextAreaElement | null>(null)
const INPUT_MAX_HEIGHT = 200
let inputResizeTimer: number | undefined

function resizeInputTextarea() {
  if (inputResizeTimer) window.clearTimeout(inputResizeTimer)
  inputResizeTimer = window.setTimeout(() => {
    const textarea = inputTextareaRef.value
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.overflowY = 'hidden'
    const contentHeight = textarea.scrollHeight
    const nextHeight = Math.min(contentHeight, INPUT_MAX_HEIGHT)
    textarea.style.height = `${Math.max(nextHeight, 30)}px`
    if (contentHeight > INPUT_MAX_HEIGHT) textarea.style.overflowY = 'auto'
  }, 16)
}

function handleInput(event: Event) {
  emit('update:input-text', (event.target as HTMLTextAreaElement).value)
  resizeInputTextarea()
}

watch(() => props.inputText, resizeInputTextarea, { flush: 'post' })
onMounted(resizeInputTextarea)
onBeforeUnmount(() => {
  if (inputResizeTimer) window.clearTimeout(inputResizeTimer)
})

function isImageUrl(value: string) {
  return /^https?:\/\/\S+\.(?:png|jpe?g|gif|webp|bmp|svg)(?:\?\S*)?$/i.test(value.replace(/[),.;，。；）]+$/u, ''))
}

function getMessageImageUrls(message: UserMessage) {
  const inlineImageUrls = getInlineImageUrls(message.content)
  const urls = [
    ...(message.imageUrls || []),
  ]
  return [...new Set(urls.map((url) => url.trim().replace(/[),.;，。；）]+$/u, '')).filter(isImageUrl))]
    .filter((url) => !inlineImageUrls.includes(url))
}

function getInlineImageUrls(content: string) {
  const imageBlock = content.match(/【图片】\s*([\s\S]*?)$/u)?.[1] || ''
  return [...new Set([
    ...Array.from(content.matchAll(/!\[[^\]]*\]\((https?:\/\/[^\s)]+)\)/g), (match) => match[1]),
    ...imageBlock.split(/\r?\n/).map((line) => line.trim()),
  ].map((url) => url.trim().replace(/[),.;，。；）]+$/u, '')).filter(isImageUrl))]
}

function scrollToLatest() {
  void nextTick(() => {
    const container = chatScrollRef.value
    if (container) container.scrollTop = container.scrollHeight
  })
}

watch(
  () => props.messages,
  scrollToLatest,
  { deep: true, flush: 'post' },
)

function getTextContent(content: string) {
  const legacyImageBlock = content.match(/\n?【图片】\s*([\s\S]*?)$/u)
  if (!legacyImageBlock) return content.trim()
  const imageMarkdown = getInlineImageUrls(content)
    .map((url) => `![回答图片](${url})`)
    .join('\n')
  return content.replace(legacyImageBlock[0], imageMarkdown ? `\n${imageMarkdown}` : '').trim()
}

const renderMarkdown = (value: string) => marked.parse(getTextContent(value), { breaks: true }) as string

function getCopyableContent(content: string) {
  return getTextContent(content)
    .replace(/!\[[^\]]*\]\([^\)]+\)/g, '')
    .replace(/^https?:\/\/\S+\.(?:png|jpe?g|gif|webp|bmp|svg)(?:\?\S*)?\s*$/gim, '')
    .trim()
}

async function copyMessage(content: string) {
  const copyableContent = getCopyableContent(content)
  if (!copyableContent) {
    MessagePlugin.warning('没有可复制的文字内容')
    return
  }
  try {
    await navigator.clipboard.writeText(copyableContent)
    MessagePlugin.success('复制成功')
    return
  } catch {}
  try {
    const textarea = document.createElement('textarea')
    textarea.value = copyableContent
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    const copied = document.execCommand('copy')
    textarea.remove()
    if (!copied) throw new Error('copy failed')
    MessagePlugin.success('复制成功')
  } catch {
    MessagePlugin.error('复制失败，请手动复制')
  }
}
</script>
<template>
  <section class="chat-panel"><div class="chat-head"><div><div class="eyebrow">知识库咨询</div><h1>{{ title }}</h1></div><div class="chat-actions"><t-button variant="outline" size="small" @click="emit('toggle-sources')"><span class="source-toggle-label">{{ sourcePanelOpen ? '隐藏来源' : '查看来源' }}</span></t-button></div></div><div ref="chatScrollRef" class="chat-scroll"><div v-if="messages.length === 0" class="empty-chat"><div class="empty-icon">工</div><h2>开始一次新的技术咨询</h2><p>从设备型号、故障代码或操作方法开始提问。</p></div><template v-else><div v-for="message in messages" :key="message.id" class="message" :class="message.role"><div class="message-avatar" :class="message.role"><UserIcon v-if="message.role === 'user'" /><span v-else>工</span></div><div class="message-body"><div class="message-meta"><strong>{{ message.role === 'user' ? '用户' : '工智库助手' }}</strong><span>{{ message.time }}</span><span v-if="message.role === 'assistant' && sourceCount" class="source-tag">基于 {{ sourceCount }} 个来源</span></div><div v-if="message.role === 'assistant'" class="markdown-content" v-html="renderMarkdown(message.content)"></div><div v-else class="user-text">{{ message.content }}</div><div v-if="message.role === 'assistant' && getMessageImageUrls(message).length" class="assistant-images"><img v-for="imageUrl in getMessageImageUrls(message)" :key="imageUrl" :src="imageUrl" alt="回答关联图片" loading="lazy" @load="scrollToLatest" /></div><div v-if="message.streaming" class="typing-indicator"><i></i><i></i><i></i></div><div v-if="message.role === 'assistant' && !message.streaming" class="message-tools"><t-button variant="text" size="small" @click="copyMessage(message.content)"><CopyIcon />复制</t-button></div></div></div></template></div><div class="composer-wrap"><div class="composer"><textarea ref="inputTextareaRef" :value="inputText" rows="1" placeholder="输入设备型号、故障代码或你的问题..." @input="handleInput" @keydown.enter.exact.prevent="emit('send')" /><div class="composer-bottom"><div class="composer-hints"><span class="hint-text">当前仅基于本地知识库回答</span></div><t-button theme="primary" shape="square" class="send-icon-button" title="发送" :disabled="!inputText.trim() || isSending" @click="emit('send')"><SendIcon /></t-button></div></div><div class="privacy-note">工智库可能会产生不准确的信息，请结合设备手册进行确认</div></div></section>
</template>
<style scoped>
.assistant-images { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-top: 14px; }
.assistant-images img { display: block; width: 100%; max-width: 420px; max-height: 320px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); object-fit: contain; }
.markdown-content :deep(img) { display: block; max-width: min(100%, 520px); max-height: 360px; margin: 12px 0; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); object-fit: contain; }
</style>
