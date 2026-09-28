/** 查询上下文版本守卫：异步回包返回时判定其是否仍属于当前页面上下文。 */

import { ref } from 'vue'

/**
 * getLibraryId 返回当前生效的知识库 ID：快照后版本或知识库任一变化，
 * 旧上下文即失效，回包不再被采纳。
 */
export function useQueryContext(getLibraryId: () => number | undefined) {
  const version = ref(0)

  /** 使当前上下文失效：此前发出的请求回包不再被采纳。 */
  function invalidate() {
    version.value += 1
  }

  /** 为一次异步操作生成上下文快照（版本 + 知识库）。 */
  function snapshot(libraryId: number) {
    return `${version.value}:${libraryId}`
  }

  /** 判断回包是否仍属于 snapshot 对应的上下文。 */
  function isCurrent(context: string, libraryId: number) {
    return getLibraryId() === libraryId && snapshot(libraryId) === context
  }

  return { invalidate, snapshot, isCurrent }
}
