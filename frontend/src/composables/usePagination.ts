/** 分页状态、页码切换和分页数据加载逻辑。 */

import { onMounted, ref, type Ref } from 'vue'
import { PAGINATION } from '../constants'

export type PaginationParams = Record<string, unknown>

export type PaginationResult<T> = {
  data: T[]
  total: number
}

export type PaginationFetcher<T, P extends PaginationParams = PaginationParams> = (
  params: P & { page: number; pageSize: number },
) => Promise<PaginationResult<T> | T[]>

export type UsePaginationOptions<P extends PaginationParams = PaginationParams> = {
  immediate?: boolean
  initialPage?: number
  initialPageSize?: number
  initialParams?: P
}

export function usePagination<T, P extends PaginationParams = PaginationParams>(
  fetcher: PaginationFetcher<T, P>,
  options: UsePaginationOptions<P> = {},
) {
  const data = ref([]) as Ref<T[]>
  const loading = ref(false)
  const error = ref<Error | null>(null)
  const currentPage = ref(options.initialPage ?? 1)
  const total = ref(0)
  const pageSize = ref(options.initialPageSize ?? PAGINATION.PAGE_SIZE)
  const params = ref<P>((options.initialParams ?? {}) as P) as Ref<P>

  async function fetchData(nextParams?: P): Promise<T[]> {
    if (nextParams) params.value = nextParams
    loading.value = true
    error.value = null
    try {
      const result = await fetcher({
        ...params.value,
        page: currentPage.value,
        pageSize: pageSize.value,
      })
      if (Array.isArray(result)) {
        data.value = result
        total.value = result.length
      } else {
        data.value = result.data
        total.value = result.total
      }
      return data.value
    } catch (cause) {
      error.value = cause instanceof Error ? cause : new Error('数据加载失败')
      throw error.value
    } finally {
      loading.value = false
    }
  }

  async function onPageChange(page: number) {
    currentPage.value = page
    return fetchData()
  }

  async function onPageSizeChange(size: number) {
    pageSize.value = size
    currentPage.value = 1
    return fetchData()
  }

  function reset() {
    currentPage.value = options.initialPage ?? 1
    pageSize.value = options.initialPageSize ?? PAGINATION.PAGE_SIZE
    params.value = (options.initialParams ?? {}) as P
    data.value = []
    total.value = 0
    error.value = null
  }

  if (options.immediate) onMounted(() => void fetchData())

  return {
    data,
    loading,
    error,
    currentPage,
    total,
    pageSize,
    params,
    fetchData,
    onPageChange,
    onPageSizeChange,
    reset,
  }
}