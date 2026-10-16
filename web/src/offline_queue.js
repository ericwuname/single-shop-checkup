/**
 * 断网本地暂存 + 恢复补传（M6-4 §5.4，docs/06）。
 *
 * ## 为什么需要它
 *
 * 小店老板的手机信号差是常态（后厨、地下室、收银台角落）。
 * 原来的实现：保存失败就弹一个 toast，老板以为记上了，其实**数据没了**。
 * 这种"以为存了其实没存"比明确的失败更糟 —— 他第二天看到空页面会怀疑产品，
 * 而且那天的数再也补不回来。
 *
 * ## 设计
 *
 * - 保存失败（网络错误）→ 写进 localStorage 队列，**明确告诉老板"已存本地，连上网自动补"**
 * - 每次启动 + 每次保存成功后 → 尝试补传队列
 * - 队列上限 50 条（防爆 localStorage；超了丢最旧的并提示）
 *
 * ⚠️ 只对 **PUT /api/daily** 做暂存：它是**幂等的覆盖写**（同一天重复保存 = 覆盖），
 * 补传不会造成重复数据。POST 类接口（记一笔采购）补传可能重复，
 * 所以**不暂存** —— 那种失败就明确报错，让老板重来。
 */

const QUEUE_KEY = 'checkup.pendingDaily'
const MAX_QUEUE = 50

function readQueue() {
  try {
    const raw = localStorage.getItem(QUEUE_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function writeQueue(items) {
  try {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(items))
    return true
  } catch {
    // localStorage 满了/被禁 → 返回 false，让调用方明确告知"这次真没了"
    return false
  }
}

/** 入队一条待补传的日报。返回是否成功入队。 */
export function enqueueDaily(payload) {
  const items = readQueue()
  // 同一天同店的旧条目先去掉（覆盖语义，只留最新一次）
  const filtered = items.filter(
    (it) => !(it.shop_id === payload.shop_id && it.stat_date === payload.stat_date),
  )
  filtered.push({ ...payload, queued_at: new Date().toISOString() })

  if (filtered.length > MAX_QUEUE) {
    filtered.splice(0, filtered.length - MAX_QUEUE)
  }
  return writeQueue(filtered)
}

/** 当前待补传条数（用于在 UI 上提示）。 */
export function pendingCount() {
  return readQueue().length
}

/**
 * 尝试补传。返回 { sent, failed }。
 *
 * `sender` 是真正发请求的函数（App.vue 传 api.putDaily），
 * 这样本模块不依赖具体的 api 实现，也方便测试。
 */
export async function flushQueue(sender) {
  const items = readQueue()
  if (!items.length) return { sent: 0, failed: 0 }

  const stillPending = []
  let sent = 0

  for (const item of items) {
    try {
      await sender(item)
      sent += 1
    } catch {
      // 仍然失败 → 留着下次再试（顺序保持，先来的先补）
      stillPending.push(item)
    }
  }

  writeQueue(stillPending)
  return { sent, failed: stillPending.length }
}

/** 网络是否真的不通（区别于"服务器返回 400"这种业务错误）。 */
export function isNetworkError(err) {
  if (!err) return false
  // fetch 在网络层失败时抛 TypeError；我们的 request() 会把 HTTP 错误转成 Error(message)
  const msg = String(err.message || err)
  return (
    err instanceof TypeError ||
    /failed to fetch|networkerror|network request failed/i.test(msg)
  )
}
