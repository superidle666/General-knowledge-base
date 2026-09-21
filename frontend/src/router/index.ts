/** 前端路由配置和页面访问控制。 */

import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '../store/modules/user'
import pinia from '../store'
import UserPage from '../pages/user/index.vue'
import UserLoginPage from '../pages/user/login.vue'
import AdminView from '../pages/admin/index.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'user', component: UserPage },
    { path: '/login', name: 'user-login', component: UserLoginPage },
    { path: '/admin/login', name: 'admin-login', component: AdminView },
    { path: '/admin', name: 'admin', component: AdminView, meta: { requiresAuth: true } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  const isLoggedIn = useUserStore(pinia).isLoggedIn
  if (to.meta.requiresAuth && !isLoggedIn) return { name: 'admin-login' }
  if (to.name === 'admin-login' && isLoggedIn) return { name: 'admin' }
  if (to.name === 'user-login' && isLoggedIn) return { name: 'user' }
  return true
})

export default router