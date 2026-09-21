<!-- 文档详情弹窗，展示元数据、标签和当前已有的 Chunk。 -->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { marked } from 'marked'
import { mysqlApi, type Document, type DocumentChunk } from '../../../api/mysql'
import { formatFileSize } from '../../../utils/format'
import { formatDocumentStatus } from '../shared/formatters'

const props = defineProps<{ visible: boolean; documentId?: number }>()
const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'updated'): void
  (event: 'error', message: string): void
}>()

const detail = ref<Document>()
const chunks = ref<DocumentChunk[]>([])
const loading = ref(false)
const savingTitle = ref(false)
const savingTags = ref(false)
const errorMessage = ref('')
const titleInput = ref('')
const tagOptions = ref<Array<{ label: string; value: string }>>([])
const selectedTags = ref<string[]>([])
const tagSearch = ref('')
const chunkSearch = ref('')
const expandedChunkIds = ref<number[]>([])

const CHUNK_PREVIEW_LENGTH = 420
const imageMetadataKeys = ['image_url', 'image_urls', 'image_path', 'image_paths', 'image_source', 'image_sources']

const filteredChunks = computed(() => {
  const keyword = chunkSearch.value.trim().toLocaleLowerCase()
  if (!keyword) return chunks.value

  return chunks.value.filter((chunk) => {
    const searchText = [
      String(chunk.chunk_index),
      chunk.section_title || '',
      chunk.content,
      ...Object.values(chunk.metadata || {}).map(metadataValueText),
    ].join(' ').toLocaleLowerCase()
    return searchText.includes(keyword)
  })
})

const allFilteredChunksExpanded = computed(() => (
  filteredChunks.value.length > 0
  && filteredChunks.value.every((chunk) => isExpanded(chunk.id))
))

async function loadDetail() {
  if (!props.documentId) return
  loading.value = true
  errorMessage.value = ''
  detail.value = undefined
  chunks.value = []
  chunkSearch.value = ''
  expandedChunkIds.value = []
  try {
    detail.value = await mysqlApi.getDocument(props.documentId)
    titleInput.value = detail.value.title
    chunks.value = await mysqlApi.listChunks(props.documentId)
    selectedTags.value = detail.value.tags || []
    const tags = await mysqlApi.listTags()
    tagOptions.value = tags.map((tag) => ({ label: tag.name, value: tag.name }))
    tagSearch.value = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '文档详情加载失败'
  } finally {
    loading.value = false
  }
}

function metadataValueText(value: unknown): string {
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  if (Array.isArray(value)) return value.map(metadataValueText).filter(Boolean).join('、')
  return ''
}

function metadataText(chunk: DocumentChunk, key: string): string {
  return metadataValueText(chunk.metadata?.[key])
}

function imageSourceText(chunk: DocumentChunk): string {
  return imageMetadataKeys
    .map((key) => metadataText(chunk, key))
    .filter(Boolean)
    .join('；')
}

function isExpanded(chunkId: number): boolean {
  return expandedChunkIds.value.includes(chunkId)
}

function toggleChunk(chunkId: number) {
  expandedChunkIds.value = isExpanded(chunkId)
    ? expandedChunkIds.value.filter((id) => id !== chunkId)
    : [...expandedChunkIds.value, chunkId]
}

function toggleAllChunks() {
  if (filteredChunks.value.length && filteredChunks.value.every((chunk) => isExpanded(chunk.id))) {
    expandedChunkIds.value = expandedChunkIds.value.filter(
      (id) => !filteredChunks.value.some((chunk) => chunk.id === id),
    )
    return
  }
  expandedChunkIds.value = Array.from(new Set([
    ...expandedChunkIds.value,
    ...filteredChunks.value.map((chunk) => chunk.id),
  ]))
}

function previewContent(chunk: DocumentChunk): string {
  if (isExpanded(chunk.id) || chunk.content.length <= CHUNK_PREVIEW_LENGTH) return chunk.content
  return `${chunk.content.slice(0, CHUNK_PREVIEW_LENGTH)}…`
}

function hasExpandableContent(chunk: DocumentChunk): boolean {
  return chunk.content.length > CHUNK_PREVIEW_LENGTH
}

function renderMarkdown(content: string): string {
  return marked.parse(content, { breaks: true }) as string
}

/** 保存文档标题并通知列表刷新。 */
async function saveTitle() {
  if (!detail.value) return
  if (!titleInput.value.trim()) {
    errorMessage.value = '文档名称不能为空。'
    return
  }

  errorMessage.value = ''
  savingTitle.value = true
  try {
    const updated = await mysqlApi.updateDocument(detail.value.id, {
      title: titleInput.value.trim(),
    })
    detail.value = { ...detail.value, ...updated }
    emit('updated')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '文档名称保存失败'
  } finally {
    savingTitle.value = false
  }
}

/** 记录标签选择器输入，用于提示尚未创建的标签。 */
function handleTagInput(value: string) {
  tagSearch.value = value
}

/** 将首次输入的标签加入选中项，保存时由后端完成标签创建。 */
function handleTagCreate(value: string) {
  const name = value.trim()
  if (!name || selectedTags.value.includes(name)) return
  selectedTags.value = [...selectedTags.value, name]
  tagSearch.value = ''
}

function isUnknownTag(value: string) {
  const name = value.trim()
  return Boolean(name) && !tagOptions.value.some((option) => option.value === name)
}

async function saveTags() {
  if (!detail.value) return

  const names = Array.from(new Set([
    ...selectedTags.value,
    tagSearch.value,
  ].map((item) => item.trim()).filter(Boolean)))
  if (names.length > 30) {
    errorMessage.value = '每个文档最多可关联 30 个标签。'
    return
  }

  errorMessage.value = ''
  savingTags.value = true
  try {
    selectedTags.value = names
    detail.value = await mysqlApi.updateDocumentTags(detail.value.id, names)
    const tags = await mysqlApi.listTags()
    tagOptions.value = tags.map((tag) => ({ label: tag.name, value: tag.name }))
    emit('updated')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签保存失败'
  } finally {
    savingTags.value = false
  }
}

watch(
  () => [props.visible, props.documentId],
  ([visible]) => {
    if (visible) void loadDetail()
  },
)
</script>

<template>
  <t-dialog
    :visible="visible"
    header="文档详情"
    width="760px"
    :footer="false"
    @update:visible="emit('update:visible', $event)"
  >
    <div v-if="loading" class="admin-loading">正在加载...</div>
    <div v-else>
      <div v-if="errorMessage" class="dialog-form-error">{{ errorMessage }}</div>
      <template v-if="detail">
      <div class="detail-title-row">
        <t-input v-model="titleInput" label="文档名称" />
        <t-button
          theme="primary"
          variant="outline"
          :loading="savingTitle"
          @click="saveTitle"
        >
          保存名称
        </t-button>
      </div>
      <p class="detail-filename">原始文件：{{ detail.original_filename }}</p>
      <p class="detail-meta">
        状态：{{ formatDocumentStatus(detail.status) }}　大小：
        {{ formatFileSize(detail.file_size) }}
      </p>
      <div class="dialog-fields">
        <p v-if="isUnknownTag(tagSearch)" class="tag-create-hint">
          该标签未被创建, 使用该标签会自动创建标签
        </p>
        <t-select
          v-model="selectedTags"
          :options="tagOptions"
          multiple
          filterable
          creatable
          placeholder="选中或者输入标签"
          @input-change="handleTagInput"
          @create="handleTagCreate"
        />
        <t-button theme="primary" variant="outline" :loading="savingTags" @click="saveTags">
          保存标签
        </t-button>
      </div>
      <div class="chunk-section-header">
        <div>
          <h3>导入 Chunk（{{ chunks.length }}）</h3>
          <p>查看最终入库的切分结果与来源信息。</p>
        </div>
        <t-button
          size="small"
          variant="outline"
          :disabled="!filteredChunks.length"
          @click="toggleAllChunks"
        >
          {{ allFilteredChunksExpanded ? '全部收起' : '全部展开' }}
        </t-button>
      </div>
      <t-input
        v-model="chunkSearch"
        class="chunk-search"
        clearable
        placeholder="搜索序号、标题、内容、文件或实体"
      />
      <p v-if="chunkSearch" class="chunk-search-result">
        找到 {{ filteredChunks.length }} / {{ chunks.length }} 个 Chunk
      </p>
      <div class="chunk-list">
        <article v-for="chunk in filteredChunks" :key="chunk.id" class="chunk-card">
          <header class="chunk-card-header">
            <div>
              <strong>Chunk #{{ chunk.chunk_index }}</strong>
              <span>第 {{ chunk.page_number || '-' }} 页</span>
              <span>{{ chunk.content.length }} 字符</span>
            </div>
            <span v-if="metadataText(chunk, 'entity_recognition_mode')" class="chunk-mode">
              {{ metadataText(chunk, 'entity_recognition_mode') }}
            </span>
          </header>
          <div v-if="chunk.section_title || metadataText(chunk, 'parent_title')" class="chunk-heading">
            <strong v-if="metadataText(chunk, 'parent_title')">{{ metadataText(chunk, 'parent_title') }}</strong>
            <span v-if="chunk.section_title">{{ chunk.section_title }}</span>
          </div>
          <dl class="chunk-metadata">
            <template v-if="metadataText(chunk, 'file_title') || metadataText(chunk, 'file_name')">
              <dt>来源文件</dt>
              <dd>{{ metadataText(chunk, 'file_title') || metadataText(chunk, 'file_name') }}</dd>
            </template>
            <template v-if="metadataText(chunk, 'item_names') || metadataText(chunk, 'item_name')">
              <dt>识别实体</dt>
              <dd>{{ metadataText(chunk, 'item_names') || metadataText(chunk, 'item_name') }}</dd>
            </template>
            <template v-if="imageSourceText(chunk)">
              <dt>图片来源</dt>
              <dd>{{ imageSourceText(chunk) }}</dd>
            </template>
          </dl>
          <div class="chunk-content chunk-markdown" v-html="renderMarkdown(previewContent(chunk))"></div>
          <div class="chunk-card-footer">
            <small>向量 ID：{{ chunk.vector_id || '未生成' }}</small>
            <t-button
              v-if="hasExpandableContent(chunk)"
              size="small"
              variant="text"
              @click="toggleChunk(chunk.id)"
            >
              {{ isExpanded(chunk.id) ? '收起全文' : '展开全文' }}
            </t-button>
          </div>
        </article>
        <p v-if="!chunks.length" class="empty-state">暂无切分内容</p>
        <p v-else-if="!filteredChunks.length" class="empty-state">没有匹配的 Chunk，请调整搜索条件。</p>
      </div>
      </template>
    </div>
  </t-dialog>
</template>

<style scoped>
.detail-title-row { display: flex; align-items: flex-end; gap: 10px; margin-bottom: 8px; }
.detail-title-row .t-input { min-width: 0; flex: 1; }
.detail-title-row .t-button { flex: 0 0 auto; }
.detail-filename { color: #6b7280; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.detail-meta { color: #6b7280; font-size: 12px; word-break: break-all; }
.chunk-section-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin-top: 22px; }
.chunk-section-header h3 { margin: 0; }
.chunk-section-header p { margin: 5px 0 0; color: #6b7280; font-size: 12px; }
.chunk-search { margin: 12px 0 6px; }
.chunk-search-result { margin: 0 0 8px; color: #6b7280; font-size: 12px; }
.chunk-list { display: grid; gap: 10px; max-height: 430px; overflow-y: auto; padding-right: 2px; }
.chunk-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px; background: #fff; }
.chunk-card-header, .chunk-card-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.chunk-card-header > div { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.chunk-card-header span, .chunk-card-footer small { color: #6b7280; font-size: 11px; }
.chunk-mode { padding: 2px 7px; border-radius: 999px; background: #f1f5f9; color: #475569 !important; white-space: nowrap; }
.chunk-heading { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; color: #374151; font-size: 12px; }
.chunk-heading span::before { content: '/'; margin-right: 7px; color: #9ca3af; }
.chunk-metadata { display: grid; grid-template-columns: auto 1fr; gap: 4px 8px; margin: 9px 0 0; color: #6b7280; font-size: 11px; }
.chunk-metadata dt { color: #9ca3af; }
.chunk-metadata dd { min-width: 0; margin: 0; overflow-wrap: anywhere; }
.chunk-content { margin: 10px 0; color: #374151; font-size: 12px; line-height: 1.75; overflow-wrap: anywhere; }.chunk-markdown :deep(p) { margin: 0 0 8px; }.chunk-markdown :deep(h1), .chunk-markdown :deep(h2), .chunk-markdown :deep(h3) { margin: 12px 0 6px; font-size: 14px; }.chunk-markdown :deep(ul), .chunk-markdown :deep(ol) { margin: 6px 0 8px; padding-left: 18px; }.chunk-markdown :deep(table) { width: 100%; margin: 8px 0; border-collapse: collapse; }.chunk-markdown :deep(th), .chunk-markdown :deep(td) { padding: 5px 7px; border: 1px solid #dbe2ea; text-align: left; vertical-align: top; }.chunk-markdown :deep(code) { padding: 1px 4px; border-radius: 3px; background: #f1f5f9; }.chunk-markdown :deep(pre) { overflow: auto; padding: 8px; border-radius: 4px; background: #f1f5f9; }
.empty-state { color: #9ca3af; font-size: 12px; }
@media (max-width: 560px) { .detail-title-row, .chunk-section-header { align-items: stretch; flex-direction: column; } .detail-title-row .t-button, .chunk-section-header .t-button { align-self: flex-start; } }
</style>
