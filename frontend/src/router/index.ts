/** 前端路由配置和页面访问控制。 */

import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '../store/modules/user'
import pinia from '../store'
import UserPage from '../pages/user/index.vue'
import UserLoginPage from '../pages/user/login.vue'
import AdminShell from '../pages/admin/AdminShell.vue'
import AdminLoginView from '../pages/admin/login.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'user', component: UserPage },
    { path: '/login', name: 'user-login', component: UserLoginPage },
    { path: '/admin/login', name: 'admin-login', component: AdminLoginView },
    {
      path: '/admin',
      component: AdminShell,
      meta: { requiresAuth: true },
      children: [
        { path: '', redirect: { name: 'admin-dashboard' } },
        {
          path: 'dashboard',
          name: 'admin-dashboard',
          component: () => import('../pages/admin/dashboard/index.vue'),
          meta: { title: '仪表盘' },
        },
        {
          path: 'libraries',
          name: 'admin-libraries',
          component: () => import('../pages/admin/libraries/index.vue'),
          meta: { title: '知识库管理' },
        },
        {
          path: 'documents',
          name: 'admin-documents',
          component: () => import('../pages/admin/documents/index.vue'),
          meta: { title: '文档管理' },
        },
        {
          path: 'users',
          name: 'admin-users',
          component: () => import('../pages/admin/users/index.vue'),
          meta: { title: '用户管理' },
        },
        {
          path: 'roles',
          name: 'admin-roles',
          component: () => import('../pages/admin/roles/index.vue'),
          meta: { title: '角色管理' },
        },
        {
          path: 'permissions',
          name: 'admin-permissions',
          component: () => import('../pages/admin/permissions/index.vue'),
          meta: { title: '权限管理' },
        },
        {
          path: 'sessions',
          name: 'admin-sessions',
          component: () => import('../pages/admin/sessions/index.vue'),
          meta: { title: '操作日志' },
        },
        {
          path: 'settings',
          name: 'admin-settings',
          component: () => import('../pages/admin/settings/index.vue'),
          meta: { title: '系统设置', requiresAdmin: true },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  const userStore = useUserStore(pinia)
  if (to.meta.requiresAuth && !userStore.isLoggedIn) return { name: 'admin-login' }
  if (to.meta.requiresAdmin && !userStore.userInfo?.roles.includes('admin')) return { name: 'admin-dashboard' }
  if (to.name === 'admin-login' && userStore.isLoggedIn) return { name: 'admin-dashboard' }
  if (to.name === 'user-login' && userStore.isLoggedIn) return { name: 'user' }
  return true
})

export default router
