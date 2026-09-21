/** 用户认证状态：登录用户信息、令牌生命周期和权限信息。 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  authApi,
  clearSession,
  getAccessToken,
  getStoredUser,
  login as loginRequest,
  logout as logoutRequest,
  type AuthUser,
} from '../../api/auth'

export const useUserStore = defineStore('user', () => {
  const token = ref(getAccessToken() || '')
  const userInfo = ref<AuthUser | null>(getStoredUser())
  const permissions = ref<string[]>(userInfo.value?.permissions || [])

  const isLoggedIn = computed(() => Boolean(token.value))
  const userName = computed(() => userInfo.value?.display_name || userInfo.value?.username || '')
  const userRole = computed(() => userInfo.value?.roles?.[0] || '')

  function setToken(nextToken: string) {
    token.value = nextToken
  }

  function clearToken() {
    token.value = ''
    clearSession()
  }

  function setUserInfo(info: AuthUser | null) {
    userInfo.value = info
    permissions.value = info?.permissions || []
  }

  async function login(username: string, password: string, rememberLogin = true) {
    const info = await loginRequest(username, password, rememberLogin)
    token.value = getAccessToken() || ''
    setUserInfo(info)
    return info
  }

  async function logout() {
    try {
      await logoutRequest()
    } finally {
      token.value = ''
      setUserInfo(null)
      clearSession()
    }
  }

  async function fetchUserInfo() {
    const info = await authApi.getUserInfo()
    setUserInfo(info)
    token.value = getAccessToken() || token.value
    return info
  }

  return {
    token,
    userInfo,
    permissions,
    isLoggedIn,
    userName,
    userRole,
    setToken,
    clearToken,
    setUserInfo,
    login,
    logout,
    fetchUserInfo,
  }
})