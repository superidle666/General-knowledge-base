/** 后端错误响应解析工具。 */

/** 从后端错误响应体提取可读错误信息，兼容 FastAPI 字符串与数组两种 detail 形态。 */
export function getErrorMessage(body: unknown, fallback: string) {
  if (!body || typeof body !== 'object' || !('detail' in body)) return fallback
  const detail = body.detail
  if (typeof detail === 'string') return detail
  if (!Array.isArray(detail)) return fallback
  const messages = detail
    .map((item) => (item && typeof item === 'object' && 'msg' in item && typeof item.msg === 'string' ? item.msg : ''))
    .filter(Boolean)
  return messages.join('；') || fallback
}
