/** 通用异步请求状态管理，统一处理加载、结果、错误和重置。 */

import { ref } from 'vue'

export type RequestExecutor<T, P = void> = (params: P) => Promise<T>

export type UseRequestOptions<P> = {
  immediate?: boolean
  initialParams?: P
}

export function useRequest<T, P = void>(
  executor: RequestExecutor<T, P>,
  options: UseRequestOptions<P> = {},
) {
  const data = ref<T>()
  const loading = ref(false)
  const error = ref<Error | null>(null)

  async function execute(params?: P): Promise<T> {
    loading.value = true
    error.value = null
    try {
      const result = await executor(params === undefined ? options.initialParams as P : params)
      data.value = result
      return result
    } catch (cause) {
      error.value = cause instanceof Error ? cause : new Error('请求失败')
      throw error.value
    } finally {
      loading.value = false
    }
  }

  function reset() {
    data.value = undefined
    loading.value = false
    error.value = null
  }

  if (options.immediate) void execute()

  return { data, loading, error, execute, reset }
}