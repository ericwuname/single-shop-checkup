<template>
  <!-- 访问码门（M7 WP-1）：只有公网模式后端返回 401 时才出现（docs/08 §4.3）。
       locked 时主界面整体不渲染 —— 不给"半加载"的假象。 -->
  <AccessGate v-if="locked" @unlocked="retryBootstrap" />
  <template v-else>
  <van-nav-bar fixed placeholder :title="title" />

  <div class="page">
    <!-- ---------------- 首次使用：建店 ---------------- -->
    <template v-if="!shop">
      <div class="block card">
        <div class="block-title" style="margin-left: 0">先告诉我这是哪家店</div>
        <van-field v-model="shopName" label="店名" placeholder="喜洋洋" input-align="right" />
        <van-field
          v-model="hourlyRate"
          label="每小时人工"
          type="number"
          input-align="right"
        >
          <template #extra><span class="unit">元/小时</span></template>
        </van-field>
        <div class="actions">
          <van-button block type="primary" :loading="saving" @click="createShop">开始</van-button>
        </div>
        <div class="hint" style="padding: 0 0 12px">
          人工按你填的时薪算。不知道就先用 25，后面随时能改。
        </div>
      </div>
    </template>

    <template v-else>
      <!-- ---------------- 录入 ---------------- -->
      <template v-if="tab === 'input'">
        <div class="datebar block">
          <van-button size="small" plain @click="moveDate(-1)">‹ 前一天</van-button>
          <input v-model="date" type="date" class="dateinput" @change="loadDay" />
          <van-button size="small" plain @click="moveDate(1)">后一天 ›</van-button>
        </div>

        <div class="block-title">今天这三个数</div>
        <DailyInput v-model="values" :fields="dayFields" />

        <div class="block-title">今天谁上班，上了多久</div>
        <ShiftTable v-model="shifts" />

        <div class="actions">
          <van-button block type="primary" :loading="saving" @click="save">保存</van-button>
        </div>

        <div class="block-title">今天算下来</div>
        <ResultCard :report="dayReport" />
      </template>

      <!-- ---------------- 报告 ---------------- -->
      <template v-else-if="tab === 'report'">
        <div class="block-title">最近 7 天</div>
        <ResultCard :report="weeklyReport" :series="weeklySeries" />

        <div v-if="weeklyReport && weeklyReport.observations.daily.length" class="block">
          <van-cell-group :border="false" title="每天的数">
            <van-cell
              v-for="d in weeklyReport.observations.daily"
              :key="d.date"
              :title="d.label"
              :value="`${fmt(d.revenue)} 元 / ${fmt(d.purchase)} 元 / ${fmt(d.orders)} 单`"
            />
          </van-cell-group>
        </div>

        <div v-if="weeklyReport" class="block card">
          <div class="kpi-line">
            工作日日均 <b>{{ fmt(weeklyReport.observations.weekday_avg_revenue) }}</b> 元 ·
            周末日均 <b>{{ fmt(weeklyReport.observations.weekend_avg_revenue) }}</b> 元
          </div>
        </div>
        <div class="actions">
          <van-button block plain type="primary" :loading="loading" @click="loadWeekly">
            重新算一遍
          </van-button>
        </div>
      </template>

      <!-- ---------------- 设置 ---------------- -->
      <template v-else>
        <div class="block-title">门店</div>
        <div class="block">
          <van-cell-group :border="false">
            <van-field v-model="shopDraft.name" label="店名" input-align="right" />
            <van-field
              v-model="shopDraft.default_hourly_rate"
              label="每小时人工"
              type="number"
              input-align="right"
            >
              <template #extra><span class="unit">元/小时</span></template>
            </van-field>
          </van-cell-group>
        </div>

        <div class="block-title">这个月的固定开销（{{ month }}）</div>
        <div class="block">
          <van-cell-group :border="false">
            <van-field
              v-for="f in monthFields"
              :key="f.code"
              :model-value="monthDraft[f.code] ?? ''"
              :label="f.label"
              type="number"
              :placeholder="f.help_text || ''"
              input-align="right"
              @update:model-value="(v) => (monthDraft[f.code] = v === '' ? null : Number(v))"
            >
              <template #extra><span class="unit">{{ f.unit }}</span></template>
            </van-field>
          </van-cell-group>
        </div>
        <div class="hint" style="margin: 8px 16px">
          毛利率填比例，比如 0.6 表示卖 100 块钱刨掉食材还剩 60。
        </div>

        <div class="actions">
          <van-button block type="primary" :loading="saving" @click="saveSettings">保存</van-button>
        </div>

        <div class="block card">
          <div class="kpi-line">指标注册表里有 {{ metricMeta.length }} 个指标</div>
          <div class="hint">
            加指标不用改这个 App：后端往 metric_def 插一行，这里刷新就有。
          </div>
        </div>
      </template>
    </template>
  </div>

  <van-tabbar v-if="shop" v-model="tab" fixed>
    <van-tabbar-item name="input" icon="edit">录入</van-tabbar-item>
    <van-tabbar-item name="report" icon="bar-chart-o">报告</van-tabbar-item>
    <van-tabbar-item name="settings" icon="setting-o">设置</van-tabbar-item>
  </van-tabbar>
  </template>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { showToast } from 'vant'
import { api, AccessDenied, monthOf, shiftDay, today } from './api'
import AccessGate from './components/AccessGate.vue'
import DailyInput from './components/DailyInput.vue'
import { enqueueDaily, flushQueue, isNetworkError, pendingCount } from './offline_queue'
import ShiftTable from './components/ShiftTable.vue'
import ResultCard from './components/ResultCard.vue'

const tab = ref('input')
const locked = ref(false) // 公网模式下访问码未通过（M7 WP-1）
const loading = ref(false)
const saving = ref(false)

const shop = ref(null)
const shopName = ref('我的店')
const hourlyRate = ref('25')
const shopDraft = ref({ name: '', default_hourly_rate: 25 })

const dayFields = ref([])
const monthFields = ref([])
const metricMeta = ref([])

const date = ref(today())
const values = ref({})
const shifts = ref([])
const dayReport = ref(null)
const weeklySeries = ref(null)

const weeklyReport = ref(null)
const monthDraft = ref({})

const month = computed(() => monthOf(date.value))
const title = computed(() => (shop.value ? shop.value.name : '单店体检'))

function fmt(v) {
  if (v === null || v === undefined) return '--'
  return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

async function bootstrap() {
  const [fields, monthFieldsRes, metrics, shops] = await Promise.all([
    api.metaFields('day'),
    api.metaFields('month'),
    api.metaMetrics(),
    api.listShops(),
  ])
  dayFields.value = fields
  monthFields.value = monthFieldsRes
  metricMeta.value = metrics

  if (shops.length) {
    shop.value = shops[0]
    shopDraft.value = { name: shops[0].name, default_hourly_rate: shops[0].default_hourly_rate }
    await Promise.all([loadDay(), loadWeekly(), loadMonthly()])
  }
}

async function createShop() {
  saving.value = true
  try {
    shop.value = await api.createShop({
      name: shopName.value || '我的店',
      default_hourly_rate: Number(hourlyRate.value) || 25,
    })
    shopDraft.value = { name: shop.value.name, default_hourly_rate: shop.value.default_hourly_rate }
    await Promise.all([loadDay(), loadWeekly(), loadMonthly()])
    showToast('建好了')
  } catch (err) {
    showToast(err.message)
  } finally {
    saving.value = false
  }
}

async function loadDay() {
  if (!shop.value) return
  const rows = await api.listDaily(shop.value.id, date.value, date.value)
  const row = rows[0]
  values.value = row ? { ...row.values } : {}
  shifts.value = row
    ? row.shifts.map((s) => ({
        worker_name: s.worker_name,
        start_time: s.start_time,
        end_time: s.end_time,
      }))
    : []
  dayReport.value = await api.report(shop.value.id, date.value, date.value)
}

async function loadWeekly() {
  if (!shop.value) return
  loading.value = true
  try {
    weeklyReport.value = await api.weekly(shop.value.id, today(), 7)
    // M4-9 第③段：近 12 个月趋势。单独 try —— 趋势取不到不该让整个报告页白屏
    try {
      weeklySeries.value = await api.monthlySeries(shop.value.id, 12, today())
    } catch {
      weeklySeries.value = null
    }
  } finally {
    loading.value = false
  }
}

async function loadMonthly() {
  if (!shop.value) return
  const data = await api.getMonthly(shop.value.id, month.value)
  monthDraft.value = { ...data.values }
}

async function save() {
  if (!shop.value) return

  // 只提交填完的排班（刚点"加一个人"还没填名字的行不发出去）
  const shiftsPayload = shifts.value
    .filter((s) => s.worker_name && s.start_time && s.end_time)
    .map((s) => ({
      worker_name: s.worker_name,
      start_time: s.start_time,
      end_time: s.end_time,
    }))

  const hasValue = Object.values(values.value).some(
    (v) => v !== null && v !== undefined && v !== '',
  )
  if (!hasValue && !shiftsPayload.length) {
    showToast('先填一个数，或者加一条排班')
    return
  }

  const payload = {
    shop_id: shop.value.id,
    stat_date: date.value,
    values: values.value,
    shifts: shiftsPayload,
  }

  saving.value = true
  try {
    await api.putDaily(payload)
    showToast('记下了')
    // 保存成功也顺手补一次队列（可能之前攒了没传出去的）
    flushQueue(api.putDaily).then((r) => {
      if (r.sent > 0) showToast(`补上了之前存的 ${r.sent} 天`)
    })
    await loadDay()
    await loadWeekly()
  } catch (err) {
    // M6-4 §5.4：**网络不通** → 存本地，明确告诉老板"已存本地，连上网自动补"。
    // 明确失败比"以为存了其实没存"好得多 —— 后者第二天看到空页面会怀疑产品。
    if (isNetworkError(err)) {
      const stored = enqueueDaily(payload)
      showToast(
        stored
          ? '现在没网，已存到手机里，连上网自动补传'
          : '没网，而且手机本地也存不下 —— 请先把今天的数拍个照',
      )
    } else {
      // 业务错误（比如没填原因代码）→ 原样报，不暂存
      showToast(err.message)
    }
  } finally {
    saving.value = false
  }
}

async function saveSettings() {
  if (!shop.value) return
  saving.value = true
  try {
    await api.patchShop(shop.value.id, {
      name: shopDraft.value.name,
      default_hourly_rate: Number(shopDraft.value.default_hourly_rate) || 25,
    })
    await api.putMonthly(shop.value.id, month.value, monthDraft.value)
    const shops = await api.listShops()
    shop.value = shops[0]
    showToast('改好了')
    await loadDay()
  } catch (err) {
    showToast(err.message)
  } finally {
    saving.value = false
  }
}

function moveDate(delta) {
  date.value = shiftDay(date.value, delta)
  loadDay()
}

watch(tab, (next) => {
  if (next === 'report') loadWeekly()
  if (next === 'settings') loadMonthly()
})

async function retryBootstrap() {
  // 访问码已由 AccessGate 写入 api 层，重新拉一遍元数据即可。
  // 再拿到 AccessDenied 说明码还是错的（AccessGate 已清码），留在锁屏页。
  try {
    await bootstrap()
    locked.value = false
  } catch (err) {
    if (err instanceof AccessDenied) return
    showToast(`连不上后端：${err.message}`)
  }
}

onMounted(async () => {
  try {
    await bootstrap()
  } catch (err) {
    if (err instanceof AccessDenied) {
      locked.value = true
      return
    }
    showToast(`连不上后端：${err.message}`)
  }

  // M6-4 §5.4：启动时补一次队列（老板昨晚在后厨存的数，今早打开就连上了）
  const before = pendingCount()
  if (before > 0) {
    const r = await flushQueue(api.putDaily)
    if (r.sent > 0) {
      showToast(`补上了之前存的 ${r.sent} 天`)
      await loadWeekly()
    } else if (r.failed > 0) {
      showToast(`还有 ${r.failed} 天存在手机里没传上去，连上网再试`)
    }
  }
})
</script>

<style scoped>
.card {
  padding: 16px;
}

.datebar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  margin: 12px 12px 0;
}

.dateinput {
  flex: 1;
  border: none;
  background: transparent;
  text-align: center;
  font-size: 15px;
  font-weight: 600;
  color: var(--ink);
  outline: none;
}

.unit {
  color: #969799;
  font-size: 13px;
  margin-left: 6px;
}

.kpi-line {
  font-size: 14px;
  line-height: 1.7;
}

.kpi-line b {
  font-variant-numeric: tabular-nums;
}
</style>
