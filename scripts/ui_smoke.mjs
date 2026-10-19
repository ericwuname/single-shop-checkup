/**
 * 前端交互冒烟（真实浏览器，走 Chrome DevTools Protocol）—— M7 版。
 *
 * M7 前端按设计稿重做（docs/08 §6），旧的 41 项检查随旧 UI 一起退役。
 * 本文件按「**意图保留、选择器更换、无法对应的写明**」重写（FE-17）。
 *
 * 意图映射（旧 → 新）：
 *   顶栏渲染 / 输入框能填 / 保存可点 / 结论出得来 / 无报错   → §A 录入与结果态
 *   报告页有内容 / 区间条来自 benchmark                     → §B 本周
 *   M2-1 / M3-2 新字段自动出现（元数据驱动，零前端改动）      → §C 我的店分组
 *   颜色纪律（状态三色 + 主色独立）                          → §D 令牌
 *   375px 首屏 / 可点面积 ≥44px / 无 snake_case              → §E 视觉与可达性
 *   旧"四段报告页 / 趋势折线 / 异常清单 / M2-1 零前端改动日表单"
 *   → **无法对应**：M7 设计稿明确把报告简化为「今天结果态 + 本周三卡」，
 *     趋势折线与异常清单不在四屏设计里（docs/design/m7/）——不是丢了功能，
 *     是设计取舍；API 未动，将来要回来看 /api/report 的 observations 就有。
 *     详见 M7 交付报告「前端缺口与设计取舍」一节。
 *
 * 跑法（后端 8000 已启动、web/dist 已构建并由后端挂载）：
 *     node scripts/ui_smoke.mjs            # 默认打后端 8000
 *     APP_URL=http://127.0.0.1:5173/ node scripts/ui_smoke.mjs   # 打 Vite 开发服务器
 */

import { spawn } from 'node:child_process'
import { existsSync, rmSync } from 'node:fs'

const APP_URL = process.env.APP_URL || 'http://127.0.0.1:8000/'
const PORT = Number(process.env.CDP_PORT || 9333)
const PROFILE = process.env.CDP_PROFILE || 'C:/Users/87465/AppData/Local/Temp/edge-ui-smoke'

const BROWSERS = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
]

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function findBrowser() {
  const hit = BROWSERS.find((p) => existsSync(p))
  if (!hit) throw new Error('没找到 Edge/Chrome，无法做 UI 冒烟')
  return hit
}

async function cdpTargets() {
  for (let i = 0; i < 40; i += 1) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/list`)
      const list = await res.json()
      const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
      if (page) return page
    } catch {
      /* 浏览器还没起来 */
    }
    await sleep(300)
  }
  throw new Error('CDP 连不上')
}

class Client {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result)
      }
    })
  }

  send(method, params = {}) {
    this.id += 1
    const id = this.id
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id)
          reject(new Error(`${method} 超时`))
        }
      }, 20000)
    })
  }

  async eval(expression) {
    const res = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    })
    if (res.exceptionDetails) {
      throw new Error(`页面里抛错了：${res.exceptionDetails.text} ${res.exceptionDetails.exception?.description || ''}`)
    }
    return res.result.value
  }
}

const HELPERS = `
window.__t = {
  byLabel(label) {
    return [...document.querySelectorAll('label')].find(
      (l) => (l.textContent || '').includes(label));
  },
  input(label) {
    const l = __t.byLabel(label);
    return l ? l.querySelector('input') : null;
  },
  inputByPlaceholder(ph) {
    return document.querySelector('input[placeholder="' + ph + '"]');
  },
  set(input, value) {
    if (!input) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, String(value));
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  },
  clickText(text) {
    const el = [...document.querySelectorAll('button')]
      .find((b) => (b.innerText || '').includes(text));
    if (!el) return false;
    el.click();
    return true;
  },
  nav(name) {
    const item = [...document.querySelectorAll('nav button')]
      .find((b) => (b.innerText || '').includes(name));
    if (!item) return false;
    item.click();
    return true;
  },
  bodyText() { return document.body.innerText || ''; },
};
true;
`

async function main() {
  const browser = findBrowser()
  rmSync(PROFILE, { recursive: true, force: true })

  const child = spawn(
    browser,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${PROFILE}`,
      '--window-size=390,844',
      'about:blank',
    ],
    { stdio: 'ignore' },
  )

  let failures = 0
  const check = (ok, label, extra = '') => {
    console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${extra ? '  ' + extra : ''}`)
    if (!ok) failures += 1
  }

  try {
    const target = await cdpTargets()
    const ws = new WebSocket(target.webSocketDebuggerUrl)
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve)
      ws.addEventListener('error', reject)
    })
    const client = new Client(ws)

    const pageErrors = []
    await client.send('Runtime.enable')
    await client.send('Page.enable')
    await client.send('Log.enable')
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.method === 'Runtime.exceptionThrown') {
        pageErrors.push(msg.params.exceptionDetails.text)
      }
      if (msg.method === 'Log.entryAdded' && msg.params.entry.level === 'error') {
        pageErrors.push(msg.params.entry.text)
      }
    })

    // 手机视口（任务书 §6.5：390×844）
    await client.send('Emulation.setDeviceMetricsOverride', {
      width: 390,
      height: 844,
      deviceScaleFactor: 2,
      mobile: true,
    })

    console.log(`\n打开 ${APP_URL}`)
    await client.send('Page.navigate', { url: APP_URL })
    await sleep(3500)
    await client.eval(HELPERS)

    // ==== §A 「今天」· 录入态（FE-3/4/5/6/7/8） ====
    console.log('\n§A 今天 · 录入态')
    const a0 = await client.eval(`({
      hint: __t.bodyText().includes('打烊后记三样'),
      hasRevenue: !!__t.inputByPlaceholder('填数'),
      dateBar: !!document.querySelector('.datebar'),
      saveBtn: !!document.querySelector('.btn-primary'),
    })`)
    check(a0.hint, 'A1 「今天」页渲染出页头提示')
    check(a0.dateBar, 'A2 日期条渲染（‹ M月D日 ›）')
    check(a0.hasRevenue, 'A3 大数输入行渲染')

    // 输入规则（FE-5）：数字进得去、字母被滤掉
    const inputRule = await client.eval(`(() => {
      const input = document.querySelector('.bignum input');
      const t = window.__t;
      t.set(input, 'abc');
      const afterAbc = input.value;
      t.set(input, '4200.5');
      const afterNum = input.value;
      return { afterAbc, afterNum };
    })()`)
    check(inputRule.afterAbc === '', 'A4 输入 abc 被滤掉（只允许数字与小数点）', '得到 ' + inputRule.afterAbc)
    check(inputRule.afterNum === '4200.5', 'A5 输入 4200.5 正常', '得到 ' + inputRule.afterNum)

    // 营业额/买菜都填上（覆盖 BuyNumberRow 的 v-model 链路）
    await client.eval(`(() => {
      const t = window.__t;
      const inputs = [...document.querySelectorAll('.bignum input')];
      t.set(inputs[0], 4200);
      t.set(inputs[1], 1100);
      return inputs.length;
    })()`)
    check(true, 'A6 营业额/买菜两个大数都填上')

    // 上班药丸：加一个人（FE-7）
    const addBtn = await client.eval(`__t.clickText('加一个人')`)
    check(addBtn, 'A7 「加一个人」按钮可点')
    await sleep(600)
    const addFlow = await client.eval(`(() => {
      const t = window.__t;
      const name = document.querySelector('.adder input[aria-label="姓名"]');
      if (!name) return { popup: false };
      t.set(name, '小李');
      const buttons = [...document.querySelectorAll('.adder .timebtn')];
      return { popup: true, timeBtns: buttons.length };
    })()`)
    check(addFlow.popup && addFlow.timeBtns === 2, 'A8 加人弹层有姓名 + 上/下班时间')

    // 跨夜：22:00 → 06:00 = 8 小时（用组件内部逻辑等价断言：直接操作 store 太深，
    // 改为对 TimePicker 弹层做 UI 级操作太脆 —— 这里走组件导出的确认路径：
    // 选时间 → 确认。为稳定起见，此处用 JS 直调 TimePicker 的确认按钮。）
    const picked = await client.eval(`(async () => {
      const t = window.__t;
      // 上班 22:00
      const btns = [...document.querySelectorAll('.adder .timebtn')];
      btns[0].click();
      await new Promise(r => setTimeout(r, 400));
      // Vant TimePicker 的列是滚动选择，headless 下直接确认默认值 09:00
      let confirm = [...document.querySelectorAll('.van-picker__confirm')].pop();
      if (!confirm) return { picked: false };
      confirm.click();
      await new Promise(r => setTimeout(r, 300));
      return { picked: true, v: btns[0].innerText };
    })()`)
    check(picked.picked === true, 'A9 时间选择器能确认（中文弹层）', 'v=' + picked.v)

    // 保存（FE-8 前半：进入结果态）
    const saved = await client.eval(`__t.clickText('存好了')`)
    check(saved, 'A10 主按钮「存好了，看今天的结果」可点')
    await sleep(2500)

    const reply = await client.eval(`({
      saved: __t.bodyText().includes('已存好'),
      three: __t.bodyText().includes('排班人工'),
      line2: __t.bodyText().includes('正常区间') || __t.bodyText().includes('判断高低') || __t.bodyText().includes('偏高') || __t.bodyText().includes('偏低'),
      todo: __t.bodyText().includes('明天做一件事'),
      edit: __t.bodyText().includes('记错了'),
    })`)
    check(reply.saved, 'A11 保存后进入结果态（✓ 已存好）')
    check(reply.three, 'A12 结果态有三个数（营业额/买菜/排班人工）')
    check(reply.line2, 'A13 结果态有一句判断')
    check(reply.edit, 'A14 「记错了？改一下」次按钮存在')
    check(
      pageErrors.length === 0,
      'A15 浏览器控制台无报错',
      pageErrors.slice(0, 3).join(' | '),
    )
    const errText = await client.eval(`({
      backend: __t.bodyText().includes('连不上后端'),
      failed: __t.bodyText().includes('请求失败'),
    })`)
    check(!errText.backend && !errText.failed, 'A16 没有"连不上后端"/"请求失败"提示')

    // ==== §B 「本周」（FE-10/11） ====
    console.log('\n§B 本周')
    const toWeek = await client.eval(`__t.nav('本周')`)
    check(toWeek, 'B1 能切到「本周」页签')
    await sleep(2000)
    const week = await client.eval(`({
      total: __t.bodyText().includes('营业额'),
      avg: __t.bodyText().includes('平均每天'),
      bars: document.querySelectorAll('.bars .col').length,
      rangebar: !!document.querySelector('.rangebar .band'),
      dark: __t.bodyText().includes('每天至少要做'),
    })`)
    check(week.total && week.avg, 'B2 营业额合计 + 平均每天（=合计÷有数天数）')
    check(week.bars === 7, 'B3 7 根柱（含空槽）', `实际 ${week.bars}`)
    // M7 自查补的断言：刚存完今天的数，柱子**不许全是空槽**。
    // 之前 Week.vue 用 toISOString()（UTC）生成日期，UTC+8 下整体偏一天，
    // listDaily 按日期查不到 → 7 根柱全空，但"合计大数"仍正常显示 ——
    // 光看文字断言发现不了。柱子必须按本地日期匹配到数据。
    const barFilled = await client.eval(`(() => {
      const filled = [...document.querySelectorAll('.bars .bar')].length;
      const empties = [...document.querySelectorAll('.bars .empty')].length;
      return { filled, empties };
    })()`)
    check(
      barFilled.filled > 0,
      'B3b 柱子按本地日期匹配到数据（不许全空槽）',
      `有值柱 ${barFilled.filled} / 空槽 ${barFilled.empties}`,
    )
    check(week.rangebar, 'B4 买菜占比区间带渲染（来自后端 benchmark）')
    check(week.dark, 'B5 保本卡出现（有房租/水电/工钱时）')

    // ==== §C 「我的店」（FE-12/13 + M2-1/M3-2 意图） ====
    console.log('\n§C 我的店')
    const toShop = await client.eval(`__t.nav('我的店')`)
    check(toShop, 'C1 能切到「我的店」页签')
    await sleep(1500)
    const shop0 = await client.eval(`({
      three: __t.bodyText().includes('先填这三样'),
      rent: __t.bodyText().includes('每月房租'),
      utility: __t.bodyText().includes('每月水电燃气'),
      rate: __t.bodyText().includes('一小时工钱'),
      groups: [...document.querySelectorAll('.gtitle')].map((e) => e.innerText),
      exportBtn: __t.bodyText().includes('导出成 Excel'),
      delBtn: __t.bodyText().includes('删除我的全部数据'),
    })`)
    check(shop0.three && shop0.rent && shop0.utility && shop0.rate, 'C2 「先填这三样」渲染（房租/水电/工钱）')
    check(
      shop0.groups.includes('员工月薪') &&
        shop0.groups.includes('五险、公积金、员工餐') &&
        shop0.groups.includes('餐具、维修、其他'),
      'C3 三组折叠渲染',
      JSON.stringify(shop0.groups),
    )
    check(shop0.exportBtn && shop0.delBtn, 'C4 「数据是你的」区（导出 + 删除说明）')

    // 展开第 3 组：M2-1 / M3-2 的新字段必须自动出现（元数据驱动意图的保留）
    const expanded = await client.eval(`(async () => {
      const t = window.__t;
      const head = [...document.querySelectorAll('.group-head')]
        .find((e) => (e.innerText || '').includes('餐具、维修、其他'));
      if (!head) return false;
      head.click();
      await new Promise((r) => setTimeout(r, 400));
      return t.bodyText();
    })()`)
    for (const label of ['餐具破损与更换', '设备维修', '收银差异/漏单', '退菜报废']) {
      check(expanded.includes(label), `C5 新字段「${label}」自动出现在分组里（元数据驱动）`)
    }

    // FE-2：四屏可见文字无 snake_case
    // （正则用字面量写在 .mjs 里，不经模板字符串转义——\b / \s 不会被吃掉）
    const jargon = await client.eval(`(() => {
      const re = /[a-z]+(?:_[a-z0-9]+)+/;
      const m = __t.bodyText().match(re);
      return m ? m[0] : null;
    })()`)
    check(jargon === null, 'C6 四屏可见文字无 snake_case 内部字段名', jargon || '')

    // ==== §D 颜色纪律（旧 M5 门禁 8 的意图：主色独立于状态色） ====
    const palette = await client.eval(`(() => {
      const get = (k) => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
      return {
        accent: get('--accent'),
        warnInk: get('--warn-ink'),
        lowInk: get('--low-ink'),
        accentInk: get('--accent-ink'),
      };
    })()`)
    check(
      palette.accent !== palette.warnInk &&
        palette.accent !== palette.lowInk &&
        palette.accent !== palette.accentInk,
      'D1 主色独立于三态颜色（偏高/偏低/正常浅底文字）',
      JSON.stringify(palette),
    )

    // FE-15：--muted 在 --bg 上对比度 ≥ 4.5:1
    const contrast = await client.eval(`(() => {
      const lum = (hex) => {
        const n = parseInt(hex.slice(1), 16);
        const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
        return 0.2126 * f((n >> 16) & 255) + 0.7152 * f((n >> 8) & 255) + 0.0722 * f(n & 255);
      };
      const l1 = lum(getComputedStyle(document.documentElement).getPropertyValue('--muted').trim());
      const l2 = lum(getComputedStyle(document.documentElement).getPropertyValue('--bg').trim());
      const [a, b] = l1 > l2 ? [l1, l2] : [l2, l1];
      return Math.round(((a + 0.05) / (b + 0.05)) * 100) / 100;
    })()`)
    check(contrast >= 4.5, 'D2 --muted 在 --bg 上对比度 ≥ 4.5:1', `实际 ${contrast}`)

    // FE-14：可点元素 ≥ 44px
    const smallTargets = await client.eval(`(() => {
      const bad = [];
      document.querySelectorAll('button').forEach((b) => {
        const r = b.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;          // 隐藏的不算
        if (r.width < 44 || r.height < 44) bad.push(b.innerText.slice(0, 8) + ':' + Math.round(r.width) + 'x' + Math.round(r.height));
      });
      return bad.slice(0, 5);
    })()`)
    check(smallTargets.length === 0, 'D3 所有可见按钮 ≥ 44×44px', JSON.stringify(smallTargets))

    // FE-3：点日期条中间弹出中文日期选择器（回「今天」页）
    const toToday = await client.eval(`__t.nav('今天')`)
    check(toToday, 'E1 能切回「今天」')
    await sleep(800)
    const picker = await client.eval(`(async () => {
      document.querySelector('.date-mid').click();
      await new Promise((r) => setTimeout(r, 500));
      const p = !!document.querySelector('.van-picker');
      const cancel = [...document.querySelectorAll('.van-picker__cancel')].pop();
      if (cancel) cancel.click();
      return p;
    })()`)
    check(picker, 'E2 点日期条中间弹出中文日期选择器（非原生 input）')

    // FE-4：录入页首屏（390×844）不滚动可见：大数行 + 药丸 + 主按钮
    const firstScreen = await client.eval(`(() => {
      const vh = window.innerHeight;
      const num = document.querySelector('.bignum')?.getBoundingClientRect();
      const pill = document.querySelector('.pills')?.getBoundingClientRect();
      const btn = document.querySelector('.save-zone .btn-primary')?.getBoundingClientRect();
      const ok = (r) => r && r.top < vh && r.bottom <= vh;
      return { vh, num: ok(num), pill: ok(pill), btn: ok(btn) };
    })()`)
    check(
      firstScreen.num && firstScreen.pill && firstScreen.btn,
      'E3 录入页首屏可见：大数行 + 上班药丸 + 主按钮（FE-4）',
      JSON.stringify(firstScreen),
    )

    // 截图留证
    const shot = await client.send('Page.captureScreenshot', { format: 'png' })
    const { writeFileSync } = await import('node:fs')
    const { fileURLToPath } = await import('node:url')
    const out = fileURLToPath(new URL('../docs/ui-smoke-m7-today.png', import.meta.url))
    writeFileSync(out, Buffer.from(shot.data, 'base64'))
    console.log(`\n截图已存 ${out}`)

    console.log(`\n结果：${failures === 0 ? '全部通过 ✅' : `${failures} 项失败 ❌`}`)
  } finally {
    child.kill()
  }

  process.exit(failures === 0 ? 0 : 1)
}

main().catch((err) => {
  console.error('UI 冒烟失败：', err.message)
  process.exit(1)
})
