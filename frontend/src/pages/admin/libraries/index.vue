<!--
 * @FilePath: @/pages/admin/libraries/index.vue
 * @Author: 项目维护者
 * @Date: 2026-09-28
 * @Description: 知识库列表页：卡片网格 + 新增弹窗 + 卡片快捷操作，点击进入详情页管理文档与配置。
 * @BusinessRule: 旧深链 ?libraryId=&upload=1 在本页转跳详情页消费；删除知识库需先清空文档；创建成功自动进入新库详情。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { AddIcon, FileIcon, SearchIcon } from 'tdesign-icons-vue-next'
import { MessagePlugin } from 'tdesign-vue-next'
import { mysqlApi, type Library } from '../../../api/mysql'
import { USER_ROLE } from '../../../constants'
import { useUserStore } from '../../../store/modules/user'
import { useAdminCountsStore } from '../../../store/modules/adminCounts'
import { useRequest } from '../../../composables'
import KbCard from './KbCard.vue'
import {
  ALL_ENTITY_MODE_OPTIONS,
  FILE_TYPE_OPTIONS,
  VISIBILITY_OPTIONS,
  emptyLibraryForm,
  ensureAllowedFileType,
  libraryFormToPayload,
  normalizeMaxFileSize,
  validateLibraryForm,
} from './libraryForm'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const countsStore = useAdminCountsStore()

const search = ref('')
const creating = ref(false)
const showCreateDialog = ref(false)
const createDialogError = ref('')
const createForm = ref(emptyLibraryForm())
const mutationError = ref('')

const isSystemAdmin = computed(() => userStore.userInfo?.roles.includes(USER_ROLE.ADMIN) ?? false)
const entityModeOptions = computed(() => (isSystemAdmin.value ? ALL_ENTITY_MODE_OPTIONS : [ALL_ENTITY_MODE_OPTIONS[0]]))

/** 加载知识库列表；错误统一由 errorMessage 展示。 */
const librariesRequest = useRequest(async () => {
  return mysqlApi.listLibraries(true, false)
})
const libraries = computed(() => librariesRequest.data.value ?? [])
const loading = librariesRequest.loading
const errorMessage = computed(() => mutationError.value || librariesRequest.error.value?.message || '')

const visibleLibraries = computed(() => libraries.value.filter((library) => !library.deleted_at))
const filteredLibraries = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return visibleLibraries.value
  return visibleLibraries.value.filter((library) =>
    `${library.name} ${library.code} ${library.description || ''}`.toLowerCase().includes(query),
  )
})
const activeLibraries = computed(() => visibleLibraries.value.filter((library) => library.status === 'active'))
const totalDocuments = computed(() =>
  activeLibraries.value.reduce((total, library) => total + library.document_count, 0),
)

/** 重新加载知识库列表，并把文档总数写入侧栏计数（写入点收敛于本页）。 */
async function loadLibraries() {
  mutationError.value = ''
  try {
    await librariesRequest.execute()
  } catch {
    // 加载错误通过 librariesRequest.error 在模板中展示
    return
  }
  countsStore.updateCounts({ documents: totalDocuments.value })
}

function goDetail(library: Library, tab?: 'config') {
  void router.push({
    name: 'admin-library-detail',
    params: { libraryId: String(library.id) },
    query: tab ? { tab } : {},
  })
}

// 旧深链兼容：/admin/documents?libraryId=2&upload=1 redirect 到本页后转跳详情页，
// 同时认历史 key library；upload 一次性信号原样转交，由详情页消费后剥离。
watch(
  () => route.query,
  (query) => {
    const id = Number(query.libraryId ?? query.library) || 0
    if (!id) return
    void router.replace({
      name: 'admin-library-detail',
      params: { libraryId: String(id) },
      query: query.upload === '1' ? { upload: '1' } : {},
    })
  },
  { immediate: true },
)

// ---- 新增知识库 ----

function openCreateDialog() {
  createForm.value = emptyLibraryForm()
  createDialogError.value = ''
  showCreateDialog.value = true
}

/** 文件类型清空兜底：恢复默认并展示/清除提示。 */
function handleCreateFileTypeChange() {
  const message = ensureAllowedFileType(createForm.value)
  if (message) {
    createDialogError.value = message
    return
  }
  if (createDialogError.value === '至少选择一个文件类型。') createDialogError.value = ''
}

async function createLibrary() {
  const form = createForm.value
  normalizeMaxFileSize(form)
  const validationError = validateLibraryForm(form)
  if (validationError) {
    createDialogError.value = validationError
    return
  }
  creating.value = true
  createDialogError.value = ''
  try {
    const library = await mysqlApi.createLibrary(libraryFormToPayload(form, { isAdmin: isSystemAdmin.value }))
    createForm.value = emptyLibraryForm()
    showCreateDialog.value = false
    await loadLibraries()
    await MessagePlugin.success('知识库新增成功。')
    // 创建后直接进入新库详情，替代改版前"右侧表单预选"的衔接语义。
    goDetail(library)
  } catch (error) {
    createDialogError.value = error instanceof Error ? error.message : '知识库创建失败'
  } finally {
    creating.value = false
  }
}

// ---- 卡片快捷操作 ----

async function toggleLibraryStatus(library: Library) {
  mutationError.value = ''
  try {
    await mysqlApi.updateLibrary(library.id, { status: library.status === 'active' ? 'disabled' : 'active' })
    const nextStatus = library.status === 'active' ? '停用' : '启用'
    await loadLibraries()
    await MessagePlugin.success(`知识库已${nextStatus}。`)
  } catch (error) {
    mutationError.value = error instanceof Error ? error.message : '知识库状态更新失败'
  }
}

async function deleteLibrary(library: Library) {
  if (library.document_count > 0) {
    await MessagePlugin.warning('请先删除该知识库文档后再删除知识库。')
    return
  }
  if (!window.confirm(`确定删除知识库“${library.name}”吗？删除后将不再在管理端列表展示。`)) return
  mutationError.value = ''
  try {
    await mysqlApi.deleteLibrary(library.id)
    await loadLibraries()
    await MessagePlugin.success('知识库删除成功。')
  } catch (error) {
    mutationError.value = error instanceof Error ? error.message : '知识库删除失败'
  }
}

async function handleCardCommand(command: 'edit' | 'toggle-status' | 'delete', library: Library) {
  if (command === 'edit') {
    goDetail(library, 'config')
    return
  }
  if (command === 'toggle-status') {
    await toggleLibraryStatus(library)
    return
  }
  await deleteLibrary(library)
}

void loadLibraries()
</script>

<template>
  <section class="admin-module library-page">
    <div class="admin-page-head compact">
      <div>
        <div class="eyebrow">KNOWLEDGE BASES</div>
        <h1>知识库</h1>
        <p>点击知识库卡片进入详情，管理该库的文档与配置规则。</p>
      </div>
      <t-button theme="primary" class="create-action" @click="openCreateDialog"><AddIcon />新增知识库</t-button>
    </div>
    <div v-if="errorMessage" class="admin-error">{{ errorMessage }}<button @click="loadLibraries">重试</button></div>
    <div v-if="loading" class="admin-loading">正在加载知识库数据...</div>
    <div class="library-toolbar">
      <t-input v-model="search" placeholder="搜索名称、编码或描述" clearable
        ><template #prefix-icon><SearchIcon /></template
      ></t-input>
    </div>
    <div v-if="filteredLibraries.length" class="kb-card-grid">
      <KbCard
        v-for="library in filteredLibraries"
        :key="library.id"
        :library="library"
        @open="goDetail(library)"
        @command="(command) => handleCardCommand(command, library)"
      />
    </div>
    <div v-else-if="!loading" class="panel kb-empty">
      <FileIcon />
      <strong>{{ visibleLibraries.length ? '没有匹配的知识库' : '还没有知识库' }}</strong>
      <span>{{ visibleLibraries.length ? '试试更换搜索关键词。' : '创建第一个知识库，上传文档后即可开始问答。' }}</span>
      <t-button v-if="!visibleLibraries.length" theme="primary" @click="openCreateDialog"
        ><AddIcon />新增知识库</t-button
      >
    </div>
    <t-dialog v-model:visible="showCreateDialog" header="新增知识库" width="760px" :footer="false">
      <div class="library-create-panel">
        <p v-if="createDialogError" class="dialog-form-error">{{ createDialogError }}</p>
        <div class="library-section-heading">
          <strong>知识库概述</strong><span>填写名称、编码和用途，便于后续识别和维护。</span>
        </div>
        <div class="library-form-grid">
          <div class="library-field">
            <t-input v-model="createForm.name" label="知识库名称" placeholder="例如：工业设备资料" />
          </div>
          <div class="library-field">
            <t-input v-model="createForm.code" label="知识库编码" placeholder="例如：industrial-equipment" />
          </div>
          <div class="library-field">
            <t-select v-model="createForm.entityMode" :options="entityModeOptions" label="实体识别模式" />
          </div>
          <div class="library-field">
            <t-select
              v-model="createForm.allowedFileTypes"
              :options="FILE_TYPE_OPTIONS"
              label="允许文件类型"
              multiple
              clearable
              @change="handleCreateFileTypeChange"
            />
          </div>
          <div class="library-field">
            <t-select v-model="createForm.visibility" :options="VISIBILITY_OPTIONS" label="可见范围" />
          </div>
          <div class="library-field">
            <t-input-number
              class="library-rule-number"
              v-model="createForm.maxFileSizeMb"
              label="单文件大小上限（MB）"
              :min="1"
              :max="10240"
              clearable
              @blur="normalizeMaxFileSize(createForm)"
            />
          </div>
          <div class="library-field">
            <t-input-number
              class="library-rule-number"
              v-model="createForm.maxDocumentCount"
              label="知识库文档总上限"
              :min="1"
              clearable
            />
          </div>
          <div class="library-field">
            <t-input-number
              class="library-rule-number"
              v-model="createForm.maxUploadFileCount"
              label="单次上传文件上限"
              :min="1"
              clearable
            />
          </div>
          <div class="library-field">
            <t-input-number
              class="library-rule-number"
              v-model="createForm.chunkSize"
              label="切片长度（字符）"
              :min="1"
              clearable
            />
          </div>
          <div class="library-field">
            <t-input-number
              class="library-rule-number"
              v-model="createForm.chunkOverlap"
              label="切片重叠（字符）"
              :min="0"
              clearable
            />
          </div>
          <div class="library-field library-field-switch">
            <div class="library-switch-head">
              <span class="library-switch-label">HyDE 扩展召回</span>
              <t-switch v-model="createForm.useHyde" />
            </div>
            <small class="library-switch-hint"
              >先让大模型生成假设性答案再检索，可提升召回；每条问题多一次大模型调用、回答更慢，默认关闭。</small
            >
          </div>
          <div class="library-field">
            <t-textarea
              v-model="createForm.description"
              label="描述"
              placeholder="请输入知识库用途、资料范围和使用场景（可选）"
            />
          </div>
        </div>
        <div class="library-actions">
          <t-button variant="outline" @click="showCreateDialog = false">取消</t-button
          ><t-button theme="primary" :loading="creating" @click="createLibrary">创建知识库</t-button>
        </div>
      </div>
    </t-dialog>
  </section>
</template>

<style scoped>
.library-page {
  min-width: 0;
}
.library-toolbar {
  max-width: 280px;
  margin-bottom: 16px;
}
.kb-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 16px;
}
.kb-empty {
  display: grid;
  min-height: 280px;
  justify-items: center;
  align-content: center;
  gap: 10px;
  color: var(--weak);
  font-size: 12px;
  text-align: center;
}
.kb-empty strong {
  color: var(--text);
  font-size: 14px;
}
.kb-empty svg {
  width: 28px;
  color: var(--primary);
}
.library-create-panel {
  display: grid;
  gap: 14px;
  margin-bottom: 20px;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
}
.library-section-heading,
.library-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.library-section-heading strong {
  color: var(--text);
  font-size: 14px;
}
.library-section-heading span {
  color: var(--muted);
  font-size: 11px;
}
.library-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.library-actions {
  justify-content: flex-end;
}
.library-field-switch {
  display: grid;
  gap: 6px;
  align-content: start;
}
.library-switch-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.library-switch-label {
  color: var(--text);
  font-size: 14px;
  line-height: 22px;
}
.library-switch-hint {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.5;
}
.library-rule-number {
  width: 100%;
  padding: 0 28px;
}
.library-rule-number :deep(.t-input-number__decrease),
.library-rule-number :deep(.t-input-number__increase) {
  width: 28px;
  height: 32px;
  padding: 0;
  margin: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0;
  flex: 0 0 28px;
  line-height: 1;
}
.library-rule-number :deep(.t-input-number__decrease) {
  left: 0;
}
.library-rule-number :deep(.t-input-number__increase) {
  right: 0;
}
.library-rule-number :deep(.t-input-number__decrease .t-button__icon),
.library-rule-number :deep(.t-input-number__increase .t-button__icon) {
  margin: 0;
}
.library-rule-number :deep(.t-input) {
  width: 100%;
  min-width: 0;
}
.library-rule-number :deep(.t-input__inner) {
  text-align: center;
}
@media (max-width: 680px) {
  .library-form-grid {
    grid-template-columns: 1fr;
  }
  .library-section-heading,
  .library-actions {
    align-items: flex-start;
    flex-direction: column;
  }
  .library-actions {
    width: 100%;
  }
}
</style>
