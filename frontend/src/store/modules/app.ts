/** 应用级状态：主题和管理端侧栏状态。 */

import { ref } from 'vue'
import { defineStore } from 'pinia'

type Theme = 'light' | 'dark'

export const useAppStore = defineStore('app', () => {
  const sidebarCollapsed = ref(false)
  const theme = ref<Theme>('light')

  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  function setTheme(nextTheme: Theme) {
    theme.value = nextTheme
  }

  return { sidebarCollapsed, theme, toggleSidebar, setTheme }
})