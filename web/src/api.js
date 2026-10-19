// 统一的取数口。所有页面只通过这里跟后端说话，
// 将来后端地址变了（M4 上云）只改这一个文件。

const BASE = import.meta.env.VITE_API_BASE || ''

// ---- 访问码（M7 WP-1，docs/08 §4.3）----
// 公网模式下后端要求每个 /api 请求带 X-Access-Key。
// 码只存在浏览器 localStorage（无痕模式会抛异常 → 退化为仅内存保存）。
// 401 = 码错/过期 → 清掉并抛 AccessDenied，让界面回到访问码页；
// 429 = 暴力防护触发 → 原样提示，不重试。
let memoryKey = '' // localStorage 不可用时的兜底

function loadKey() {
  try {
    return localStorage.getItem('access_key') || memoryKey
  } catch {
    return memoryKey
  }
}

function saveKey(key) {
  memoryKey = key || ''
  try {
    if (key) localStorage.setItem('access_key', key)
    else localStorage.removeItem('access_key')
  } catch {
    // 无痕模式写不进就算了 —— 内存里还有这份，本次会话仍可用
  }
}

export class AccessDenied extends Error {
  constructor() {
    super('需要访问码')
    this.name = 'AccessDenied'
  }
}

export function setAccessKey(key) {
  saveKey((key || '').trim())
}

export function hasAccessKey() {
  return loadKey() !== ''
}

async function request(path, { method = 'GET', body } = {}) {
  const key = loadKey()
  const res = await fetch(BASE + path, {
    method,
    headers: {
      ...(body ? { 'Content-Type': 'application/json' } : {}),
      // 访问码只走请求头：绝不放进 URL 查询串（会进日志/浏览器历史）
      ...(key && path.startsWith('/api/') ? { 'X-Access-Key': key } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  })

  if (res.status === 204) return null

  const text = await res.text()
  const data = text ? JSON.parse(text) : null

  if (!res.ok) {
    // 401：码缺失或错误。清掉本地码 —— 留着一个错码，下次还是 401，
    // 用户却看不到输入框（以为已经输过了）。
    if (res.status === 401 && path.startsWith('/api/')) {
      saveKey('')
      throw new AccessDenied()
    }
    const detail = data && data.detail
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg).join('；')
      : detail || `请求失败（HTTP ${res.status}）`
    // 429：暴力防护触发，别重试 —— 越试封得越久
    if (res.status === 429) {
      throw new Error(message || '尝试太多次，请 10 分钟后再试')
    }
    throw new Error(message)
  }
  return data
}

export const api = {
  health: () => request('/api/health'),

  // 元数据：前端表单和指标卡都从注册表渲染，加字段/加指标不用改前端
  metaFields: (scope) => request(`/api/meta/fields${scope ? `?scope=${scope}` : ''}`),
  metaMetrics: () => request('/api/meta/metrics'),

  listShops: () => request('/api/shops'),
  createShop: (payload) => request('/api/shops', { method: 'POST', body: payload }),
  patchShop: (id, payload) => request(`/api/shops/${id}`, { method: 'PATCH', body: payload }),

  getMonthly: (id, month) => request(`/api/shops/${id}/monthly?month=${month}`),
  putMonthly: (id, month, values) =>
    request(`/api/shops/${id}/monthly`, { method: 'PUT', body: { month, values } }),

  putDaily: (payload) => request('/api/daily', { method: 'PUT', body: payload }),
  listDaily: (id, start, end) => request(`/api/daily?shop_id=${id}&start=${start}&end=${end}`),

  report: (id, start, end) => request(`/api/report?shop_id=${id}&start=${start}&end=${end}`),
  weekly: (id, anchor, days = 7) =>
    request(`/api/report/weekly?shop_id=${id}&anchor=${anchor}&days=${days}`),

  // M4-9：近 N 个月趋势（营业额 vs 保本线两条线）
  monthlySeries: (id, months = 12, anchor) =>
    request(
      `/api/report/monthly-series?shop_id=${id}&months=${months}` +
        (anchor ? `&anchor=${anchor}` : ''),
    ),

  // M4-3 / M4-6：分类与多店
  categories: (level) => request(`/api/categories${level ? `?level=${level}` : ''}`),
  categoryCost: (id, start, end, level = 1) =>
    request(`/api/report/category-cost?shop_id=${id}&start=${start}&end=${end}&level=${level}`),
  portfolio: (ownerKey, start, end) =>
    request(
      `/api/portfolio?start=${start}&end=${end}` + (ownerKey ? `&owner_key=${ownerKey}` : ''),
    ),
}

// ---- 日期小工具（避免为一个 format 装 dayjs）----
export function toISO(d) {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export function today() {
  return toISO(new Date())
}

export function monthOf(isoDate) {
  return (isoDate || today()).slice(0, 7)
}

export function shiftDay(isoDate, delta) {
  const d = new Date(`${isoDate}T00:00:00`)
  d.setDate(d.getDate() + delta)
  return toISO(d)
}
