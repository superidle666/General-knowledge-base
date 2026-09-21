/** 用户端知识库问答请求状态和流式响应管理。 */

import { ref } from 'vue'
import { queryKnowledgeBaseStream, type QueryRequest, type QueryResponse } from '../api/query'

export function useUserQuery() {
  const isLoading = ref(false)
  const error = ref('')
  const answer = ref('')
  const sources = ref<QueryResponse['sources']>([])
  const currentQuery = ref('')
  let activeController: AbortController | null = null
  let activeRequestId = 0

  async function submit(payload: QueryRequest, onDelta?: (delta: string) => void) {
    if (isLoading.value) return null

    const requestId = ++activeRequestId
    const controller = new AbortController()
    activeController?.abort()
    activeController = controller
    isLoading.value = true
    error.value = ''
    answer.value = ''
    sources.value = []
    currentQuery.value = payload.query

    try {
      const result = await queryKnowledgeBaseStream(payload, {
        onDelta: (delta) => {
          if (isCurrentRequest(requestId, controller)) onDelta?.(delta)
        },
      }, controller.signal)
      if (!isCurrentRequest(requestId, controller)) return null
      answer.value = result.answer
      sources.value = result.sources
      return result
    } catch (caughtError) {
      if (controller.signal.aborted || !isCurrentRequest(requestId, controller)) return null
      error.value = caughtError instanceof Error ? caughtError.message : '查询失败，请稍后重试'
      throw caughtError
    } finally {
      if (isCurrentRequest(requestId, controller)) {
        isLoading.value = false
        activeController = null
      }
    }
  }

  function cancel() {
    activeRequestId += 1
    activeController?.abort()
    activeController = null
    isLoading.value = false
  }

  function isCurrentRequest(requestId: number, controller: AbortController) {
    return activeRequestId === requestId && activeController === controller && !controller.signal.aborted
  }

  function reset() {
    cancel()
    error.value = ''
    answer.value = ''
    sources.value = []
    currentQuery.value = ''
  }

  function restore(result: QueryResponse) {
    cancel()
    error.value = ''
    answer.value = result.answer
    sources.value = result.sources
    currentQuery.value = result.query
  }

  return { isLoading, error, answer, sources, currentQuery, submit, cancel, reset, restore }
}