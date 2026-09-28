/**
 * 认证持久化与 HTTP 基座：浏览器会话存储、令牌刷新和带认证 fetch。
 *
 * Note:
 *     访问令牌和刷新令牌必须始终写入同一存储介质，避免“记住我”切换后误用旧会话；
 *     存储只是“刷新页面后恢复会话”的持久化通道，运行时登录态的唯一权威在
 *     store/modules/user.ts（Pinia），本层不自持状态、只读 store 注入凭据。
 */
import { HTTP_STATUS, STORAGE_KEY } from '../constants'

/** Browser-side auth session handling for the administration console. */

// 默认同源相对路径：dev 由 vite 代理 /api 到后端；跨域部署时显式设 VITE_API_BASE_URL=http://host:port
const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')
const ACCESS_TOKEN_KEY = STORAGE_KEY.TOKEN
const REFRESH_TOKEN_KEY = STORAGE_KEY.REFRESH_TOKEN
const USER_KEY = STORAGE_KEY.USER_INFO
const LEGACY_REMEMBERED_CREDENTIALS_KEY = STORAGE_KEY.REMEMBERED_CREDENTIALS

export type AuthUser = {
  id: number
  username: string
  display_name: string
  avatar_url: string | null
  roles: string[]
  permissions: string[]
}

export type TokenPair = {
  access_token: string
  refresh_token: string
  token_type?: 'bearer'
  expires_in?: number
  user: AuthUser
}

function errorMessage(body: unknown, fallback: string) {
  return body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string'
    ? body.detail
    : fallback
}

const sessionKeys = [ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, USER_KEY] as const

function getStorage() {
  return typeof window === 'undefined' ? null : window.localStorage
}

function getSessionStorageObject() {
  return typeof window === 'undefined' ? null : window.sessionStorage
}

function clearLegacyRememberedCredentials() {
  getStorage()?.removeItem(LEGACY_REMEMBERED_CREDENTIALS_KEY)
  getSessionStorageObject()?.removeItem(LEGACY_REMEMBERED_CREDENTIALS_KEY)
}

clearLegacyRememberedCredentials()

/** 读会话项：localStorage 优先（“记住我”），回落 sessionStorage。 */
function getStoredItem(key: (typeof sessionKeys)[number]) {
  const local = getStorage()
  const session = getSessionStorageObject()
  return local?.getItem(key) ?? session?.getItem(key) ?? null
}

/** 启动重 hydration：仅供 store 初始值读取，运行期判断登录态一律用 store。 */
export function getStoredAccessToken() {
  return getStoredItem(ACCESS_TOKEN_KEY)
}

export function getStoredRefreshToken() {
  return getStoredItem(REFRESH_TOKEN_KEY)
}

export function getStoredUser(): AuthUser | null {
  const value = getStoredItem(USER_KEY)
  if (!value) return null
  try {
    return JSON.parse(value) as AuthUser
  } catch {
    return null
  }
}

/** 当前会话的持久化介质是否是不失忆的 localStorage（决定刷新后延续“记住我”语义）。 */
export function isRememberedSession() {
  return Boolean(getStorage()?.getItem(REFRESH_TOKEN_KEY))
}

// NOTE: 未勾选“记住我”时仅存入 sessionStorage，关闭浏览器后不保留登录态。
export function saveSession(session: TokenPair, rememberLogin = true) {
  const storage = rememberLogin ? getStorage() : getSessionStorageObject()
  const otherStorage = rememberLogin ? getSessionStorageObject() : getStorage()
  if (!storage || !otherStorage) return
  for (const key of sessionKeys) otherStorage.removeItem(key)
  storage.setItem(ACCESS_TOKEN_KEY, session.access_token)
  storage.setItem(REFRESH_TOKEN_KEY, session.refresh_token)
  storage.setItem(USER_KEY, JSON.stringify(session.user))
}

/** 清空两种介质中的会话数据；store 清空后调用本函数即可保证两端一致。 */
export function clearSession() {
  const local = getStorage()
  const session = getSessionStorageObject()
  for (const key of sessionKeys) {
    local?.removeItem(key)
    session?.removeItem(key)
  }
}

async function rawRequest(path: string, options: RequestInit = {}) {
  return fetch(API_BASE + path, options)
}

async function refreshSession(): Promise<boolean> {
  const refreshToken = getStoredRefreshToken()
  if (!refreshToken) return false
  try {
    const response = await rawRequest('/api/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })
    const body = (await response.json().catch(() => null)) as TokenPair | null
    if (!response.ok || !body) return false
    saveSession(body, isRememberedSession())
    // 惰性导入：避免 auth.ts ↔ store 静态循环依赖（登录态权威在 store）。
    const { useUserStore } = await import('../store/modules/user')
    useUserStore().syncAccessToken(body.access_token)
    return true
  } catch {
    return false
  }
}

/**
 * 带认证 fetch：非 JSON 场景（SSE 流式、二进制上传）直接用它，需要原始 Response；
 * 普通 JSON REST 请走 utils/request 的 request()，不要把响应体处理/错误反序列化下沉到这里。
 *
 * 职责边界：凭据注入（读 store 唯一权威令牌）、401 时一次性刷新重试、
 * 刷新失败时清会话并广播 auth-expired、URL 拼接（API_BASE）。
 * NOTE: 仅允许一次刷新重试，防止无效令牌造成无限请求循环。
 */
export async function authenticatedFetch(path: string, options: RequestInit = {}, retried = false): Promise<Response> {
  // 运行时令牌只认 store；存储读取已退出凭据链路。
  const { useUserStore } = await import('../store/modules/user')
  const userStore = useUserStore()
  const accessToken = userStore.token
  const headers = new Headers(options.headers)
  if (!headers.has('Authorization') && accessToken) headers.set('Authorization', 'Bearer ' + accessToken)
  const response = await rawRequest(path, { ...options, headers })
  if (response.status !== HTTP_STATUS.UNAUTHORIZED) return response
  if (!retried && (await refreshSession())) {
    const retryHeaders = new Headers(options.headers)
    retryHeaders.delete('Authorization')
    return authenticatedFetch(path, { ...options, headers: retryHeaders }, true)
  }
  userStore.clearSession()
  window.dispatchEvent(new Event('auth-expired'))
  return response
}

/** 登录接口：只返回服务端令牌对，写入存储与登录态由 store 统一接管。 */
export async function login(username: string, password: string): Promise<TokenPair> {
  const response = await rawRequest('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const body = (await response.json().catch(() => null)) as TokenPair | null
  if (!response.ok || !body) throw new Error(errorMessage(body, '登录失败'))
  return body
}

/** 服务端注销：刷新令牌由 store 传入；网络异常也照常抛出，存储清理由调用方 store 负责。 */
export async function logout(refreshToken: string | null) {
  await authenticatedFetch('/api/v1/auth/logout', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refreshToken }),
  })
}

/** 只回写用户信息到当前会话介质，不触碰令牌（令牌写入只发生在登录/刷新链路）。 */
function persistUser(user: AuthUser) {
  const storage = isRememberedSession() ? getStorage() : getSessionStorageObject()
  storage?.setItem(USER_KEY, JSON.stringify(user))
}

export async function currentUser(): Promise<AuthUser> {
  const response = await authenticatedFetch('/api/v1/auth/me')
  const body = await response.json().catch(() => null)
  if (!response.ok || !body) throw new Error(errorMessage(body, '登录状态已失效'))
  persistUser(body as AuthUser)
  return body as AuthUser
}
