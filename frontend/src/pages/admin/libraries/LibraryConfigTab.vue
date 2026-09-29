<!--
 * @FilePath: @/pages/admin/libraries/LibraryConfigTab.vue
 * @Author: 项目维护者
 * @Date: 2026-09-28
 * @Description: 知识库详情页「配置」Tab：当前库规则表单与保存、停用/启用、删除操作。
 * @BusinessRule: 实体识别模式仅 admin 可编辑；存在文档时禁止删除知识库；字段与校验口径见 libraryForm.ts。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { mysqlApi, type Library } from '../../../api/mysql'
import { USER_ROLE } from '../../../constants'
import { useUserStore } from '../../../store/modules/user'
import {
  ALL_ENTITY_MODE_OPTIONS,
  FILE_TYPE_OPTIONS,
  VISIBILITY_OPTIONS,
  ensureAllowedFileType,
  libraryFormToPayload,
  libraryToForm,
  normalizeMaxFileSize,
  validateLibraryForm,
  type LibraryForm,
} from './libraryForm'

const props = defineProps<{ library: Library }>()

/** saved：库数据变化需刷新壳头部；deleted：库已删除需返回列表。 */
const emit = defineEmits<{ (event: 'saved'): void; (event: 'deleted'): void }>()

const userStore = useUserStore()
const isSystemAdmin = computed(() => userStore.userInfo?.roles.includes(USER_ROLE.ADMIN) ?? false)
const entityModeOptions = computed(() => (isSystemAdmin.value ? ALL_ENTITY_MODE_OPTIONS : [ALL_ENTITY_MODE_OPTIONS[0]]))

const form = ref<LibraryForm>(libraryToForm(props.library))
const saving = ref(false)
const deleting = ref(false)
const changingStatus = ref(false)
const mutationError = ref('')

// 壳层刷新库数据后重新回填表单（保存成功/状态切换后保持展示与后端一致）。
watch(
  () => props.library,
  (library) => {
    form.value = libraryToForm(library)
  },
)

/** 文件类型清空兜底：恢复默认并展示/清除提示（与创建弹窗同一纯函数）。 */
function handleFileTypeChange() {
  const message = ensureAllowedFileType(form.value)
  if (message) {
    mutationError.value = message
    return
  }
  if (mutationError.value === '至少选择一个文件类型。') mutationError.value = ''
}

async function saveLibrary() {
  normalizeMaxFileSize(form.value)
  const validationError = validateLibraryForm(form.value, false)
  if (validationError) {
    mutationError.value = validationError
    return
  }
  saving.value = true
  mutationError.value = ''
  try {
    await mysqlApi.updateLibrary(
      props.library.id,
      libraryFormToPayload(form.value, { isAdmin: isSystemAdmin.value, code: props.library.code }),
    )
    await MessagePlugin.success('知识库已保存。')
    emit('saved')
  } catch (error) {
    mutationError.value = error instanceof Error ? error.message : '知识库保存失败'
  } finally {
    saving.value = false
  }
}

async function toggleLibraryStatus() {
  if (changingStatus.value) return
  changingStatus.value = true
  mutationError.value = ''
  try {
    await mysqlApi.updateLibrary(props.library.id, {
      status: props.library.status === 'active' ? 'disabled' : 'active',
    })
    const nextStatus = props.library.status === 'active' ? '停用' : '启用'
    await MessagePlugin.success(`知识库已${nextStatus}。`)
    emit('saved')
  } catch (error) {
    mutationError.value = error instanceof Error ? error.message : '知识库状态更新失败'
  } finally {
    changingStatus.value = false
  }
}

async function deleteLibrary() {
  if (props.library.document_count > 0) return
  if (!window.confirm(`确定删除知识库“${props.library.name}”吗？删除后将不再在管理端列表展示。`)) return
  deleting.value = true
  mutationError.value = ''
  try {
    await mysqlApi.deleteLibrary(props.library.id)
    await MessagePlugin.success('知识库删除成功。')
    emit('deleted')
  } catch (error) {
    mutationError.value = error instanceof Error ? error.message : '知识库删除失败'
  } finally {
    deleting.value = false
  }
}
</script>

<template>
  <div class="library-config-tab panel">
    <div class="library-section-heading">
      <strong>当前知识库规则</strong><span>以下内容仅针对“{{ props.library.name }}”；规则均为可选。</span>
    </div>
    <div v-if="mutationError" class="admin-error">{{ mutationError }}</div>
    <div class="library-form-grid">
      <div class="library-field"><t-input v-model="form.name" label="知识库名称" /></div>
      <t-input :value="props.library.code" label="知识库编码" disabled />
      <div class="library-field">
        <t-select
          v-model="form.entityMode"
          :options="entityModeOptions"
          :disabled="!isSystemAdmin"
          label="实体识别模式"
        /><small
          v-if="!isSystemAdmin && props.library.entity_recognition_mode !== 'disabled'"
          class="library-rule-warning"
          >当前知识库启用了实体识别，暂不支持普通用户导入，请联系管理员修改。</small
        >
      </div>
      <div class="library-field">
        <t-select
          v-model="form.allowedFileTypes"
          :options="FILE_TYPE_OPTIONS"
          label="允许文件类型"
          multiple
          clearable
          @change="handleFileTypeChange"
        />
      </div>
      <div class="library-field">
        <t-select v-model="form.visibility" :options="VISIBILITY_OPTIONS" label="可见范围" />
      </div>
      <div class="library-field">
        <t-input-number
          class="library-rule-number"
          v-model="form.maxFileSizeMb"
          label="单文件大小上限（MB）"
          :min="1"
          :max="10240"
          clearable
          @blur="normalizeMaxFileSize(form)"
        />
      </div>
      <div class="library-field">
        <t-input-number
          class="library-rule-number"
          v-model="form.maxDocumentCount"
          label="知识库文档总上限"
          :min="1"
          clearable
        />
      </div>
      <div class="library-field">
        <t-input-number
          class="library-rule-number"
          v-model="form.maxUploadFileCount"
          label="单次上传文件上限"
          :min="1"
          clearable
        />
      </div>
      <div class="library-field">
        <t-input-number
          class="library-rule-number"
          v-model="form.chunkSize"
          label="切片长度（字符）"
          :min="1"
          clearable
        />
      </div>
      <div class="library-field">
        <t-input-number
          class="library-rule-number"
          v-model="form.chunkOverlap"
          label="切片重叠（字符）"
          :min="0"
          clearable
        />
      </div>
      <div class="library-field library-field-switch">
        <div class="library-switch-head">
          <span class="library-switch-label">HyDE 扩展召回</span>
          <t-switch v-model="form.useHyde" />
        </div>
        <small class="library-switch-hint"
          >开启后该知识库的每次问答都会多一次大模型调用，回答更慢、用量更高；用户端不再提供该开关。</small
        >
      </div>
      <div class="library-field">
        <t-textarea
          v-model="form.description"
          label="描述"
          placeholder="请输入知识库用途、资料范围和使用场景（可选）"
        />
      </div>
    </div>
    <div class="library-actions">
      <t-button theme="primary" :loading="saving" @click="saveLibrary">保存当前规则</t-button>
      <t-button variant="outline" :loading="changingStatus" @click="toggleLibraryStatus">{{
        props.library.status === 'active' ? '停用知识库' : '启用知识库'
      }}</t-button>
      <t-tooltip content="请先删除该知识库文档后再删除知识库" :disabled="props.library.document_count === 0">
        <span>
          <t-button
            theme="danger"
            variant="outline"
            :disabled="props.library.document_count > 0"
            :loading="deleting"
            @click="deleteLibrary"
            >删除知识库</t-button
          >
        </span>
      </t-tooltip>
    </div>
  </div>
</template>

<style scoped>
.library-config-tab {
  display: grid;
  gap: 16px;
  padding: 18px;
}
.library-section-heading {
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
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
}
.library-rule-warning {
  display: block;
  margin-top: 5px;
  color: var(--warning);
  font-size: 11px;
  line-height: 1.5;
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
