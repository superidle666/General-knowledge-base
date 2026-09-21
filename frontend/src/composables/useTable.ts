/** 管理端表格查询、分页、筛选和刷新逻辑。 */

import { computed, ref, type Ref } from 'vue'
import { usePagination, type PaginationFetcher, type UsePaginationOptions } from './usePagination'

export type SortOrder = 'asc' | 'desc' | ''

export function useTable<T, P extends Record<string, unknown> = Record<string, unknown>>(
  fetcher: PaginationFetcher<T, P>,
  options: UsePaginationOptions<P> & { initialSearchForm?: P } = {},
) {
  const pagination = usePagination(fetcher, {
    ...options,
    initialParams: options.initialSearchForm ?? options.initialParams,
  })
  const searchForm = ref<P>({ ...(options.initialSearchForm ?? {}) } as P)
  const sortField = ref('')
  const sortOrder = ref<SortOrder>('')
  const selectedRows = ref([]) as Ref<T[]>

  async function search() {
    return pagination.fetchData({
      ...searchForm.value,
      sortField: sortField.value,
      sortOrder: sortOrder.value,
    } as P)
  }

  function resetSearch() {
    searchForm.value = { ...(options.initialSearchForm ?? {}) } as P
    sortField.value = ''
    sortOrder.value = ''
    pagination.reset()
    return search()
  }

  function toggleSelect(row: T) {
    const index = selectedRows.value.indexOf(row)
    if (index >= 0) selectedRows.value.splice(index, 1)
    else selectedRows.value.push(row)
  }

  function clearSelect() {
    selectedRows.value = []
  }

  const hasSelection = computed(() => selectedRows.value.length > 0)

  async function refresh() {
    return pagination.fetchData()
  }

  return {
    ...pagination,
    searchForm,
    sortField,
    sortOrder,
    selectedRows,
    hasSelection,
    search,
    resetSearch,
    toggleSelect,
    clearSelect,
    refresh,
  }
}