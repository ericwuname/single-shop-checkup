/**
 * M7 WP-4 Service Worker（docs/08 §7.3，逐条照做）。
 *
 * ## 缓存策略一句话
 *
 *   静态资源（文件名带哈希）→ 缓存优先；页面导航 → 网络优先、失败回退首页；
 *   **/api/ 一律不缓存** —— 报告数据以服务端为准，缓存接口响应会让老板
 *   看到"昨天的数"，那是拿假数据做经营判断，比白屏更糟。
 */

// ① 每次发版手动加一（写进 M7 手机试用手册的"发版步骤"）
const CACHE = 'checkup-v1'

// ② install：抢立即激活。不做预缓存清单 —— 资源文件名带哈希，
//    预缓存清单要构建时生成，本轮不搞（docs/08 §7.3 第 2 条）。
self.addEventListener('install', () => {
  self.skipWaiting()
})

// ③ activate：清掉所有不等于当前版本号的旧缓存；接管已打开的页面
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const req = event.request

  // 非 GET → 不处理（直接走网络）
  if (req.method !== 'GET') return

  const url = new URL(req.url)

  // 跨域请求 → 不处理
  if (url.origin !== self.location.origin) return

  // ⚠️ /api/ 开头 → 不处理（必须走网络，绝不缓存接口响应）。
  //    这条字符串必须出现在源码里，验收脚本会检查（accept_m7 3.10/3.11）。
  if (url.pathname.startsWith('/api/')) return

  // 页面导航 → 网络优先，失败回退缓存里的首页（断网也能打开壳）
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone()
          caches.open(CACHE).then((c) => c.put('/', copy))
          return res
        })
        .catch(() => caches.match('/')),
    )
    return
  }

  // /assets/ 与 /icons/ 静态文件 → 缓存优先（文件名带哈希，内容不变），未命中取网络并写缓存
  if (url.pathname.startsWith('/assets/') || url.pathname.startsWith('/icons/')) {
    event.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req).then((res) => {
            const copy = res.clone()
            caches.open(CACHE).then((c) => c.put(req, copy))
            return res
          }),
      ),
    )
  }
})
