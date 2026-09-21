<!--
 * @FilePath: @/layouts/AdminLayout.vue
 * @Author: 项目维护者
 * @Date: 2026-08-31
 * @Description: 管理端主布局容器，提供响应式内容区域
 -->
<script setup lang="ts">
defineProps<{ authenticated?: boolean; darkMode?: boolean }>()
</script>
<template>
  <div class="admin-shell" :class="{ 'is-dark': darkMode }">
    <slot />
  </div>
</template>
