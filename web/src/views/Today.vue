<template>
  <div>
    <!-- 离线待补传提示条（M6 离线队列 + M7 任务书 §6.4 A 离线项）：
         有暂存时出现，补传成功自动消失。不进入结果态。 -->
    <div v-if="pending > 0" class="pending" role="status">
      有 {{ pending }} 天的数存在手机里没传上去，连上网会自动补
    </div>

    <!-- ================= 录入态 ================= -->
    <template v-if="view === 'form'">
      <p class="page-hint">{{ shopName }} · 打烊后记三样，10 秒就好</p>

      <!-- 日期条：‹ ›  + 中间 M月D日（点开中文选择器，禁未来）。
           F3-01：禁用原生 <input type=date>（会显示成 10/18/2026）。 -->
      <div class="datebar">
        <button type="button" class="arrow" aria-label="前一天" @click="moveDate(-1)">‹</button>
        <button type="button" class="date-mid" @click="showDatePicker = true">
          <span class="d1">{{ dateLabel }}</span>
          <span class="d2">{{ dayWord }}</span>
        </button>
        <button
          type="button"
          class="arrow"
          aria-label="后一天"
          :disabled="date.value >= todayStr"
          @click="moveDate(1)"
        >
          ›
        </button>
      </div>

      <van-popup v-model:show="showDatePicker" position="bottom" round>
        <van-date-picker
          v-model="pickDate"
          title="选择日期"
          :min-date="minDate"
          :max-date="maxDate"
          :columns-type="['year', 'month', 'day']"
          @confirm="onDatePicked"
          @cancel="showDatePicker = false"
        />
      </van-popup>

      <div class="card">
        <BigNumberRow
          v-model="values.revenue"
          title="营业额"
          hint="今天一共收了多少"
          unit="元"
        />
        <BigNumberRow
          v-model="values.purchase"
          title="买菜进货"
          hint="菜、肉、调料、包装都算，按今天买的"
          unit="元"
        />
      </div>

      <div class="card">
        <ShiftChips v-model="shifts" :prefilled="prefilled" />
      </div>

      <p v-if="yesterdayLine" class="yesterday num">昨天记的：{{ yesterdayLine }}</p>

      <div class="save-zone">
        <button type="button" class="btn-primary" :disabled="saving" @click="save">
          {{ saving ? '正在存…' : '存好了，看今天的结果' }}
        </button>
      </div>
    </template>

    <!-- ================= 结果态（Reply.png）================= -->
    <template v-else-if="view === 'reply' && rep">
      <div class="reply-head">
        <span>{{ repDateLabel }} · {{ shopName }}</span>
        <span class="saved" role="status">✓ 已存好</span>
      </div>

      <div class="card">
        <div class="threenum">
          <div class="tn">
            <span class="tn-label">营业额</span>
            <span class="tn-value num">{{ fmtMetric('revenue') }}</span>
          </div>
          <div class="tn">
            <span class="tn-label">买菜</span>
            <span class="tn-value num">{{ fmtMetric('purchase') }}</span>
          </div>
          <div class="tn">
            <span class="tn-label">排班人工</span>
            <span class="tn-value num">{{ fmtMetric('labor_cost') }}</span>
          </div>
        </div>
        <div class="verdict-row" v-if="foodVerdict">
          <VerdictChip :status="foodVerdict.status" />
        </div>
        <p class="line2">{{ rep.conclusion.lines[1] }}</p>
      </div>

      <div class="card todo" v-if="rep.conclusion.lines[2]">
        <p class="todo-label">明天做一件事</p>
        <p class="todo-text">{{ rep.conclusion.lines[2] }}</p>
      </div>

      <div class="card">
        <div class="progress" aria-hidden="true">
          <span
            v-for="i in 7"
            :key="i"
            class="seg"
            :class="{ filled: i <= daysWithData }"
          ></span>
        </div>
        <p class="progress-text">
          已记 {{ daysWithData }} 天，攒够 7 天出一张周汇总。<template v-if="daysWithData < 7">
            数据不足 7 天，仅供参考。</template
          >
        </p>
      </div>

      <button type="button" class="btn-secondary" @click="view = 'form'">
        记错了？改一下今天的数
      </button>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { showToast } from 'vant'
import { api, AccessDenied, shiftDay, today, toISO } from '../api'
import { enqueueDaily, flushQueue, isNetworkError, pendingCount } from '../offline_queue'
import BigNumberRow from '../components/BigNumberRow.vue'
import ShiftChips from '../components/ShiftChips.vue'
import VerdictChip from '../components/VerdictChip.vue'

const props = defineProps({
  shop: { type: Object, default: null },
  refreshKey: { type: Number, default: 0 }, // 我的店改完设置后让今天页刷新
})

const view = ref('form') // 'form' | 'reply'
const date = ref(today())
const todayStr = today()
const values = ref({ revenue: '', purchase: '' })
const shifts = ref([])
const prefilled = ref(false)
const rep = ref(null)
const saving = ref(false)
const pending = ref(0)
const showDatePicker = ref(false)
const yesterdayLine = ref('')
const daysWithData = ref(0)

const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

const dateLabel = computed(() => {
  const [y, m, d] = date.value.split('-')
  return `${Number(m)}月${Number(d)}日`
})
const repDateLabel = computed(() => dateLabel.value)
const dayWord = computed(() => {
  if (date.value === todayStr) return '今天'
  if (date.value === shiftDay(todayStr, -1)) return '昨天'
  if (date.value === shiftDay(todayStr, -2)) return '前天'
  return WEEK[new Date(`${date.value}T00:00:00`).getDay()]
})

const minDate = new Date(new Date().getFullYear() - 2, 0, 1)
const maxDate = new Date()
const pickDate = ref(date.value.split('-').map((p) => String(Number(p))))

function onDatePicked(val) {
  date.value = toISO(new Date(Number(val[0]), Number(val[1]) - 1, Number(val[2])))
  showDatePicker.value = false
  loadDay()
}

function moveDate(delta) {
  const next = shiftDay(date.value, delta)
  if (next > todayStr) return // 不许选未来
  date.value = next
  loadDay()
}

function fmtMetric(code) {
  const m = (rep.value?.metrics || []).find((x) => x.code === code)
  return m && m.value !== null && m.value !== undefined
    ? Number(m.value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
    : '--'
}

const foodVerdict = computed(() => {
  const m = (rep.value?.metrics || []).find((x) => x.code === 'food_cost_rate')
  return m?.benchmark || null
})

async function loadDay() {
  if (!props.shop) return
  view.value = 'form'
  const rows = await api.listDaily(props.shop.id, date.value, date.value)
  const row = rows[0]
  if (row) {
    // 当天已有保存的数据 → 用当天的，不预填（docs/08 §6.4 A 预填规则）
    values.value = { revenue: row.values?.revenue ?? '', purchase: row.values?.purchase ?? '' }
    shifts.value = (row.shifts || []).map((s) => ({ ...s, on: true }))
    prefilled.value = false
  } else {
    values.value = { revenue: '', purchase: '' }
    shifts.value = await prefillShifts()
    prefilled.value = shifts.value.length > 0
  }
  yesterdayLine.value = await loadYesterday()
  rep.value = await api.report(props.shop.id, date.value, date.value)
  daysWithData.value = rep.value?.period?.days_with_data ?? 0
}

/** 预填：最近一个有排班记录的日子（向前最多 14 天）的班次，默认选中 */
async function prefillShifts() {
  try {
    const start = shiftDay(todayStr, -14)
    const rows = await api.listDaily(props.shop.id, start, shiftDay(date.value, -1))
    for (const row of [...rows].reverse()) {
      if (row.shifts && row.shifts.length) {
        return row.shifts.map((s) => ({ ...s, on: true }))
      }
    }
  } catch {
    // 拿不到历史就不预填 —— 不该因此挡住录入
  }
  return []
}

async function loadYesterday() {
  try {
    const y = shiftDay(date.value, -1)
    const rows = await api.listDaily(props.shop.id, y, y)
    const row = rows[0]
    if (!row || !row.values) return ''
    const r = row.values.revenue
    const p = row.values.purchase
    if ((r === null || r === undefined || r === '') && (p === null || p === undefined || p === '')) {
      return ''
    }
    const parts = []
    if (r !== null && r !== undefined && r !== '') parts.push(`营业额 ${Number(r).toLocaleString('zh-CN')} 元`)
    if (p !== null && p !== undefined && p !== '') parts.push(`买菜 ${Number(p).toLocaleString('zh-CN')} 元`)
    return parts.join('，')
  } catch {
    return ''
  }
}

async function save() {
  if (!props.shop) return
  const shiftsPayload = shifts.value
    .filter((s) => s.on !== false && s.worker_name && s.start_time && s.end_time)
    .map((s) => ({
      worker_name: s.worker_name,
      start_time: s.start_time,
      end_time: s.end_time,
    }))

  const cleanValues = {}
  for (const [k, v] of Object.entries(values.value)) {
    // 留空 = 没填（不写入）；填 0 = 明确是 0 —— 两者必须区分
    if (v !== null && v !== undefined && v !== '') cleanValues[k] = Number(v)
  }

  if (!Object.keys(cleanValues).length && !shiftsPayload.length) {
    showToast('先填一个数，或者点一下上班的人')
    return
  }

  const payload = {
    shop_id: props.shop.id,
    stat_date: date.value,
    values: cleanValues,
    shifts: shiftsPayload,
  }

  saving.value = true
  try {
    await api.putDaily(payload)
    showToast('记下了')
    flushQueue(api.putDaily).then((r) => {
      if (r.sent > 0) showToast(`补上了之前存的 ${r.sent} 天`)
      pending.value = pendingCount()
    })
    await loadDay()
    view.value = 'reply' // 存好 → 结果态（Reply.png）
  } catch (err) {
    if (isNetworkError(err)) {
      const stored = enqueueDaily(payload)
      pending.value = pendingCount()
      showToast(
        stored
          ? '现在没网，已存到手机里，连上网自动补传'
          : '没网，而且手机本地也存不下 —— 请先把今天的数拍个照',
      )
      // 不进入结果态
    } else {
      showToast(err.message)
    }
  } finally {
    saving.value = false
  }
}

/** 保存中途掉登录态（码被改）→ 通知外壳重新锁屏 */
defineExpose({ reload: loadDay })

onMounted(async () => {
  pending.value = pendingCount()
  if (props.shop) await loadDay()
})

watch(
  () => [props.shop?.id, props.refreshKey],
  () => {
    if (props.shop) loadDay()
  },
)
</script>

<style scoped>
.pending {
  background: var(--warn-bg);
  color: var(--warn-ink);
  border-radius: var(--radius-btn);
  padding: 10px 14px;
  font-size: 14px;
  margin-bottom: 10px;
}
.page-hint {
  text-align: center;
  font-size: 13px;
  color: var(--muted);
  margin: 0 0 10px;
}
.datebar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
}
.arrow {
  width: 44px;
  height: 44px;
  border-radius: 14px;
  border: 1px solid var(--line);
  background: var(--card);
  font-size: 22px;
  color: var(--ink);
}
.arrow:disabled {
  opacity: 0.35;
  cursor: default;
}
.date-mid {
  flex: 1;
  height: 44px;
  border: 0;
  background: transparent;
  display: flex;
  align-items: baseline;
  justify-content: center;
  gap: 8px;
}
.d1 {
  font-size: 26px;
  font-weight: 700;
}
.d2 {
  font-size: 13px;
  color: var(--muted);
}
.yesterday {
  text-align: center;
  font-size: 13px;
  color: var(--muted);
  margin: 10px 0 0;
}
.save-zone {
  position: fixed;
  left: 0;
  right: 0;
  bottom: calc(72px + env(safe-area-inset-bottom));
  padding: 8px 14px;
  background: linear-gradient(transparent, var(--bg) 40%);
  max-width: 480px;
  margin: 0 auto;
}
.reply-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 14px;
  color: var(--muted);
  margin: 4px 0 12px;
}
.saved {
  background: var(--accent-soft);
  color: var(--accent-ink);
  border-radius: 16px;
  padding: 4px 12px;
  font-size: 13px;
  font-weight: 700;
}
.threenum {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}
.tn {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tn-label {
  font-size: 13px;
  color: var(--muted);
}
.tn-value {
  font-size: 22px;
  font-weight: 700;
}
.verdict-row {
  margin: 6px 0;
}
.line2 {
  font-size: 16px;
  margin: 10px 0 0;
}
.todo {
  background: var(--accent-soft);
  border-color: transparent;
}
.todo-label {
  font-size: 13px;
  color: var(--accent-ink);
  margin: 0 0 4px;
  font-weight: 700;
}
.todo-text {
  font-size: 18px;
  font-weight: 700;
  margin: 0;
  color: var(--accent-ink);
}
.progress {
  display: flex;
  gap: 6px;
}
.seg {
  flex: 1;
  height: 8px;
  border-radius: 4px;
  background: var(--line-soft);
}
.seg.filled {
  background: var(--accent);
}
.progress-text {
  font-size: 13px;
  color: var(--muted);
  margin: 10px 0 0;
}
.btn-secondary {
  width: 100%;
  height: 48px;
  border: 1px solid var(--line);
  border-radius: var(--radius-btn);
  background: var(--card);
  color: var(--ink);
  font-size: 15px;
}
</style>
