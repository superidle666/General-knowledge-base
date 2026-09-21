/** HTTP 请求封装，统一处理 API 地址、认证令牌和错误信息。 */

import { useUserStore } from '../../store/modules/user'
import { authenticatedFetch, getAccessToken } from '../../api/auth'

const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')

function getErrorMessage(body: unknown, fallback: string) {
  if (!body || typeof body !== 'object' || !('detail' in body)) return fallback
  const detail = body.detail
  if (typeof detail === 'string') return detail
  if (!Array.isArray(detail)) return fallback
  const messages = detail
    .map((item) => item && typeof item === 'object' && 'msg' in item && typeof item.msg === 'string' ? item.msg : '')
    .filter(Boolean)
  return messages.join('；') || fallback
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const userStore = useUserStore()
  const headers = new Headers(options.headers)
  if (!(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (userStore.token) headers.set('Authorization', 'Bearer ' + userStore.token)

  let response: Response
  try {
    response = await authenticatedFetch(path, { ...options, headers })
    const refreshedToken = getAccessToken()
    if (refreshedToken && refreshedToken !== userStore.token) userStore.setToken(refreshedToken)
  } catch {
    throw new Error('无法连接后端服务，请确认 FastAPI 服务已启动')
  }

  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(getErrorMessage(body, `请求失败（${response.status}）`))
  return body as T
}

export { API_BASE }
export default request