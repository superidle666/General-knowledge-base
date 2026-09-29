<!--
 * @FilePath: @/pages/admin/libraries/LibraryDetail.vue
 * @Author: 项目维护者
 * @Date: 2026-09-28
 * @Description: 知识库详情页壳：头部信息 + 「文档/配置」双 Tab + 上传弹窗，对齐 RAGFlow Dataset 页组织方式。
 * @BusinessRule: 路由 /admin/libraries/:libraryId 仅接受数字 id；?tab=config 深链直达配置；?upload=1 为一次性上传信号，消费后剥离。
-->
<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChevronRightIcon, FileIcon } from 'tdesign-icons-vue-next'
import { mysqlApi, type Library } from '../../../api/mysql'
import { useRequest } from '../../../composables'
import { LIBRARY_STATUS_META, getStatusMeta } from '../shared/statusMeta'
import DocumentUploadDialog from '../documents/DocumentUploadDialog.vue'
import LibraryDocumentsTab from './LibraryDocumentsTab.vue'
import LibraryConfigTab from './LibraryConfigTab.vue'

const route = useRoute()
const router = useRouter()

const libraryId = computed(() => Number(route.params.libraryId) || 0)

const mutationError = ref('')
const showUpload = ref(false)
const docsTabRef = ref<InstanceType<typeof LibraryDocumentsTab>>()

/** 无单库详情接口，沿用列表接口前端 find。 */
const detailRequest = useRequest(async () => mysqlApi.listLibraries(true, false))
const loading = detailRequest.loading
const loadError = detailRequest.error
const library = computed<Library | undefined>(() =>
  (detailRequest.data.value ?? []).find((item) => item.id === libraryId.value && !item.deleted_at),
)
const errorMessage = computed(() => {
  if (loadError.value) return loadError.value.message
  if (!loading.value && !library.value) return '知识库不存在或已删除。'
  return ''
})

const statusMeta = computed(() => (library.value ? getStatusMeta(LIBRARY_STATUS_META, library.value.status) : null))
const visibilityLabel = computed(() => (library.value?.visibility === 'shared' ? '共享' : '私有'))
const entityModeLabel = computed(() =>
  library.value?.entity_recognition_mode === 'required' ? '强制实体识别' : '不启用实体识别',
)
const isActive = computed(() => library.value?.status === 'active')

/** 上传弹窗锁定为当前库，杜绝跨库误传。 */
const uploadLibraryOptions = computed(() => {
  if (!library.value) return []
  return [{ label: library.value.name, value: library.value.id }]
})

// ---- Tab 与 query 同步：默认 documents 不写 query，保持 URL 干净 ----
const activeTab = ref<'documents' | 'config'>(route.query.tab === 'config' ? 'config' : 'documents')
let syncingTabFromQuery = false
watch(activeTab, (tab) => {
  if (syncingTabFromQuery) return
  const nextQuery = { ...route.query }
  if (tab === 'config') nextQuery.tab = 'config'
  else delete nextQuery.tab
  void router.replace({ query: nextQuery })
})
watch(
  () => route.query.tab,
  (tab) => {
    syncingTabFromQuery = true
    activeTab.value = tab === 'config' ? 'config' : 'documents'
    void Promise.resolve().then(() => {
      syncingTabFromQuery = false
    })
  },
)

/** 深链 ?upload=1：切到文档 Tab 并打开上传弹窗，随即剥离标记（一次性消费）。 */
watch(
  () => route.query,
  (query) => {
    if (query.upload !== '1') return
    activeTab.value = 'documents'
    showUpload.value = true
    const nextQuery = { ...query }
    delete nextQuery.upload
    void router.replace({ query: nextQuery })
  },
  { immediate: true },
)

async function reload() {
  mutationError.value = ''
  try {
    await detailRequest.execute()
  } catch {
    // 加载错误通过 loadError 在模板中展示
  }
}
void reload()

function showError(message: string) {
  mutationError.value = message
}

/** 库数据变化（保存/文档增删）后刷新壳头部；库 id 未变时文档 Tab 自行重拉列表。 */
function handleChanged() {
  void reload()
  docsTabRef.value?.reload()
}

function handleDeleted() {
  void router.replace({ name: 'admin-libraries' })
}

function openUpload() {
  if (!isActive.value) return
  showUpload.value = true
}

/** 导入队列按钮在头部，弹窗与刷新逻辑在文档 Tab：先切到文档 Tab 确保挂载，再调用子组件方法。 */
async function openImportQueue() {
  activeTab.value = 'documents'
  await nextTick()
  docsTabRef.value?.openImportQueue()
}

function backToList() {
  void router.push({ name: 'admin-libraries' })
}
</script>

<template>
  <section class="admin-module library-detail-page">
    <div class="library-detail-backline">
      <t-button variant="text" size="small" @click="backToList">
        <ChevronRightIcon class="back-icon" />
        返回知识库
      </t-button>
    </div>
    <div v-if="errorMessage || mutationError" class="admin-error">
      {{ errorMessage || mutationError }}<button v-if="errorMessage" @click="reload">重试</button>
    </div>
    <div v-if="loading" class="admin-loading">正在加载知识库详情...</div>
    <template v-else-if="library">
      <div class="panel library-detail-head">
        <div class="library-icon">
          <FileIcon />
        </div>
        <div class="library-head-copy">
          <div class="library-head-title">
            <h1>{{ library.name }}</h1>
            <t-tag v-if="statusMeta" :theme="statusMeta.theme" variant="light">{{ statusMeta.label }}</t-tag>
          </div>
          <p>
            {{ library.code }} · {{ library.document_count }} 篇文档 · {{ visibilityLabel }} ·
            {{ entityModeLabel }}
          </p>
        </div>
        <div class="library-head-actions">
          <t-button theme="primary" variant="outline" @click="openImportQueue">导入队列</t-button>
          <t-button theme="primary" :disabled="!isActive" @click="openUpload"><FileIcon />上传文档</t-button>
        </div>
      </div>
      <div v-if="!isActive" class="admin-loading library-disabled-hint">知识库已停用，其文档不参与用户端检索。</div>
      <t-tabs v-model="activeTab" class="library-detail-tabs">
        <t-tab-panel value="documents" label="文档">
          <LibraryDocumentsTab v-if="library" ref="docsTabRef" :library="library" @changed="reload" />
        </t-tab-panel>
        <t-tab-panel value="config" label="配置">
          <LibraryConfigTab v-if="library" :library="library" @saved="handleChanged" @deleted="handleDeleted" />
        </t-tab-panel>
      </t-tabs>
      <DocumentUploadDialog
        v-model:visible="showUpload"
        :library-options="uploadLibraryOptions"
        :initial-library-id="library.id"
        @uploaded="handleChanged"
        @error="showError"
      />
    </template>
  </section>
</template>

<style scoped>
.library-detail-backline {
  display: flex;
  margin-bottom: 12px;
}
.library-detail-backline .back-icon {
  transform: rotate(180deg);
}
.library-detail-head {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 18px 20px;
  margin-bottom: 16px;
}
.library-icon {
  display: grid;
  width: 44px;
  height: 44px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 50%;
  background: var(--primary-soft);
  color: var(--primary);
}
.library-icon svg {
  width: 20px;
}
.library-head-copy {
  min-width: 0;
  flex: 1;
}
.library-head-title {
  display: flex;
  align-items: center;
  gap: 10px;
}
.library-head-title h1 {
  margin: 0;
  color: var(--text);
  font-size: 18px;
}
.library-head-copy p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.library-head-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
}
.library-disabled-hint {
  margin-bottom: 16px;
}
.library-detail-tabs {
  min-width: 0;
}
@media (max-width: 760px) {
  .library-detail-head {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .library-head-actions {
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
