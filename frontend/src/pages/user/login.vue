<!--
 * @FilePath: frontend/src/pages/user/login.vue
 * @Date: 2026-09-07
 * @Description: 用户端登录页面，负责账号认证和登录后跳转。
 -->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { LockOnIcon, UserIcon } from 'tdesign-icons-vue-next'
import { useUserStore } from '../../store/modules/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const username = ref('')
const password = ref('')
const rememberLogin = ref(true)
const loginError = ref('')
const loginLoading = ref(false)

const subtitle = computed(() => route.query.reason === 'anonymous-limit'
  ? '匿名体验已达到 3 条消息，请登录后继续咨询。'
  : '登录后即可继续使用工智库进行技术咨询。')

function getRedirectPath() {
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
  return redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/'
}

async function login() {
  if (loginLoading.value) return
  loginError.value = ''
  if (!username.value.trim() || !password.value) {
    loginError.value = '请输入账号和密码'
    return
  }

  loginLoading.value = true
  try {
    await userStore.login(username.value.trim(), password.value, rememberLogin.value)
    password.value = ''
    await router.replace(getRedirectPath())
  } catch (error) {
    loginError.value = error instanceof Error ? error.message : '登录失败'
  } finally {
    loginLoading.value = false
  }
}
</script>

<template>
  <main class="user-login-page">
    <section class="user-login-card">
      <div class="user-login-brand">
        <div class="user-login-mark">工</div>
        <div>
          <strong>工智库</strong>
          <span>设备技术知识平台</span>
        </div>
      </div>
      <div class="user-login-eyebrow">USER ACCESS</div>
      <h1>登录工智库</h1>
      <p class="user-login-subtitle">{{ subtitle }}</p>

      <t-form class="user-login-form" autocomplete="off" @submit="login">
        <t-input v-model="username" name="user-login-account" autocomplete="off" placeholder="账号" clearable autofocus>
          <template #prefix-icon><UserIcon /></template>
        </t-input>
        <t-input v-model="password" name="user-login-password" type="password" autocomplete="new-password" placeholder="登录密码" clearable @enter="login">
          <template #prefix-icon><LockOnIcon /></template>
        </t-input>
        <t-checkbox v-model="rememberLogin">保持登录状态</t-checkbox>
        <div v-if="loginError" class="user-login-error" role="alert">{{ loginError }}</div>
        <t-button theme="primary" block size="large" type="submit" :loading="loginLoading">登录并继续</t-button>
      </t-form>

      <t-button variant="text" block @click="router.replace('/')">暂不登录，返回用户端</t-button>
    </section>
    <p class="user-login-footer">工智库 · 本地知识库问答</p>
  </main>
</template>

<style scoped>
.user-login-page { min-height: 100vh; display: grid; place-items: center; align-content: center; gap: 18px; padding: 24px; background: #f7f8fa; color: #1f2937; }
.user-login-card { box-sizing: border-box; width: min(100%, 420px); background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 32px; box-shadow: 0 12px 35px rgb(15 23 42 / 7%); }
.user-login-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 28px; }.user-login-brand > div:last-child { display: grid; gap: 2px; }.user-login-brand strong { font-size: 17px; }.user-login-brand span { color: #6b7280; font-size: 11px; }.user-login-mark { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 8px; color: #fff; font-weight: 700; background: #2563eb; }
.user-login-eyebrow { color: #6b7280; font-size: 11px; letter-spacing: .04em; }.user-login-card h1 { font-size: 24px; margin: 8px 0 6px; }.user-login-subtitle { color: #6b7280; font-size: 13px; line-height: 21px; margin: 0 0 26px; }.user-login-form { display: grid; gap: 16px; }.user-login-error { box-sizing: border-box; color: #b91c1c; background: #fef2f2; border: 1px solid #fecaca; border-radius: 4px; padding: 8px 10px; font-size: 12px; line-height: 18px; }.user-login-footer { color: #9ca3af; font-size: 11px; }
@media (max-width: 480px) { .user-login-card { padding: 24px 20px; } }
</style>