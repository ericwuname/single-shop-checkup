<template>
  <!-- 报告页（M4-9 四段结构）：① 结论 ② 指标卡 ③ 趋势 ④ 异常清单。
       前端只做展示，**不生成任何文字** —— 话术全部由后端产出，
       否则同一件事会有两套说法。

       配色纪律：全页只用 红 / 黄 / 绿 + 中性灰，颜色是信号不是装饰。 -->
  <div>
    <!-- ① 结论区：老板不滚动，字号最大 -->
    <section v-if="report" class="block card conclusion">
      <div class="period">
        {{ report.period.start === report.period.end ? '今天' : `${report.period.start} ~ ${report.period.end}` }}
        · 有数 {{ report.period.days_with_data }} 天
      </div>
      <p v-for="(line, i) in report.conclusion.lines" :key="i" :class="['line', `line-${i}`]">
        {{ line }}
      </p>
      <div v-if="report.conclusion.warnings.length" class="warns">
        <div v-for="(w, i) in report.conclusion.warnings" :key="i" class="warn">⚠ {{ w }}</div>
      </div>
    </section>

    <!-- ② 指标卡网格（2 列）：名字 + 大数字 + 区间条 + 一句话 -->
    <section v-if="report && report.metrics.length" class="grid">
      <article
        v-for="m in shownMetrics"
        :key="m.code"
        class="mcard"
        :data-code="m.code"
      >
        <div class="mcard-name">{{ m.name }}</div>
        <div class="mcard-value big-num">{{ m.display }}</div>

        <!-- 有基准才画条；无基准（如保本额）只给数字 -->
        <div v-if="hasBar(m)" class="bar" :data-position="m.benchmark.position">
          <div class="bar-track">
            <div class="bar-zone"></div>
            <div :class="['bar-dot', dotClass(m)]" :style="{ left: (m.benchmark.position * 100) + '%' }"></div>
          </div>
          <div class="bar-scale">
            <span>{{ bound(m.benchmark.low) }}</span>
            <span>{{ bound(m.benchmark.high) }}</span>
          </div>
        </div>
        <div v-else class="bar-none">{{ noBarHint(m) }}</div>

        <div v-if="m.sentence" class="mcard-say">{{ m.sentence }}</div>
        <span v-if="statusText(m)" :class="['tag', m.benchmark.status]">{{ statusText(m) }}</span>
      </article>
    </section>

    <!-- ③ 趋势：近 12 个月，日均营业额 vs 保本线（两个都是"每天多少钱"，才能同轴比） -->
    <section v-if="seriesPoints.length" class="block card">
      <div class="block-title">近 12 个月：你的日均 vs 保本线</div>
      <svg class="trend" viewBox="0 0 320 120" preserveAspectRatio="none" role="img"
           aria-label="近 12 个月日均营业额与保本线趋势">
        <line v-for="g in 4" :key="g" class="grid-line" x1="0" :y1="g * 24" x2="320" :y2="g * 24" />
        <polyline class="line-be" :points="breakevenPath" />
        <polyline class="line-rev" :points="revenuePath" />
      </svg>
      <div class="legend">
        <span><i class="dot rev"></i>日均营业额</span>
        <span><i class="dot be"></i>保本线</span>
        <span class="legend-note">虚线/空心 = 该月没数据</span>
      </div>
    </section>

    <!-- ④ 异常清单：按金额降序 —— 老板先看钱最多的那个 -->
    <section v-if="anomalies.length" class="block card">
      <div class="block-title">异常清单（按影响金额排序）</div>
      <div v-for="(a, i) in anomalies" :key="a.code + a.shop_id" class="anomaly">
        <span class="rank">{{ i + 1 }}</span>
        <span :class="['flag', a.status]">{{ a.status === 'warn' ? '要查' : '越线' }}</span>
        <div class="anomaly-body">
          <div class="anomaly-head">
            <b>{{ a.shop_name ? a.shop_name + ' · ' : '' }}{{ a.name }}</b>
            <span class="anomaly-amount big-num">{{ money(a.amount) }}</span>
          </div>
          <div class="anomaly-say">{{ a.sentence }}</div>
        </div>
      </div>
    </section>

    <div v-if="!report" class="empty">还没有数据。先在上面填今天那三个数。</div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  report: { type: Object, default: null },
  series: { type: Object, default: null },
})

// 指标卡只展示"有值的核心指标 + 支撑量"，原始字段不在报告页铺开
const HIDE_ON_CARD = new Set([
  'revenue_m_avg', 'purchase_m', 'stock_book_amount', 'stock_actual_amount',
  'stock_variance_amount', 'scrap_amount', 'hours', 'labor_cost', 'labor_m',
  'labor_m_full', 'hidden_cost_m', 'book_profit_m', 'revenue', 'purchase',
  'orders', 'rent_m', 'utility_m', 'gross_margin', 'full_time_headcount',
  'full_time_monthly', 'social_insurance_m', 'housing_fund_m', 'welfare_m',
  'staff_meal_m', 'tableware_m', 'repair_m', 'cash_diff_m', 'returned_m',
])

const shownMetrics = computed(() => {
  if (!props.report) return []
  const core = new Set(props.report.metrics.map((m) => m.code))
  return props.report.metrics.filter(
    (m) => !HIDE_ON_CARD.has(m.code) || (m.benchmark && m.benchmark.status !== 'none' && core.has(m.code)),
  )
})

function hasBar(m) {
  const b = m.benchmark
  return !!(b && b.low !== null && b.low !== undefined && b.high !== null && b.high !== undefined
    && b.position !== null && b.position !== undefined)
}

// 越界一律红：越高越好、越低越好的指标方向不同，但"出正常区间"这件事同样值得看
function dotClass(m) {
  const s = (m.benchmark || {}).status
  return s === 'ok' ? 'ok' : 'bad'
}

function statusText(metric) {
  const status = (metric.benchmark || {}).status
  if (status === 'high') return '偏高'
  if (status === 'low') return '偏低'
  if (status === 'ok') return '正常'
  return ''
}

function bound(v) {
  if (v === null || v === undefined) return ''
  return Math.abs(v) < 1 ? `${(v * 100).toFixed(0)}%` : `${Math.round(v)}`
}

// 不画条的两种情况要分开说（M4-0）：直接人工率是**故意不给判断**的，
// 不是"没有可比区间" —— 否则老板会以为这个数不重要。
const NO_BAR_HINT = {
  labor_cost_rate: '口径参考，判断请看全口径人力成本率',
  breakeven_daily: '没有同行可比区间，只看你自己的趋势',
  stock_variance_rate: '盘点过才有这个数',
}

function noBarHint(m) {
  return NO_BAR_HINT[m.code] || '没有同行可比区间，只看数'
}

function money(v) {
  const n = Number(v || 0)
  if (!n) return '—'
  return `≈ ${Math.round(n).toLocaleString('zh-CN')} 元`
}

// ---- 趋势折线 ----
const seriesPoints = computed(() => {
  const pts = (props.series && props.series.points) || []
  return pts.filter((p) => p.daily_avg_revenue !== null || p.breakeven_daily !== null)
})

function pathFor(key) {
  const pts = seriesPoints.value
  if (pts.length < 2) return ''
  const vals = pts.flatMap((p) => [p[key]]).filter((v) => v !== null && v !== undefined)
  if (!vals.length) return ''
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  const span = max - min || 1
  const step = 320 / (pts.length - 1)
  // 上 10px / 下 10px 留白，避免线贴边
  return pts
    .map((p, i) => {
      const v = p[key]
      if (v === null || v === undefined) return null
      const y = 110 - ((v - min) / span) * 100
      return `${Math.round(i * step)},${Math.round(y)}`
    })
    .filter(Boolean)
    .join(' ')
}

const revenuePath = computed(() => pathFor('daily_avg_revenue'))
const breakevenPath = computed(() => pathFor('breakeven_daily'))

const anomalies = computed(() => (props.report && props.report.anomalies) || [])
</script>

<style scoped>
.card {
  padding: 16px;
}

.conclusion {
  /* ① 结论区尽量留在首屏：375px 视口下也不该被挤下去 */
  padding: 14px 16px 12px;
}

.period {
  font-size: 12px;
  color: var(--ink-2);
  margin-bottom: 8px;
}

.line {
  margin: 0 0 6px;
  font-size: 15px;
  line-height: 1.65;
}

.line-0 {
  font-weight: 600;
  color: var(--ink);
}

.line-1 {
  color: var(--ink);
}

.line-2 {
  color: var(--brand);
}

.line-2::before {
  content: '→ ';
}

.warns {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--line);
}

.warn {
  font-size: 12px;
  color: var(--warn);
  line-height: 1.6;
}

/* ---- ② 指标卡网格 ---- */
.grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
  margin: 12px 12px 0;
}

.mcard {
  background: #fff;
  border-radius: 12px;
  padding: 12px;
  position: relative;
}

.mcard-name {
  font-size: 12px;
  color: var(--ink-2);
}

.mcard-value {
  font-size: 24px;
  font-weight: 700;
  line-height: 1.25;
  margin: 2px 0 8px;
}

.bar-track {
  position: relative;
  height: 8px;
  background: var(--line);
  border-radius: 4px;
}

.bar-zone {
  position: absolute;
  left: 25%;
  width: 50%;
  top: 0;
  bottom: 0;
  background: var(--ok-soft);
  border-radius: 4px;
}

.bar-dot {
  position: absolute;
  top: 50%;
  width: 12px;
  height: 12px;
  margin-left: -6px;
  margin-top: -6px;
  border-radius: 50%;
  border: 2px solid #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}

.bar-dot.ok {
  background: var(--ok);
}

.bar-dot.bad {
  background: var(--danger);
}

.bar-scale {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--ink-2);
  margin-top: 3px;
}

.bar-none {
  font-size: 10px;
  color: var(--ink-2);
  padding: 4px 0;
}

.mcard-say {
  font-size: 12px;
  color: var(--ink-2);
  line-height: 1.5;
  margin-top: 6px;
}

.tag {
  display: inline-block;
  margin-top: 6px;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 8px;
  background: #f2f3f5;
  color: var(--ink-2);
}

.tag.high {
  background: var(--danger-soft);
  color: var(--danger);
}

.tag.low {
  background: var(--danger-soft);
  color: var(--danger);
}

.tag.ok {
  background: var(--ok-soft);
  color: var(--ok);
}

/* ---- ③ 趋势 ---- */
.trend {
  width: 100%;
  height: 120px;
  display: block;
}

.grid-line {
  stroke: var(--line);
  stroke-width: 1;
}

.line-rev {
  fill: none;
  stroke: var(--brand);
  stroke-width: 2;
  stroke-linejoin: round;
}

.line-be {
  fill: none;
  stroke: var(--warn);
  stroke-width: 2;
  stroke-dasharray: 5 4;
  stroke-linejoin: round;
}

.legend {
  display: flex;
  gap: 12px;
  align-items: center;
  font-size: 11px;
  color: var(--ink-2);
  margin-top: 6px;
}

.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 4px;
}

.dot.rev {
  background: var(--brand);
}

.dot.be {
  background: var(--warn);
}

.legend-note {
  margin-left: auto;
}

/* ---- ④ 异常清单 ---- */
.anomaly {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 10px 0;
  border-top: 1px solid var(--line);
}

.anomaly:first-of-type {
  border-top: none;
}

.rank {
  width: 18px;
  font-size: 12px;
  color: var(--ink-2);
  padding-top: 2px;
}

.flag {
  flex: 0 0 auto;
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 6px;
  margin-top: 2px;
}

.flag.high,
.flag.low {
  background: var(--danger-soft);
  color: var(--danger);
}

.flag.warn {
  background: var(--warn-soft);
  color: var(--warn);
}

.anomaly-body {
  flex: 1;
  min-width: 0;
}

.anomaly-head {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 13px;
}

.anomaly-amount {
  color: var(--danger);
  flex: 0 0 auto;
}

.anomaly-say {
  font-size: 12px;
  color: var(--ink-2);
  line-height: 1.5;
  margin-top: 2px;
}
</style>
