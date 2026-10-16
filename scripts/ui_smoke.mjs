/**
 * 前端交互冒烟（真实浏览器，走 Chrome DevTools Protocol）。
 *
 * 为什么要有这个：`vite build` 通过只说明"能编译"，不说明"点得动"。
 * 施工单 §4 的门禁原文是"手机模式可提交、有结果"——
 * 这条必须用真浏览器点一遍才算数。
 *
 * 依赖：本机 Edge/Chrome 任一个；Node 22 自带 WebSocket，无需装包。
 *
 * 跑法（后端 8000 + Vite 5173 都已启动）：
 *     node scripts/ui_smoke.mjs
 */

import { spawn } from 'node:child_process'
import { existsSync, rmSync } from 'node:fs'

const APP_URL = process.env.APP_URL || 'http://127.0.0.1:5173/'
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
  field(label) {
    const cell = [...document.querySelectorAll('.van-cell')]
      .find((c) => (c.innerText || '').trim().startsWith(label));
    return cell ? cell.querySelector('input') : null;
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

    // 手机视口（施工单要验证的是手机模式）
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

    const shell = await client.eval(`({
      title: document.querySelector('.van-nav-bar__title')?.innerText || '',
      hasFields: document.querySelectorAll('.van-field__control').length,
      bodyLen: document.body.innerText.length,
    })`)
    check(shell.title.length > 0, '页面渲染出顶栏', `title=${shell.title}`)
    check(shell.hasFields >= 3, '日口径输入框已从注册表渲染', `输入框=${shell.hasFields}`)

    // ---- 填今天三个数 ----
    console.log('\n录入今天三个数 + 一条排班')
    const filled = await client.eval(`(() => {
      const t = window.__t;
      const r = {};
      r.revenue = t.set(t.field('营业额'), 4200);
      r.purchase = t.set(t.field('买菜进货'), 1100);
      r.orders  = t.set(t.field('订单数'), 140);
      t.clickText('加一个人');
      r.cells = [...document.querySelectorAll('.van-cell')].map((c) => (c.innerText || '').replace(/\\n/g, '|'));
      return r;
    })()`)
    await sleep(600)
    if (!(filled.revenue && filled.purchase && filled.orders)) {
      console.log('  [调试] 页面上认到的 cell 文案：')
      filled.cells.slice(0, 12).forEach((c) => console.log(`         ${JSON.stringify(c)}`))
    }

    const shiftFilled = await client.eval(`(() => {
      const t = window.__t;
      const ok1 = t.set(t.field('谁'), '小李');
      const ok2 = t.set(t.field('从'), '18:00');
      const ok3 = t.set(t.field('到'), '22:00');
      return { name: ok1, start: ok2, end: ok3, rows: document.querySelectorAll('.shift-row').length };
    })()`)
    await sleep(400)
    check(
      filled.revenue && filled.purchase && filled.orders && shiftFilled.name && shiftFilled.start && shiftFilled.end,
      '输入框都能填进去',
      `排班行数=${shiftFilled.rows}`,
    )

    const submitted = await client.eval(`window.__t.clickText('保存')`)
    check(submitted, '保存按钮可点')
    await sleep(2500)

    const after = await client.eval(`(() => {
      const text = document.body.innerText;
      return {
        text,
        hasToast: !!document.querySelector('.van-toast'),
        lines: [...document.querySelectorAll('.line')].map((e) => e.innerText),
        values: [...document.querySelectorAll('.van-cell__value')].map((e) => e.innerText),
      };
    })()`)

    console.log('\n页面提交后的结论：')
    after.lines.forEach((l, i) => console.log(`  [${i}] ${l}`))

    check(after.lines.length === 3, '结论是三行', `实际 ${after.lines.length} 行`)
    check(after.lines[0]?.includes('营业额'), '第一行是数字')
    check(after.lines[0]?.includes('4,200') || after.lines[0]?.includes('4200'), '营业额已进结论')
    check(!after.text.includes('连不上后端'), '没有连不上后端的提示')
    check(!after.text.includes('请求失败'), '没有请求失败提示')
    check(pageErrors.length === 0, '浏览器控制台无报错', pageErrors.slice(0, 3).join(' | '))

    // ---- 报告页 ----
    const switched = await client.eval(`(() => {
      const item = [...document.querySelectorAll('.van-tabbar-item')]
        .find((e) => (e.innerText || '').includes('报告'));
      if (!item) return false;
      item.click();
      return true;
    })()`)
    check(switched, '能切到报告页签')
    await sleep(3000)
    const reportTab = await client.eval(`({ text: document.body.innerText })`)
    check(reportTab.text.includes('最近 7 天'), '报告页有内容')
    check(reportTab.text.includes('工作日日均'), '报告页有工作日/周末对照')

    // ---- M4-9：四段结构齐全 ----
    const sections = await client.eval(`({
      conclusion: document.querySelectorAll('.conclusion').length,
      lines: document.querySelectorAll('.conclusion .line').length,
      cards: document.querySelectorAll('.grid .mcard').length,
      trend: document.querySelectorAll('svg.trend').length,
      trendLines: document.querySelectorAll('svg.trend polyline').length,
      anomalies: document.querySelectorAll('.anomaly').length,
    })`)
    check(sections.conclusion === 1, '① 结论区存在')
    check(sections.lines === 3, '① 结论严格三行', `实际 ${sections.lines}`)
    check(sections.cards > 0, '② 指标卡网格有卡片', `卡片 ${sections.cards} 张`)
    check(sections.trend === 1, '③ 趋势折线存在（SVG）')
    check(sections.trendLines === 2, '③ 趋势两条线（营业额 vs 保本线）', `实际 ${sections.trendLines}`)
    check(sections.anomalies > 0, '④ 异常清单非空', `${sections.anomalies} 条`)

    // ---- M4-8：区间条 + 指针位置 ----
    const bars = await client.eval(`(() => {
      const cards = [...document.querySelectorAll('.grid .mcard')];
      const withBar = cards.filter((c) => c.querySelector('.bar'));
      const noBar = cards.filter((c) => c.querySelector('.bar-none'));
      const detail = withBar.map((c) => {
        const dot = c.querySelector('.bar-dot');
        const tag = c.querySelector('.tag');
        return {
          code: c.dataset.code,
          position: parseFloat(c.querySelector('.bar').dataset.position),
          leftStyle: dot ? dot.style.left : null,
          dotClass: dot ? dot.className : null,
          // ⚠️ 不要在这里用正则：这段代码是在模板字符串里，
          // \s / \b 会被 JS 的字符串转义吃掉（\s 变成 s），正则会静默变成另一个意思。
          // 踩过两次了，一律改成字符串操作。（注释里也别写反引号，会提前结束模板）
          status: tag
            ? tag.className.trim().split(' ').filter(Boolean)
                .find((cls) => ['high', 'low', 'ok'].indexOf(cls) >= 0) || null
            : null,
          tagClass: tag ? tag.className : null,
          tagText: tag ? tag.innerText : null,
          zone: !!c.querySelector('.bar-zone'),
        };
      });
      return { withBar: withBar.length, noBar: noBar.length, detail };
    })()`)
    check(bars.withBar > 0, 'M4-8 有基准的指标都画了区间条', `${bars.withBar} 张有，${bars.noBar} 张无`)
    check(bars.detail.every((b) => b.zone), 'M4-8 区间条有绿区（正常区间中段）')
    const pointerOk = bars.detail.every(
      (b) => Math.abs(parseFloat(b.leftStyle) - b.position * 100) < 0.01,
    )
    check(pointerOk, 'M4-8 指针位置 = 后端算的 position（误差 < 0.01%）',
      JSON.stringify(bars.detail.slice(0, 3)))
    check(
      bars.detail.every((b) => b.dotClass.includes('ok') || b.dotClass.includes('bad')),
      'M4-8 指针颜色只取 绿(正常)/红(越界)',
    )

    // 更本质的断言：**指针位置必须和判定方向一致**
    //   绿区 = 位置 0.25–0.75；偏高 → 指针在绿区右侧；偏低 → 左侧；正常 → 落在绿区内
    const sideOk = bars.detail.every((b) => {
      if (b.status === 'high') return b.position > 0.75;
      if (b.status === 'low') return b.position < 0.25;
      if (b.status === 'ok') return b.position >= 0.25 && b.position <= 0.75;
      return true;
    });
    check(sideOk, 'M4-8 指针方向与判定一致（偏高在右 / 偏低在左 / 正常在绿区内）',
      JSON.stringify(bars.detail.map((b) => [b.code, b.status, b.position])));
    const badDots = bars.detail.filter((b) => b.status !== 'ok' && !b.dotClass.includes('bad'));
    check(badDots.length === 0, 'M4-8 越界时指针变红');

    // ---- M4-9：异常清单按金额降序 ----
    const anomalyOrder = await client.eval(`(() => {
      const amounts = [...document.querySelectorAll('.anomaly-amount')].map(
        (e) => Number((e.innerText.match(/[\\d,]+/) || ['0'])[0].replace(/,/g, '')));
      return amounts;
    })()`)
    const sortedDesc = anomalyOrder.every((v, i) => i === 0 || anomalyOrder[i - 1] >= v)
    check(sortedDesc, 'M4-9 异常清单按金额降序', JSON.stringify(anomalyOrder))

    // ---- M4-8：颜色纪律（只用红/黄/绿 + 中性灰）----
    const palette = await client.eval(`(() => {
      const get = (k) => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
      const tokens = {
        brand: get('--brand'), danger: get('--danger'),
        ok: get('--ok'), warn: get('--warn'),
        dangerSoft: get('--danger-soft'), okSoft: get('--ok-soft'), warnSoft: get('--warn-soft'),
      };
      // 把 token 值解析成浏览器实际用的 rgb()，才好和计算样式逐个比
      const probe = document.createElement('span');
      document.body.appendChild(probe);
      const rgb = {};
      for (const [k, v] of Object.entries(tokens)) {
        probe.style.color = v;
        const asColor = getComputedStyle(probe).color;
        probe.style.color = '';
        probe.style.backgroundColor = v;
        const asBg = getComputedStyle(probe).backgroundColor;
        probe.style.backgroundColor = '';
        rgb[k] = asBg !== 'rgba(0, 0, 0, 0)' ? asBg : asColor;
      }
      probe.remove();

      const used = new Set();
      document.querySelectorAll(
        '.grid .mcard, .anomaly, .tag, .bar-dot, .flag, .dot, .conclusion .line, .bar-zone, .bar-track',
      ).forEach((el) => {
        const cs = getComputedStyle(el);
        used.add(cs.color);
        if (cs.backgroundColor !== 'rgba(0, 0, 0, 0)') used.add(cs.backgroundColor);
      });
      return { tokens, rgb, used: [...used] };
    })()`)

    // M5 门禁 8：评审推翻了 M4 的"品牌色必须并进 --danger"
    // —— 品牌色不参与状态语义，不该被状态色吞掉（docs/05 §0.1 第 2 条）。
    // 所以这里断言的是**独立**（≠ 三种状态色），而不是**相同**。
    check(
      palette.tokens.brand !== palette.tokens.danger
        && palette.tokens.brand !== palette.tokens.ok
        && palette.tokens.brand !== palette.tokens.warn,
      'M5 品牌色独立于三种状态色',
      JSON.stringify(palette.tokens),
    )
    // 中性色判定用"低饱和度"而不是"三通道相等"：
    // 主题的 ink 是 #1f2329（31,35,41），带一点蓝，但它就是灰。
    const isNeutral = (c) => {
      const m = c.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)/)
      if (!m) return false
      const [r, g, b] = [Number(m[1]), Number(m[2]), Number(m[3])]
      return Math.max(r, g, b) - Math.min(r, g, b) <= 16
    }
    const signal = new Set(Object.values(palette.rgb))
    const offenders = palette.used.filter((c) => !isNeutral(c) && !signal.has(c))
    check(
      offenders.length === 0,
      'M5 页面用色 = 红/黄/绿 + 一个品牌色（+中性灰），没有第五种',
      JSON.stringify(offenders.slice(0, 5)),
    )
    // 状态色这一侧仍然收紧：**只允许红/黄/绿**，品牌色不许出现在状态位上。
    // 上面那条放开了"品牌色独立"，这一条把放开的范围钉死在"非状态位"——
    // 而且它是**指向性断言**：直接查指针（.bar-dot）的实际颜色，
    // 不靠"页面里没别的颜色"这种间接推理（那样就算指针被刷成品牌色也发现不了）。
    const dotColors = await client.eval(`(() => {
      return [...document.querySelectorAll('.bar-dot')].map(
        (e) => getComputedStyle(e).backgroundColor);
    })()`)
    const allowedDot = new Set([palette.rgb.ok, palette.rgb.danger])
    check(
      dotColors.length > 0 && dotColors.every((c) => allowedDot.has(c)),
      'M5 区间条指针只取 绿(正常)/红(越界)，不是品牌色',
      JSON.stringify(dotColors),
    )

    // ---- M4-9：375px 手机视口下，结论三行不滚动可见 ----
    await client.send('Emulation.setDeviceMetricsOverride', {
      width: 375, height: 667, deviceScaleFactor: 2, mobile: true,
    })
    await sleep(800)
    const firstScreen = await client.eval(`(() => {
      const box = document.querySelector('.conclusion').getBoundingClientRect();
      return { bottom: box.bottom, viewport: window.innerHeight, lines: document.querySelectorAll('.conclusion .line').length };
    })()`)
    check(
      firstScreen.bottom <= firstScreen.viewport,
      'M4-9 375px 首屏内能看到完整结论三行',
      `结论底部 ${Math.round(firstScreen.bottom)}px / 视口 ${firstScreen.viewport}px`,
    )
    await client.send('Emulation.setDeviceMetricsOverride', {
      width: 390, height: 844, deviceScaleFactor: 2, mobile: true,
    })
    await sleep(400)

    // ---- 截图留证 ----
    const shot = await client.send('Page.captureScreenshot', { format: 'png' })
    const { writeFileSync } = await import('node:fs')
    const { fileURLToPath } = await import('node:url')
    const out = fileURLToPath(new URL('../docs/ui-smoke-report-tab.png', import.meta.url))
    writeFileSync(out, Buffer.from(shot.data, 'base64'))
    console.log(`\n截图已存 ${out}`)

    // ---- 设置页：M2-1 新增的 5 个字段必须自动长出来（门禁 5：零前端改动）----
    const switchedSettings = await client.eval(`(() => {
      const item = [...document.querySelectorAll('.van-tabbar-item')]
        .find((e) => (e.innerText || '').includes('设置'));
      if (!item) return false;
      item.click();
      return true;
    })()`)
    check(switchedSettings, '能切到设置页签')
    await sleep(1800)
    const settings = await client.eval(`({ text: document.body.innerText })`)
    for (const label of ['全职人数', '全职月薪合计', '五险', '公积金', '福利与员工餐']) {
      check(
        settings.text.includes(label),
        `M2-1 新字段「${label}」自动出现在表单里`,
        '前端文件一行未改',
      )
    }
    // M3-2 又加了 5 个隐性成本字段 —— 同样必须零前端改动就出现
    for (const label of ['员工餐', '餐具破损与更换', '设备维修', '收银差异/漏单', '退菜报废']) {
      check(
        settings.text.includes(label),
        `M3-2 新字段「${label}」自动出现在表单里`,
        '前端文件一行未改',
      )
    }


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
