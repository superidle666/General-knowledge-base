/** 管理端侧栏统计数：各模块页加载后写入，侧栏跨路由持续展示。 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

export type AdminCounts = {
  documents: number
  users: number
  roles: number
  permissions: number
}

export const useAdminCountsStore = defineStore('adminCounts', () => {
  const counts = ref<AdminCounts>({ documents: 0, users: 0, roles: 0, permissions: 0 })

  /** 合并更新部分统计；子页面只上报自己模块负责的字段。 */
  function updateCounts(nextCounts: Partial<AdminCounts>) {
    counts.value = { ...counts.value, ...nextCounts }
  }

  return { counts, updateCounts }
})
