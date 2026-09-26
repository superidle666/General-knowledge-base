<!--
 * @FilePath: frontend/src/components/user/UserSourcePanel.vue
 * @Date: 2026-09-07
 * @Description: 用户端来源面板，展示当前回答引用的知识库文档 Chunk 和联网资料。
 * @BusinessRule: 本地来源靠 document_id/chunk_id 溯源，联网来源靠 url 溯源，两者必须可区分。
 -->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { marked } from 'marked'
import { ChevronRightIcon, CloseIcon, FileIcon, LinkIcon } from 'tdesign-icons-vue-next'
import type { UserSource } from '../../types/user'
const props = defineProps<{ open: boolean; activeLibrary: string; sources: UserSource[]; documentCount: number; updatedAt: string | null }>()
defineEmits<{ (event: 'close'): void }>()

const webSources = computed(() => props.sources.filter((source) => source.kind === 'web'))
const webSourceCount = computed(() => webSources.value.length)

const detailVisible = ref(false)
const selectedSource = ref<UserSource | null>(null)

function openDetail(source: UserSource) {
  selectedSource.value = source
  detailVisible.value = true
}

function renderMarkdown(content: string) {
  return marked.parse(content, { breaks: true }) as string
}
</script>
<template>
  <aside v-if="open" class="source-panel"><div class="source-head"><div><div class="eyebrow">检索上下文</div><h2>回答来源</h2></div><t-button variant="text" shape="square" title="关闭来源" @click="$emit('close')"><CloseIcon /></t-button></div><div class="source-summary"><span class="summary-icon">✓</span><div><strong>已找到 {{ sources.length }} 个相关来源</strong><p>{{ webSourceCount ? '回答内容融合了本地知识库与联网资料' : '回答内容经过本地知识库检索' }}</p></div></div><div v-if="sources.length" class="source-list"><article v-for="source in sources" :key="source.id" class="source-card"><div class="source-card-top"><div class="file-icon" :style="{ background: source.color + '16', color: source.color }"><FileIcon /></div><span class="score" title="检索相关性：该来源与问题文本的匹配度（相对本次查询），不代表答案正确概率">{{ source.relevanceLabel }}</span><span class="source-kind" :class="source.kind === 'web' ? 'is-web' : 'is-local'">{{ source.kind === 'web' ? '联网' : '知识库' }}</span></div><h3>{{ source.title }}</h3><p>{{ source.type }}</p><button @click="openDetail(source)">{{ source.kind === 'web' ? '查看摘要' : '查看文档' }} <ChevronRightIcon /></button></article></div><div v-else class="online-empty"><FileIcon /><p>提交问题后，这里会显示相关文档来源</p></div><div class="online-section"><div class="section-heading"><span>联网资料</span><span class="optional">{{ webSourceCount ? `本次引用 ${webSourceCount} 条` : '本次未引用' }}</span></div><div v-if="webSourceCount" class="online-list"><a v-for="source in webSources" :key="source.id" class="online-item" :href="source.url || '#'" target="_blank" rel="noopener noreferrer"><LinkIcon /><span>{{ source.title }}</span></a></div><div v-else class="online-empty"><LinkIcon /><p>勾选输入框下方的“联网搜索”后，这里会显示外部来源</p></div></div><div class="library-status"><div class="section-heading"><span>当前知识库</span><span class="status-ok">正常</span></div><div class="status-row"><span class="library-dot"></span><strong>{{ activeLibrary }}</strong><span class="doc-count">{{ documentCount }} 份文档</span></div><div class="status-progress"><span></span></div><p>{{ updatedAt ? `最后更新于 ${new Date(updatedAt).toLocaleString('zh-CN')}` : '暂无更新时间' }}</p></div></aside>
  <t-dialog v-model:visible="detailVisible" :header="selectedSource?.title || '来源详情'" :footer="false" width="min(760px, calc(100vw - 32px))">
    <div v-if="selectedSource" class="source-detail"><div class="source-detail-meta"><div><span>{{ selectedSource.kind === 'web' ? '页面标题' : '文档标题' }}</span><strong>{{ selectedSource.title }}</strong></div><div><span>检索相关性</span><strong>{{ selectedSource.relevanceLabel }}</strong><small class="rel-note">与问题文本的匹配度（相对本次查询），非答案正确概率；原始相关度 {{ selectedSource.score }}</small></div><div v-if="selectedSource.kind === 'web'"><span>站点</span><strong>{{ selectedSource.siteName || '未提供' }}</strong></div><div v-if="selectedSource.kind === 'web'"><span>原文链接</span><strong><a :href="selectedSource.url || '#'" target="_blank" rel="noopener noreferrer">{{ selectedSource.url || '未提供' }}</a></strong></div><div v-if="selectedSource.kind !== 'web'"><span>页码</span><strong>{{ selectedSource.pageNumber ?? '未提供' }}</strong></div><div v-if="selectedSource.kind !== 'web'"><span>Chunk 序号</span><strong>{{ selectedSource.chunkIndex ?? '未提供' }}</strong></div><div v-if="selectedSource.kind !== 'web'"><span>章节标题</span><strong>{{ selectedSource.sectionTitle || '未提供' }}</strong></div><div v-if="selectedSource.kind !== 'web'"><span>父级标题</span><strong>{{ selectedSource.parentTitle || '未提供' }}</strong></div><div v-if="selectedSource.kind !== 'web'"><span>文档 ID</span><strong>{{ selectedSource.documentId ?? '未提供' }}</strong></div><div v-if="selectedSource.kind !== 'web'"><span>Chunk ID</span><strong>{{ selectedSource.chunkId ?? '未提供' }}</strong></div></div><div class="source-detail-content"><span>{{ selectedSource.kind === 'web' ? '搜索引擎返回的摘要（非页面全文）' : '完整 Chunk 内容' }}</span><div class="source-markdown-content" v-html="renderMarkdown(selectedSource.content)"></div></div></div>
  </t-dialog>
</template>

<style scoped>
.source-kind { flex: none; padding: 1px 6px; border-radius: 3px; font-size: 11px; line-height: 18px; }
.source-kind.is-local { background: #eef2ff; color: #4338ca; }
.source-kind.is-web { background: #ecfdf5; color: #047857; }
.online-list { display: grid; gap: 6px; margin: 8px 0 0; }
.online-item { display: flex; align-items: center; gap: 6px; overflow: hidden; color: #1d4ed8; font-size: 12px; text-decoration: none; }
.online-item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.online-item:hover { text-decoration: underline; }
.source-detail-meta a { color: #1d4ed8; overflow-wrap: anywhere; text-decoration: none; }
.rel-note { color: #94a3b8; font-size: 11px; font-weight: 400; line-height: 1.5; }
.source-detail-meta a:hover { text-decoration: underline; }
.source-detail { display: grid; gap: 20px; }.source-detail-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }.source-detail-meta > div { min-width: 0; display: grid; gap: 4px; padding: 10px 12px; border: 1px solid #e5e7eb; border-radius: 6px; background: #f8fafc; }.source-detail span { color: #6b7280; font-size: 12px; }.source-detail strong { overflow-wrap: anywhere; color: #1f2937; font-size: 13px; font-weight: 600; }.source-detail-content { display: grid; gap: 8px; }.source-markdown-content { box-sizing: border-box; max-height: 360px; overflow: auto; padding: 14px; border-radius: 6px; background: #f8fafc; color: #1f2937; font-size: 13px; line-height: 1.75; overflow-wrap: anywhere; }.source-markdown-content :deep(p) { margin: 0 0 12px; }.source-markdown-content :deep(h1), .source-markdown-content :deep(h2), .source-markdown-content :deep(h3) { margin: 16px 0 8px; font-size: 15px; }.source-markdown-content :deep(h1:first-child), .source-markdown-content :deep(h2:first-child), .source-markdown-content :deep(h3:first-child) { margin-top: 0; }.source-markdown-content :deep(ul), .source-markdown-content :deep(ol) { margin: 8px 0 12px; padding-left: 22px; }.source-markdown-content :deep(table) { width: 100%; border-collapse: collapse; margin: 12px 0; }.source-markdown-content :deep(th), .source-markdown-content :deep(td) { padding: 6px 8px; border: 1px solid #dbe2ea; text-align: left; vertical-align: top; }.source-markdown-content :deep(code) { padding: 1px 4px; border-radius: 3px; background: #e9eef5; }.source-markdown-content :deep(pre) { overflow: auto; padding: 10px; border-radius: 4px; background: #e9eef5; } @media (max-width: 560px) { .source-detail-meta { grid-template-columns: 1fr; } }
</style>
