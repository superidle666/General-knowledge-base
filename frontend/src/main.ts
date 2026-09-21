/** Vue 应用启动入口：注册路由、TDesign 组件和全局样式。 */
import { createApp } from 'vue'
import TDesign from 'tdesign-vue-next'
import 'tdesign-vue-next/es/style/index.css'
import './style/index.less'
import App from './App.vue'
import router from './router'
import pinia from './store'

const app = createApp(App)
app.use(pinia)
app.use(TDesign)
app.use(router)
app.mount('#app')
