<!-- 标签字典管理弹窗：维护可复用标签，不直接删除文档。 -->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { mysqlApi, type Tag } from '../../../api/mysql'
import { PAGINATION } from '../../../constants'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'updated'): void
  (event: 'error', message: string): void
}>()

const tags = ref<Tag[]>([])
const name = ref('')
const search = ref('')
const editingId = ref<number>()
const editingName = ref('')
const loading = ref(false)
const saving = ref(false)
const deletingId = ref<number>()
const errorMessage = ref('')
const tagPage = ref(1)
const tagPageSize = ref<number>(PAGINATION.PAGE_SIZE)
const filteredTags = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return tags.value
  return tags.value.filter((tag) => tag.name.toLowerCase().includes(query))
})

const visibleTags = computed(() => filteredTags.value.slice((tagPage.value - 1) * tagPageSize.value, tagPage.value * tagPageSize.value))

function handleTagPageChange(pageInfo: { current: number; pageSize: number }) {
  tagPage.value = pageInfo.current
  tagPageSize.value = pageInfo.pageSize
}

async function loadTags() {
  loading.value = true
  errorMessage.value = ''
  try {
    tags.value = await mysqlApi.listTags()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签加载失败'
  } finally {
    loading.value = false
  }
}

async function createTag() {
  const value = name.value.trim()
  if (!value) {
    errorMessage.value = '请输入标签名称。'
    return
  }

  errorMessage.value = ''
  saving.value = true
  try {
    await mysqlApi.createTag(value)
    name.value = ''
    await loadTags()
    emit('updated')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签创建失败'
  } finally {
    saving.value = false
  }
}

/** 打开标签名称编辑状态。 */
function startEdit(tag: Tag) {
  editingId.value = tag.id
  editingName.value = tag.name
}

/** 取消当前标签编辑。 */
function cancelEdit() {
  editingId.value = undefined
  editingName.value = ''
}

/** 保存标签重命名并刷新标签列表。 */
async function saveEdit() {
  if (!editingId.value) return
  if (!editingName.value.trim()) {
    errorMessage.value = '请输入标签名称。'
    return
  }

  errorMessage.value = ''
  saving.value = true
  try {
    await mysqlApi.updateTag(editingId.value, editingName.value.trim())
    cancelEdit()
    await loadTags()
    emit('updated')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签修改失败'
  } finally {
    saving.value = false
  }
}

async function deleteTag(tag: Tag) {
  if (deletingId.value) return
  if (!window.confirm(`确定删除标签“${tag.name}”吗？删除后会解除所有文档的该标签关联。`)) return

  errorMessage.value = ''
  deletingId.value = tag.id
  try {
    await mysqlApi.deleteTag(tag.id)
    await loadTags()
    emit('updated')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签删除失败'
  } finally {
    deletingId.value = undefined
  }
}

watch([search, filteredTags], () => {
  tagPage.value = 1
}, { deep: true })

watch(() => props.visible, (visible) => {
  if (visible) {
    search.value = ''
    tagPage.value = 1
    cancelEdit()
    errorMessage.value = ''
    void loadTags()
  }
})
</script>

<template>
  <t-dialog
    :visible="visible"
    header="标签管理"
    width="560px"
    :footer="false"
    @update:visible="emit('update:visible', $event)"
  >
    <p v-if="errorMessage" class="dialog-form-error">{{ errorMessage }}</p>
    <div class="tag-create-row">
      <t-input v-model="name" placeholder="输入标签名称" @enter="createTag" />
      <t-button theme="primary" :loading="saving" @click="createTag">新增标签</t-button>
    </div>
    <t-input v-model="search" placeholder="搜索标签" clearable class="tag-search-input" />
    <div v-if="loading" class="admin-loading">正在加载标签...</div>
    <div v-else class="tag-manager-list">
      <div v-for="tag in visibleTags" :key="tag.id" class="tag-manager-item">
        <t-input
          v-if="editingId === tag.id"
          v-model="editingName"
          size="small"
          class="tag-edit-input"
          @enter="saveEdit"
        />
        <t-tag v-else variant="light-outline">{{ tag.name }}</t-tag>
        <div class="tag-manager-actions">
          <template v-if="editingId === tag.id">
            <t-button variant="text" class="edit-action" size="small" :loading="saving" @click="saveEdit">保存</t-button>
            <t-button variant="text" size="small" @click="cancelEdit">取消</t-button>
          </template>
          <template v-else>
            <t-button variant="text" class="edit-action" size="small" @click="startEdit(tag)">编辑</t-button>
            <t-button
              variant="text"
              theme="danger"
              size="small"
              :loading="deletingId === tag.id"
              :disabled="Boolean(deletingId) && deletingId !== tag.id"
              @click="deleteTag(tag)"
            >删除</t-button>
          </template>
        </div>
      </div>
      <p v-if="!filteredTags.length" class="empty-state">暂无匹配标签</p>
      <div v-if="filteredTags.length" class="dialog-pagination">
        <t-pagination
          :current="tagPage"
          :page-size="tagPageSize"
          :total="filteredTags.length"
          :page-size-options="[5, 10, 20, 50]"
          @change="handleTagPageChange"
        />
      </div>
    </div>
  </t-dialog>
</template>

<style scoped>
.tag-create-row { display: flex; gap: 10px; margin-bottom: 14px; }
.tag-create-row .t-input { min-width: 0; flex: 1; }
.tag-search-input { margin-bottom: 14px; }
.tag-manager-list { display: grid; gap: 8px; max-height: min(48vh, 420px); overflow-y: auto; padding: 2px; }
.tag-manager-item { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; min-height: 40px; padding: 10px 12px; border-bottom: 1px solid #f1f5f9; }
.tag-manager-item:last-child { border-bottom: 0; }
.tag-manager-actions { display: flex; align-items: center; gap: 2px; flex: 0 0 auto; }
.tag-edit-input { min-width: 0; flex: 1; }
.empty-state { color: #9ca3af; font-size: 12px; }
.dialog-pagination { display: flex; align-items: center; justify-content: flex-end; gap: 12px; margin-top: 12px; padding-top: 12px; border-top: 1px solid #e5e7eb; color: #6b7280; font-size: 12px; }
@media (max-width: 560px) { .dialog-pagination { align-items: flex-start; flex-direction: column; } }
</style>
