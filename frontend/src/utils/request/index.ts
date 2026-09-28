/**
 * JSON REST 请求封装（与 api/auth.authenticatedFetch 的分层边界见其文件头注释）。
 *
 * 职责边界：本层只做 JSON 接口三件事——默认 Content-Type、发起 authenticatedFetch、
 * 把非 2xx 响应体反序列化为 Error 抛出；凭据注入、401 刷新与过期广播全部由
 * authenticatedFetch 负责，令牌唯一权威在 store/modules/user.ts。
 * SSE 流式（api/query.ts）与二进制上传（api/mysql.ts）需要原始 Response，
 * 不经本层，请直接使用 authenticatedFetch。
 */

import { authenticatedFetch } from '../../api/auth'
import { getErrorMessage } from '../error'

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (!(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')

  let response: Response
  try {
    response = await authenticatedFetch(path, { ...options, headers })
  } catch {
    throw new Error('无法连接后端服务，请确认 FastAPI 服务已启动')
  }

  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(getErrorMessage(body, `请求失败（${response.status}）`))
  return body as T
}

export default request
