/**
 * 管理端浏览器会话与认证请求封装。
 *
 * Note:
 *     访问令牌和刷新令牌必须始终写入同一存储介质，避免“记住我”切换后误用旧会话。
 */
import { HTTP_STATUS, STORAGE_KEY } from '../constants'

/** Browser-side auth session handling for the administration console. */

const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')
const ACCESS_TOKEN_KEY = STORAGE_KEY.TOKEN
const REFRESH_TOKEN_KEY = 'kb-admin-refresh-token'
const USER_KEY = STORAGE_KEY.USER_INFO
const LEGACY_REMEMBERED_CREDENTIALS_KEY = 'kb-admin-remembered-credentials'

export type AuthUser = {
  id: number
  username: string
  display_name: string
  avatar_url: string | null
  roles: string[]
  permissions: string[]
}

type TokenPair = {
  access_token: string
  refresh_token: string
  token_type: 'bearer'
  expires_in: number
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

export function getStoredItem(key: typeof sessionKeys[number]) {
  const local = getStorage()
  const session = getSessionStorageObject()
  return local?.getItem(key) ?? session?.getItem(key) ?? null
}

export function getAccessToken() {
  return getStoredItem(ACCESS_TOKEN_KEY)
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

function getSessionStorage() {
  const local = getStorage()
  const session = getSessionStorageObject()
  return local && local.getItem(REFRESH_TOKEN_KEY) ? local : session
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

export function clearSession() {
  const local = getStorage()
  const session = getSessionStorageObject()
  for (const key of sessionKeys) {
    local?.removeItem(key)
    session?.removeItem(key)
  }
}

export function hasAccessToken() {
  return Boolean(getStoredItem(ACCESS_TOKEN_KEY))
}
async function rawRequest(path: string, options: RequestInit = {}) {
  return fetch(API_BASE + path, options)
}

async function refreshSession(): Promise<boolean> {
  const refreshToken = getStoredItem(REFRESH_TOKEN_KEY)
  if (!refreshToken) return false
  try {
    const response = await rawRequest('/api/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })
    const body = await response.json().catch(() => null)
    if (!response.ok || !body) return false
    saveSession(body as TokenPair, getSessionStorage() === getStorage())
    return true
  } catch {
    return false
  }
}

// NOTE: 仅允许一次刷新重试，防止无效令牌造成无限请求循环。
export async function authenticatedFetch(path: string, options: RequestInit = {}, retried = false): Promise<Response> {
  const accessToken = getStoredItem(ACCESS_TOKEN_KEY)
  const headers = new Headers(options.headers)
  if (!headers.has('Authorization') && accessToken) headers.set('Authorization', 'Bearer ' + accessToken)
  const response = await rawRequest(path, { ...options, headers })
  if (response.status !== HTTP_STATUS.UNAUTHORIZED) return response
  if (!retried && await refreshSession()) {
    const retryHeaders = new Headers(options.headers)
    retryHeaders.delete('Authorization')
    return authenticatedFetch(path, { ...options, headers: retryHeaders }, true)
  }
  clearSession()
  window.dispatchEvent(new Event('auth-expired'))
  return response
}

export async function login(username: string, password: string, rememberLogin = true): Promise<AuthUser> {
  const response = await rawRequest('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const body = await response.json().catch(() => null)
  if (!response.ok || !body) throw new Error(errorMessage(body, '登录失败'))
  saveSession(body as TokenPair, rememberLogin)
  return (body as TokenPair).user
}

export async function logout() {
  const refreshToken = getStoredItem(REFRESH_TOKEN_KEY)
  try {
    await authenticatedFetch('/api/v1/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })
  } finally {
    clearSession()
  }
}

export async function currentUser(): Promise<AuthUser> {
  const response = await authenticatedFetch('/api/v1/auth/me')
  const body = await response.json().catch(() => null)
  if (!response.ok || !body) throw new Error(errorMessage(body, '登录状态已失效'))
  getSessionStorage()?.setItem(USER_KEY, JSON.stringify(body))
  return body as AuthUser
}

export const authApi = {
  login,
  logout,
  getUserInfo: currentUser,
}
