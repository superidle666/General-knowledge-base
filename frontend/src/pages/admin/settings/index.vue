<!--
 * @FilePath: @/pages/admin/settings/index.vue
 * @Author: 项目维护者
 * @Date: 2026-09-02
 * @Description: 系统设置页面，用于维护模型、向量检索、文档和站点配置。
 * @BusinessRule: 敏感配置仅可写入，页面绝不回显明文；访问权限由后端限定为系统管理员。
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { RefreshIcon } from 'tdesign-icons-vue-next'
import { mysqlApi, type SystemSetting } from '../../../api/mysql'
import { useRequest } from '../../../composables'

const settings = ref<SystemSetting[]>([])
const drafts = ref<Record<string, string>>({})
const savingKey = ref('')
const successMessage = ref('')
const saveError = ref('')
const settingsRequest = useRequest(async () => {
  const result = await mysqlApi.listSettings()
  settings.value = result
  drafts.value = Object.fromEntries(result.filter((item) => !item.is_sensitive).map((item) => [item.key, item.value || '']))
  return result
}, { immediate: true })
const loading = settingsRequest.loading
const errorMessage = computed(() => saveError.value || settingsRequest.error.value?.message || '')

const categoryNames: Record<string, string> = {
  model: '模型服务',
  embedding: '向量化服务',
  vector: '向量检索',
  document: '文档处理',
  site: '站点信息',
}
const categoryOrder = ['model', 'embedding', 'vector', 'document', 'site']
const groups = computed(() => categoryOrder
  .map((category) => ({ category, name: categoryNames[category], items: settings.value.filter((item) => item.category === category) }))
  .filter((group) => group.items.length))

// NOTE: 敏感值即使后端存在，也只在本次输入期间显示，避免二次渲染泄露明文。
function displayValue(item: SystemSetting) {
  return drafts.value[item.key] ?? (item.is_sensitive ? '' : item.value || '')
}

async function loadSettings() {
  try {
    await settingsRequest.execute()
  } catch {
    // Error is exposed through the composable for the template.
  }
}

/** 保存单项配置；敏感值保存后立即清空前端草稿。 */
async function saveSetting(item: SystemSetting) {
  savingKey.value = item.key
  saveError.value = ''
  successMessage.value = ''
  try {
    const updated = await mysqlApi.updateSetting(item.key, drafts.value[item.key] ?? '')
    settings.value = settings.value.map((setting) => setting.key === updated.key ? updated : setting)
    if (!item.is_sensitive) drafts.value[item.key] = updated.value || ''
    if (item.is_sensitive) drafts.value[item.key] = ''
    successMessage.value = item.is_sensitive ? '敏感配置已安全更新' : '配置已保存'
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : '保存系统设置失败'
  } finally {
    savingKey.value = ''
  }
}

</script>

<template>
  <section class="admin-module">
    <div class="admin-page-head compact">
      <div>
        <div class="eyebrow">SYSTEM</div>
        <h1>系统设置</h1>
        <p>仅系统管理员可维护。密钥等敏感配置只支持更新，不会返回明文。</p>
      </div>
      <t-button variant="outline" :loading="loading" @click="loadSettings">
        <RefreshIcon />
        刷新
      </t-button>
    </div>

    <div v-if="errorMessage" class="admin-error">
      {{ errorMessage }}
      <button @click="loadSettings">重试</button>
    </div>
    <div v-if="successMessage" class="settings-success">{{ successMessage }}</div>
    <div v-if="loading" class="admin-loading">正在加载系统设置...</div>

    <div class="settings-grid settings-runtime">
      <section v-for="group in groups" :key="group.category" class="panel settings-panel">
        <h2>{{ group.name }}</h2>
        <div v-for="item in group.items" :key="item.key" class="setting-row setting-edit-row">
          <div class="setting-copy">
            <strong>{{ item.name }}</strong>
            <p>{{ item.description }}</p>
          </div>
          <div class="setting-editor">
            <t-input
              :model-value="displayValue(item)"
              :type="item.is_sensitive ? 'password' : 'text'"
              :placeholder="item.is_sensitive ? (item.masked_value || '请输入新的值') : '请输入配置值'"
              :clearable="!item.is_sensitive"
              @update:model-value="(value: unknown) => drafts[item.key] = String(value || '')"
            />
            <t-button theme="primary" size="small" :loading="savingKey === item.key" @click="saveSetting(item)">保存</t-button>
          </div>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.settings-runtime { grid-template-columns: minmax(0, 1fr); align-items: start; gap: 16px; }
.settings-runtime .settings-panel { padding: 20px; }
.settings-runtime .settings-panel h2 { margin: 0; padding-bottom: 16px; }
.setting-edit-row { min-height: 0; padding: 16px 0; gap: 20px; }
.setting-copy { min-width: 0; }
.setting-editor { width: min(100%, 360px); display: flex; align-items: center; gap: 8px; }
.setting-editor :deep(.t-input) { flex: 1; min-width: 0; }
.settings-success { color: #166534; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 5px; padding: 9px 12px; margin-bottom: 16px; font-size: 12px; }
@media (max-width: 760px) { .setting-edit-row { align-items: flex-start; flex-direction: column; }.setting-editor { width: 100%; } }
</style>
