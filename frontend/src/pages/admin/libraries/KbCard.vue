<!--
 * @FilePath: @/pages/admin/libraries/KbCard.vue
 * @Author: 项目维护者
 * @Date: 2026-09-28
 * @Description: 知识库卡片：图标 + 状态标签 + 名称 + 两行描述 + 元信息 + 更多操作，整卡可点进入详情。
 * @BusinessRule: 状态标签唯一来源 statusMeta.ts；停用库仍可进入详情查看配置；操作命令上抛由列表页统一处理。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { MoreIcon, FileIcon } from 'tdesign-icons-vue-next'
import type { Library } from '../../../api/mysql'
import { LIBRARY_STATUS_META, getStatusMeta } from '../shared/statusMeta'

const props = defineProps<{ library: Library }>()

const emit = defineEmits<{
  (event: 'open'): void
  (event: 'command', command: 'edit' | 'toggle-status' | 'delete'): void
}>()

const statusMeta = computed(() => getStatusMeta(LIBRARY_STATUS_META, props.library.status))

const dropdownOptions = computed(() => [
  { content: '编辑信息', value: 'edit' },
  { content: props.library.status === 'active' ? '停用知识库' : '启用知识库', value: 'toggle-status' },
  // 删除禁用规则与详情页配置 Tab 一致：有文档先删文档。
  { content: '删除知识库', value: 'delete', disabled: props.library.document_count > 0 },
])

function handleCommand(options: { value: unknown }) {
  emit('command', options.value as 'edit' | 'toggle-status' | 'delete')
}
</script>

<template>
  <button type="button" class="kb-card" @click="emit('open')">
    <span class="kb-card-top">
      <span class="kb-card-icon"><FileIcon /></span>
      <t-tag :theme="statusMeta.theme" variant="light" size="small">{{ statusMeta.label }}</t-tag>
    </span>
    <strong class="kb-card-title">{{ props.library.name }}</strong>
    <p class="kb-card-desc">{{ props.library.description || '暂无描述' }}</p>
    <span class="kb-card-meta">{{ props.library.code }} · {{ props.library.document_count }} 篇文档</span>
    <span class="kb-card-actions" @click.stop>
      <t-dropdown :options="dropdownOptions" trigger="click" @click="handleCommand">
        <t-button variant="text" shape="square" size="small" aria-label="更多操作"><MoreIcon /></t-button>
      </t-dropdown>
    </span>
  </button>
</template>

<style scoped>
.kb-card {
  position: relative;
  display: grid;
  min-width: 0;
  gap: 6px;
  align-content: start;
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
  text-align: left;
  cursor: pointer;
  transition:
    border-color 0.2s,
    box-shadow 0.2s;
}
.kb-card:hover,
.kb-card:focus-visible {
  border-color: var(--primary-border);
  box-shadow: var(--shadow-pop);
}
.kb-card-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}
.kb-card-icon {
  display: grid;
  width: 44px;
  height: 44px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 50%;
  background: var(--primary-soft);
  color: var(--primary);
}
.kb-card-icon svg {
  width: 20px;
}
.kb-card-title {
  overflow: hidden;
  color: var(--text);
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.kb-card-desc {
  display: -webkit-box;
  min-height: 38px;
  margin: 0;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  color: var(--muted);
  font-size: 12px;
  line-height: 19px;
}
.kb-card-meta {
  overflow: hidden;
  color: var(--weak);
  font-size: 11px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.kb-card-actions {
  position: absolute;
  right: 10px;
  bottom: 10px;
  display: inline-flex;
  color: var(--muted);
}
.kb-card-actions :deep(.t-button) {
  color: var(--weak);
}
.kb-card-actions :deep(.t-button:hover) {
  color: var(--primary);
  background: var(--primary-weak);
}
</style>
