<!--
 * @FilePath: @/pages/admin/login.vue
 * @Author: 项目维护者
 * @Date: 2026-09-28
 * @Description: 管理端登录页，凭据校验后进入后台布局壳。
 * @BusinessRule: “记住我”仅控制浏览器登录会话持久化，退出不清除凭据偏好。
-->
<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { LockOnIcon, UserIcon } from 'tdesign-icons-vue-next'
import { useUserStore } from '../../store/modules/user'

const router = useRouter()
const userStore = useUserStore()
const username = ref('')
const password = ref('')
const loginError = ref('')
const loginLoading = ref(false)
const rememberLogin = ref(true)

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
    await router.replace({ name: 'admin-dashboard' })
  } catch (error) {
    loginError.value = error instanceof Error ? error.message : '登录失败'
  } finally {
    loginLoading.value = false
  }
}
</script>

<template>
  <div class="admin-login-page">
    <div class="login-brand">
      <div class="admin-mark">工</div>
      <div>
        <strong>工智库</strong>
        <span>管理控制台</span>
      </div>
    </div>

    <div class="login-card">
      <div class="eyebrow">ADMIN CONSOLE</div>
      <h1>登录管理端</h1>
      <p class="login-subtitle">使用已授权账号登录后管理知识库内容、用户与权限。</p>

      <t-form class="login-form" autocomplete="off" @submit="login">
        <div class="login-field">
          <t-input
            v-model="username"
            name="admin-login-account"
            autocomplete="off"
            placeholder="账号"
            clearable
            autofocus
          >
            <template #prefix-icon><UserIcon /></template>
          </t-input>
        </div>
        <div class="login-field">
          <t-input
            v-model="password"
            name="password"
            type="password"
            autocomplete="new-password"
            placeholder="登录密码"
            clearable
            @enter="login"
          >
            <template #prefix-icon><LockOnIcon /></template>
          </t-input>
        </div>
        <div class="login-options">
          <t-checkbox v-model="rememberLogin">记住我</t-checkbox>
        </div>
        <div v-if="loginError" class="login-error" role="alert">{{ loginError }}</div>
        <t-button theme="primary" block size="large" type="submit" :loading="loginLoading"> 登录管理端 </t-button>
      </t-form>
    </div>

    <p class="login-footer">工智库 · 设备技术知识平台</p>
  </div>
</template>

<style scoped>
.admin-login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  align-content: center;
  gap: 18px;
  padding: 24px;
  background: var(--bg);
  color: var(--text);
}
.login-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 5px;
}
.login-brand > div:last-child {
  display: grid;
  gap: 2px;
}
.login-brand strong {
  font-size: 17px;
}
.login-brand span {
  color: var(--muted);
  font-size: 11px;
}
.login-card {
  box-sizing: border-box;
  width: min(100%, 420px);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 32px;
  box-shadow: var(--shadow-card);
}
.login-card h1 {
  font-size: 24px;
  margin: 8px 0 6px;
}
.login-subtitle {
  color: var(--muted);
  font-size: 13px;
  line-height: 21px;
  margin: 0 0 26px;
}
.login-card .login-form {
  display: grid;
  gap: 16px;
}
.login-field {
  min-width: 0;
}
.login-field .t-input {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
}
.login-field .t-input__inner {
  font-size: 14px;
}
.login-field .t-input__prefix-icon {
  color: var(--weak);
}
.login-options {
  display: flex;
  align-items: center;
  min-height: 20px;
}
.login-options .t-checkbox__label {
  color: var(--text-soft);
  font-size: 13px;
}
.login-error {
  min-height: 38px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  color: var(--danger-deep);
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  border-radius: 4px;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 18px;
}
.login-card .t-button {
  margin-top: 2px;
}
.login-footer {
  color: var(--weak);
  font-size: 11px;
}
@media (max-width: 480px) {
  .login-card {
    padding: 24px 20px;
  }
}
</style>
