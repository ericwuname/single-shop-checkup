import { createApp } from 'vue'
import Vant from 'vant'
import 'vant/lib/index.css'
import './style.css'
import App from './App.vue'

createApp(App).use(Vant).mount('#app')

// ---- M7 WP-4：Service Worker 注册（docs/08 §7.4）----
// 仅在支持 SW 且安全上下文（https 或 localhost）时注册；
// 整个过程 try/catch，失败只 console.warn，**绝不影响应用本身使用**。
if (
  'serviceWorker' in navigator &&
  (location.protocol === 'https:' || location.hostname === 'localhost')
) {
  let refreshing = false
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    // 新 SW 接管时刷新一次拿到新静态资源。用变量防循环刷新。
    if (refreshing) return
    refreshing = true
    location.reload()
  })

  navigator.serviceWorker.register('/sw.js').catch((err) => {
    console.warn('Service Worker 注册失败（不影响使用）:', err)
  })
}
