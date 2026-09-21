/** 日期解析和格式化工具，兼容 Date、时间戳和日期字符串输入。 */

type DateInput = Date | string | number

function toDate(value: DateInput): Date | null {
  const date = value instanceof Date ? new Date(value.getTime()) : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

export function formatDate(value: DateInput, format = 'YYYY-MM-DD HH:mm:ss'): string {
  const date = toDate(value)
  if (!date) return ''

  const tokens: Record<string, string> = {
    YYYY: String(date.getFullYear()),
    MM: String(date.getMonth() + 1).padStart(2, '0'),
    DD: String(date.getDate()).padStart(2, '0'),
    HH: String(date.getHours()).padStart(2, '0'),
    mm: String(date.getMinutes()).padStart(2, '0'),
    ss: String(date.getSeconds()).padStart(2, '0'),
  }

  return format.replace(/YYYY|MM|DD|HH|mm|ss/g, (token) => tokens[token])
}

export function relativeTime(value: DateInput): string {
  const date = toDate(value)
  if (!date) return ''

  const diff = Date.now() - date.getTime()
  if (diff < 0) return '刚刚'
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return '刚刚'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}天前`
  const months = Math.floor(days / 30)
  if (months < 12) return `${months}个月前`
  return `${Math.floor(days / 365)}年前`
}