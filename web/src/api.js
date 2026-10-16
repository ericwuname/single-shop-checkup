// 统一的取数口。所有页面只通过这里跟后端说话，
// 将来后端地址变了（M4 上云）只改这一个文件。

const BASE = import.meta.env.VITE_API_BASE || ''

async function request(path, { method = 'GET', body } = {}) {
  const res = await fetch(BASE + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })

  if (res.status === 204) return null

  const text = await res.text()
  const data = text ? JSON.parse(text) : null

  if (!res.ok) {
    const detail = data && data.detail
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg).join('；')
      : detail || `请求失败（HTTP ${res.status}）`
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
