/**
 * 用户认证状态：登录态的唯一运行时权威（A1 收敛后）。
 *
 * Note:
 *     api/auth.ts 的浏览器存储只承担“刷新页面后恢复会话”的持久化职责；
 *     运行期令牌以本 store 的 token 为准，凭据读写全部收敛到本文件，
 *     其他模块不得绕过 store 直接读写会话存储做认证判断。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  clearSession,
  currentUser,
  getStoredAccessToken,
  getStoredRefreshToken,
  getStoredUser,
  login as loginRequest,
  logout as logoutRequest,
  saveSession,
  type AuthUser,
} from '../../api/auth'

export const useUserStore = defineStore('user', () => {
  // 初始值仅做一次启动重 hydration，之后 token 只随本 store 动作变化
  const token = ref(getStoredAccessToken() || '')
  const userInfo = ref<AuthUser | null>(getStoredUser())
  const permissions = ref<string[]>(userInfo.value?.permissions || [])

  const isLoggedIn = computed(() => Boolean(token.value))
  const userName = computed(() => userInfo.value?.display_name || userInfo.value?.username || '')
  const userRole = computed(() => userInfo.value?.roles?.[0] || '')

  function setUserInfo(info: AuthUser | null) {
    userInfo.value = info
    permissions.value = info?.permissions || []
  }

  /** 令牌刷新成功后的唯一写入口，由 api/auth 刷新链路回调。 */
  function syncAccessToken(nextToken: string) {
    token.value = nextToken
  }

  /** 清空运行时登录态并同步持久化存储，保证两端不再漂移。 */
  function clearSessionState() {
    token.value = ''
    setUserInfo(null)
    clearSession()
  }

  async function login(username: string, password: string, rememberLogin = true) {
    const pair = await loginRequest(username, password)
    saveSession(pair, rememberLogin)
    token.value = pair.access_token
    setUserInfo(pair.user)
    return pair.user
  }

  async function logout() {
    try {
      await logoutRequest(getStoredRefreshToken())
    } finally {
      clearSessionState()
    }
  }

  async function fetchUserInfo() {
    const info = await currentUser()
    setUserInfo(info)
    return info
  }

  return {
    token,
    userInfo,
    permissions,
    isLoggedIn,
    userName,
    userRole,
    setUserInfo,
    syncAccessToken,
    clearSession: clearSessionState,
    login,
    logout,
    fetchUserInfo,
  }
})
