/** 前端展示格式化工具。 */

export function formatFileSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return '-'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2
  return `${value.toFixed(digits)} ${units[unitIndex]}`
}

export function formatNumber(value: number | string): string {
  const number = typeof value === 'string' ? Number(value) : value
  return Number.isFinite(number) ? new Intl.NumberFormat('en-US').format(number) : '-'
}

export function truncateText(text: string, maxLen: number): string {
  if (maxLen <= 0) return ''
  if (text.length <= maxLen) return text
  if (maxLen <= 3) return text.slice(0, maxLen)
  return `${text.slice(0, maxLen - 3)}...`
}