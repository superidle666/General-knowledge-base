<!-- 本地文档上传弹窗，只允许选择 PDF 和 Markdown 文件。 -->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { mysqlApi } from '../../../api/mysql'

const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'uploaded'): void
  (event: 'error', message: string): void
}>()

const props = defineProps<{
  visible: boolean
  libraryOptions: Array<{ label: string; value: number }>
  initialLibraryId?: number
}>()
const files = ref<File[]>([])
const libraryId = ref<number>()
const uploading = ref(false)
const fileInput = ref<HTMLInputElement>()
const uploadError = ref('')

const selectedFileNames = computed(() =>
  files.value.map((file) => file.name),
)

/** 展示待上传文件名，避免原生文件控件被清空后显示错误的选择状态。 */
const selectedFileSummary = computed(() => {
  if (!selectedFileNames.value.length) return '暂未选择文件'
  return `已选择：${selectedFileNames.value.join('、')}`
})

/** 根据当前表单状态在弹窗内提示下一步，避免错误提示出现在文档列表页。 */
const uploadFormHint = computed(() => {
  if (uploadError.value) return uploadError.value
  if (!libraryId.value && !files.value.length) {
    return '请选择知识库并添加上传文件。'
  }
  if (!libraryId.value) return '请选择知识库。'
  if (!files.value.length) return '请添加上传文件。'
  return `已选择知识库和 ${files.value.length} 个文件，将作为同一批次提交。`
})

const isUploadHintError = computed(() => Boolean(uploadError.value))

/** 将本次选择的合法文件追加到待上传列表，而不是覆盖已选择的文件。 */
function selectFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const selectedFiles = Array.from(input.files || [])
  const supportedFiles = selectedFiles.filter(isSupportedFile)
  const invalidFiles = selectedFiles
    .filter((file) => !isSupportedFile(file))
    .map((file) => file.name)
  const existingKeys = new Set(files.value.map(fileKey))
  const uniqueFiles = supportedFiles.filter((file) => {
    const key = fileKey(file)
    if (existingKeys.has(key)) return false
    existingKeys.add(key)
    return true
  })
  files.value = [...files.value, ...uniqueFiles]
  input.value = ''
  uploadError.value = invalidFiles.length
    ? `不支持的文件格式：${invalidFiles.join('、')}，仅支持 PDF 和 MD`
    : ''
}

/** 识别同一批次中重复选择的本地文件。内容重复但文件名不同的情况由后端校验。 */
function fileKey(file: File) {
  return `${file.name}\u0000${file.size}\u0000${file.lastModified}`
}

/** 仅允许 PDF 和 Markdown 扩展名，大小写不敏感。 */
function isSupportedFile(file: File) {
  const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  return extension === '.pdf' || extension === '.md'
}

/** 移除尚未开始上传的单个文件。 */
function removeFile(index: number) {
  files.value.splice(index, 1)
  clearFileInput()
}

/** 由自定义选择区域唤起系统文件选择器。 */
function openFileSelector() {
  fileInput.value?.click()
}

/** 恢复初始状态，避免下次打开仍显示上一次的文件列表。 */
function resetForm() {
  files.value = []
  libraryId.value = undefined
  uploadError.value = ''
  clearFileInput()
}

/** 清空原生文件控件，避免已移除文件的名称仍显示在选择区域。 */
function clearFileInput() {
  if (fileInput.value) fileInput.value.value = ''
}

/** 一次提交整批文件，由后端统一校验本次上传和知识库配额。 */
async function upload() {
  if (!files.value.length || !libraryId.value) {
    return
  }
  uploadError.value = ''
  uploading.value = true
  try {
    await mysqlApi.uploadDocuments(files.value, libraryId.value)
    emit('uploaded')
    resetForm()
    emit('update:visible', false)
  } catch (error) {
    uploadError.value = error instanceof Error ? error.message : '上传失败'
  } finally {
    uploading.value = false
  }
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      libraryId.value = props.initialLibraryId
      uploadError.value = ''
      return
    }
    if (!uploading.value) resetForm()
  },
  { immediate: true },
)
</script>

<template>
  <t-dialog
    :visible="visible"
    header="上传文档"
    :confirm-btn="{ content: '开始上传', theme: 'primary', loading: uploading }"
    @update:visible="emit('update:visible', $event)"
    @confirm="upload"
  >
    <div class="dialog-fields">
      <p
        class="upload-form-hint"
        :class="{ 'upload-form-hint--error': isUploadHintError }"
      >
        {{ uploadFormHint }}
      </p>
      <t-select
        v-model="libraryId"
        :options="libraryOptions"
        placeholder="选择知识库"
      />
      <input
        ref="fileInput"
        class="upload-file-input"
        type="file"
        accept=".pdf,.md"
        multiple
        @change="selectFiles"
      />
      <p class="upload-supported-hint">当前仅支持 PDF 和 MD 类型文件。</p>
      <div class="upload-selector-row">
        <t-button variant="outline" :disabled="uploading" @click="openFileSelector">
          选择文件
        </t-button>
        <span class="upload-selection-summary" :title="selectedFileSummary">
          {{ selectedFileSummary }}
        </span>
      </div>
      <div v-if="selectedFileNames.length" class="upload-file-list">
        <div
          v-for="(name, index) in selectedFileNames"
          :key="`${name}-${index}`"
          class="upload-file-item"
        >
          <span :title="name">{{ name }}</span>
          <t-button
            variant="text"
            theme="danger"
            size="small"
            :disabled="uploading"
            @click="removeFile(index)"
          >
            移除
          </t-button>
        </div>
      </div>
    </div>
  </t-dialog>
</template>

<style scoped>
.upload-file-list { display: grid; max-height: 180px; gap: 6px; overflow-y: auto; }
.upload-file-input { position: absolute; width: 1px; height: 1px; overflow: hidden; opacity: 0; pointer-events: none; }
.upload-selector-row { display: flex; min-width: 0; align-items: center; gap: 10px; }
.upload-selection-summary { min-width: 0; overflow: hidden; color: #6b7280; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.upload-form-hint { margin: 0; color: #2563eb; font-size: 12px; line-height: 1.5; }
.upload-form-hint--error { color: #dc2626; }
.upload-file-item { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 8px; border: 1px solid #e5e7eb; border-radius: 4px; background: #f8fafc; }
.upload-file-item span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.upload-supported-hint { margin: -2px 0 4px; color: #64748b; font-size: 11px; }
</style>
