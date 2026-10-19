<template>
  <div>
    <!-- 空状态：有数天数 = 0（docs/08 §6.4 C 最后一行） -->
    <div v-if="loaded && hasNoData" class="card empty">
      <p>这一周还没有记录。</p>
      <button type="button" class="btn-primary" @click="$emit('go-today')">
        回到「今天」记下第一笔
      </button>
    </div>

    <template v-else-if="rep">
      <div class="head">
        <h1 class="h1">本周</h1>
        <p class="range num">
          {{ m(rep.period.start) }} – {{ m(rep.period.end) }} · 已记
          {{ rep.period.days_with_data }} 天
        </p>
      </div>

      <!-- 营业额卡：大数 = 区间合计；平均 = 合计 ÷ 有数天数（不是 ÷7） -->
      <div class="card">
        <p class="card-title">营业额</p>
        <p class="big num">{{ fmt(revenueTotal) }}</p>
        <p class="sub num">
          元，平均每天 {{ fmt(avgRevenue) }}
        </p>
        <WeekBars :days="bars" />
        <p v-if="bestWorst" class="best num">{{ bestWorst }}</p>
      </div>

      <!-- 买菜占比卡：区间带与游标取自后端 benchmark（不写死 28/38） -->
      <div class="card" v-if="foodMetric">
        <div class="row-head">
          <p class="card-title">买菜占营业额</p>
          <VerdictChip :status="foodMetric.benchmark?.status || ''" />
        </div>
        <p class="big num">{{ pct(foodMetric.value) }}</p>
        <RangeBar
          :low="foodMetric.benchmark?.low ?? null"
          :high="foodMetric.benchmark?.high ?? null"
          :value="foodMetric.value"
          unit="%"
        />
        <p class="sub" style="margin-top: 8px">{{ foodMetric.benchmark?.text }}</p>
      </div>

      <!-- 人工卡 -->
      <div class="card">
        <div class="row-head">
          <p class="card-title">排班工钱</p>
          <VerdictChip v-if="laborVerdict" :status="laborVerdict.status" />
        </div>
        <template v-if="laborValue !== null">
          <p class="big num">
            {{ fmt(laborValue) }} 元<span v-if="laborRate" class="small">，占 {{ laborRate }}</span>
          </p>
        </template>
        <template v-else>
          <p class="sub">还没排班，这个数先出不来。</p>
        </template>
        <template v-if="!laborVerdict && laborValue !== null">
          <p class="sub">还没填员工月薪，这个数先不评高低。</p>
          <button type="button" class="link" @click="$emit('go-shop')">
            去补员工月薪 ›
          </button>
        </template>
      </div>

      <!-- 保本卡（深色）：无值则整卡不显示 -->
      <div class="card dark" v-if="breakeven !== null">
        <p class="dark-sub">按你填的房租、水电和人工算</p>
        <p class="dark-big num">每天至少要做 {{ fmt(breakeven) }} 元才不亏</p>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api, toISO } from '../api'
import VerdictChip from '../components/VerdictChip.vue'
import RangeBar from '../components/RangeBar.vue'
import WeekBars from '../components/WeekBars.vue'

const props = defineProps({
  shop: { type: Object, default: null },
})
defineEmits(['go-today', 'go-shop'])

const rep = ref(null)
const dailyRows = ref([])
const loaded = ref(false)

const WEEK = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

function metric(code) {
  return (rep.value?.metrics || []).find((x) => x.code === code) || null
}

const revenueTotal = computed(() => metric('revenue')?.value ?? null)
const foodMetric = computed(() => metric('food_cost_rate'))
const laborValue = computed(() => metric('labor_cost')?.value ?? null)
const laborVerdict = computed(() => metric('labor_cost_rate')?.benchmark || null)
const breakeven = computed(() => metric('breakeven_daily')?.value ?? null)
const laborRate = computed(() => {
  const m = metric('labor_cost_rate')
  if (!m || m.value === null || m.value === undefined) return ''
  return `${Math.round(m.value * 1000) / 10}%`
})

const daysWithData = computed(() => rep.value?.period?.days_with_data ?? 0)
const hasNoData = computed(() => daysWithData.value === 0)

const avgRevenue = computed(() => {
  if (revenueTotal.value === null || !daysWithData.value) return null
  return Math.round((revenueTotal.value / daysWithData.value) * 100) / 100
})

/** 7 根柱：营业额按天，来自现有 listDaily（weekly 接口没有逐日明细，不改后端） */
const bars = computed(() => {
  if (!rep.value) return []
  const start = rep.value.period.start
  const end = rep.value.period.end
  const byDate = new Map(dailyRows.value.map((r) => [r.stat_date, r]))
  const out = []
  const cur = new Date(`${start}T00:00:00`)
  const endD = new Date(`${end}T00:00:00`)
  // ⚠️ 日期一律走本地时区（api.js 的 toISO）。toISOString() 是 UTC，
    // 在 UTC+8 会把 0–8 点算成昨天 —— M7 自查抓到的阻断 bug（ bars 全空槽）。
    const todayIso = toISO(new Date())
  while (cur <= endD) {
    const iso = toISO(cur)
    const row = byDate.get(iso)
    const v = row?.values?.revenue
    out.push({
      // getDay(): 0=周日 → 转成周一开始
      label: WEEK[(cur.getDay() + 6) % 7],
      value: v === null || v === undefined || v === '' ? null : Number(v),
      isToday: iso === todayIso,
    })
    cur.setDate(cur.getDate() + 1)
  }
  return out
})

const bestWorst = computed(() => {
  const withData = bars.value.filter((d) => d.value !== null)
  if (withData.length < 2) return ''
  const best = withData.reduce((a, b) => (b.value > a.value ? b : a))
  const worst = withData.reduce((a, b) => (b.value < a.value ? b : a))
  if (best.label === worst.label) return ''
  return `${best.label}最好，卖了 ${fmt(best.value)}；${worst.label}最淡，${fmt(worst.value)}。`
})

function fmt(v) {
  if (v === null || v === undefined) return '--'
  return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

function pct(v) {
  if (v === null || v === undefined) return '--'
  return `${Math.round(v * 1000) / 10}%`
}

function m(iso) {
  if (!iso) return ''
  const [, mm, dd] = iso.split('-')
  return `${Number(mm)}月${Number(dd)}日`
}

async function load() {
  if (!props.shop) return
  loaded.value = false
  const anchor = toISO(new Date())
  rep.value = await api.weekly(props.shop.id, anchor, 7)
  if (rep.value) {
    dailyRows.value = await api.listDaily(props.shop.id, rep.value.period.start, rep.value.period.end)
  }
  loaded.value = true
}

onMounted(load)
watch(() => props.shop?.id, load)
defineExpose({ reload: load })
</script>

<style scoped>
.head {
  margin: 4px 0 12px;
}
.h1 {
  font-size: 26px;
  font-weight: 700;
  margin: 0;
}
.range {
  font-size: 13px;
  color: var(--muted);
  margin: 2px 0 0;
}
.big {
  font-size: 34px;
  font-weight: 700;
  margin: 4px 0;
}
.big .small {
  font-size: 15px;
  font-weight: 500;
  color: var(--muted);
}
.sub {
  font-size: 13px;
  color: var(--muted);
  margin: 0 0 8px;
}
.best {
  font-size: 13px;
  color: var(--muted);
  margin: 10px 0 0;
}
.row-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.row-head .card-title {
  margin: 0;
}
.dark {
  background: var(--ink);
  border-color: transparent;
}
.dark-sub {
  font-size: 13px;
  color: #b8c4be;
  margin: 0 0 4px;
}
.dark-big {
  font-size: 19px;
  font-weight: 700;
  color: #fff;
  margin: 0;
}
.link {
  border: 0;
  background: transparent;
  color: var(--accent);
  font-size: 14px;
  font-weight: 700;
  padding: 0;
  min-height: 44px;
}
.empty {
  text-align: center;
  padding: 32px 16px;
}
.empty p {
  margin: 0 0 16px;
  color: var(--muted);
}
</style>
