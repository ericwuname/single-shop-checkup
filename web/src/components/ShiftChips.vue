<template>
  <!-- 上班人员"药丸"（docs/08 §6.4 A）。
       预填：取最近一个有排班记录的日子（向前最多 14 天）的班次做候选，默认选中；
       当天已有保存的班次 → 用当天的，不预填。点击切换选中；aria-pressed 表达状态。
       "加一个人"弹 Vant Popup：姓名 + TimePicker（24 小时制）。
       跨夜（结束早于开始）按 +24 小时算，规则与后端一致（22:00→06:00 = 8 小时）。 -->
  <div class="shifts">
    <div class="head">
      <span class="title">今天谁上班</span>
      <span class="hours num">共 {{ totalHours }} 小时</span>
    </div>
    <p class="sub">{{ prefilled ? '已按最近一次排班预填，不对就点一下取消' : '点下面的按钮加人' }}</p>

    <div class="pills" role="group" aria-label="上班人员">
      <button
        v-for="(s, i) in modelValue"
        :key="i"
        type="button"
        class="pill"
        :class="{ on: s.on !== false }"
        :aria-pressed="s.on !== false ? 'true' : 'false'"
        @click="toggle(i)"
      >
        <span class="name">{{ s.worker_name || '（未填名字）' }}</span>
        <span class="time num" v-if="s.start_time && s.end_time">
          {{ s.start_time }}–{{ s.end_time }} · {{ shiftHours(s) }} 小时
        </span>
        <span class="time" v-else>时间没填</span>
      </button>

      <button type="button" class="pill add" @click="openAdd">＋ 加一个人</button>
    </div>

    <van-popup v-model:show="showAdd" position="bottom" round :style="{ maxHeight: '70%' }">
      <div class="adder">
        <div class="adder-title">加一个人</div>
        <label class="field">
          <span class="field-label">姓名</span>
          <input v-model="draft.worker_name" type="text" aria-label="姓名" placeholder="名字" />
        </label>
        <div class="field">
          <span class="field-label">上班时间</span>
          <button type="button" class="timebtn num" @click="pick = 'start'">
            {{ draft.start_time || '选时间' }}
          </button>
        </div>
        <div class="field">
          <span class="field-label">下班时间</span>
          <button type="button" class="timebtn num" @click="pick = 'end'">
            {{ draft.end_time || '选时间' }}
          </button>
        </div>
        <p v-if="sameTime" class="warn">上下班时间一样，请检查</p>
        <p v-else-if="draft.start_time && draft.end_time" class="preview num">
          共 {{ shiftHours(draft) }} 小时
        </p>
        <button
          type="button"
          class="btn-primary"
          :disabled="!draft.worker_name || !draft.start_time || !draft.end_time || sameTime"
          @click="confirmAdd"
        >
          加上
        </button>
        <button type="button" class="cancel" @click="showAdd = false">算了</button>

        <van-popup v-model:show="showPicker" position="bottom" round>
          <van-time-picker v-model="pickValue" title="选择时间" @confirm="onPick" @cancel="pick = ''" />
        </van-popup>
      </div>
    </van-popup>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { showToast } from 'vant'

const props = defineProps({
  // [{ worker_name, start_time, end_time, on }]  on=false = 老板点了取消
  modelValue: { type: Array, default: () => [] },
  prefilled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const showAdd = ref(false)
const showPicker = ref(false)
const pick = ref('') // 'start' | 'end'
const pickValue = ref(['09', '00'])
const draft = ref({ worker_name: '', start_time: '', end_time: '' })

const sameTime = computed(
  () =>
    draft.value.start_time !== '' &&
    draft.value.start_time === draft.value.end_time,
)

watch(pick, (v) => {
  if (!v) return
  const cur = draft.value[`${v}_time`] || ''
  const [h, m] = cur ? cur.split(':') : ['09', '00']
  pickValue.value = [h.padStart(2, '0'), (m || '00').padStart(2, '0')]
  showPicker.value = true
})

function onPick(val) {
  if (pick.value) {
    draft.value[`${pick.value}_time`] = `${val[0]}:${val[1]}`
  }
  showPicker.value = false
  pick.value = ''
}

function openAdd() {
  draft.value = { worker_name: '', start_time: '', end_time: '' }
  showAdd.value = true
}

function confirmAdd() {
  if (sameTime.value) return
  emit('update:modelValue', [...props.modelValue, { ...draft.value, on: true }])
  showAdd.value = false
}

function toggle(i) {
  const next = props.modelValue.map((s, idx) =>
    idx === i ? { ...s, on: s.on === false } : s,
  )
  emit('update:modelValue', next)
}

/** 与后端一致：结束早于开始 = 跨夜 +24h；开始=结束 = 0（会被拦） */
function shiftHours(s) {
  const [sh, sm] = String(s.start_time).split(':').map(Number)
  const [eh, em] = String(s.end_time).split(':').map(Number)
  if ([sh, sm, eh, em].some((n) => Number.isNaN(n))) return 0
  let mins = eh * 60 + em - (sh * 60 + sm)
  if (mins < 0) mins += 24 * 60
  return Math.round((mins / 60) * 100) / 100
}

const totalHours = computed(() =>
  Math.round(
    props.modelValue
      .filter((s) => s.on !== false)
      .reduce((sum, s) => sum + shiftHours(s), 0) * 100,
  ) / 100,
)
</script>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.title {
  font-size: 17px;
  font-weight: 700;
}
.hours {
  font-size: 14px;
  color: var(--accent-ink);
  font-weight: 700;
}
.sub {
  font-size: 13px;
  color: var(--muted);
  margin: 2px 0 12px;
}
.pills {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.pill {
  min-height: 44px;
  padding: 8px 14px;
  border-radius: 16px;
  border: 2px solid var(--pill-off);
  background: #fff;
  color: var(--muted);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
}
.pill.on {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent-ink);
}
.pill .name {
  font-size: 16px;
  font-weight: 700;
}
.pill .time {
  font-size: 13px;
  font-weight: 500;
}
.pill.add {
  border-style: dashed;
  justify-content: center;
  font-size: 15px;
  font-weight: 700;
}
.adder {
  padding: 20px 18px calc(20px + env(safe-area-inset-bottom));
}
.adder-title {
  font-size: 18px;
  font-weight: 700;
  margin-bottom: 14px;
  text-align: center;
}
.field {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 0;
  border-bottom: 1px solid var(--line-soft);
}
.field-label {
  font-size: 16px;
  font-weight: 700;
}
.field input,
.timebtn {
  border: 0;
  outline: none;
  text-align: right;
  font-size: 17px;
  background: transparent;
  min-height: 44px;
}
.warn {
  color: var(--danger-ink);
  font-size: 14px;
  margin: 10px 0 0;
}
.preview {
  color: var(--accent-ink);
  font-size: 14px;
  margin: 10px 0 0;
  text-align: right;
}
.adder .btn-primary {
  margin-top: 18px;
}
.cancel {
  width: 100%;
  height: 48px;
  margin-top: 10px;
  border: 0;
  background: transparent;
  color: var(--muted);
  font-size: 15px;
}
</style>
