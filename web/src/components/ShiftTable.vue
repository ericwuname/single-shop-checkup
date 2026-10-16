<template>
  <!-- 今天谁上班。老板微信里发的是"小李 18-22"，这里一一对应。
       跨夜班（22:00 → 06:00）后端按 +24h 记 8 小时，前端不用特殊处理。 -->
  <div class="block">
    <div v-for="(row, idx) in rows" :key="idx" class="shift-row">
      <van-field
        v-model="row.worker_name"
        label="谁"
        placeholder="小李"
        input-align="right"
        @update:model-value="emitRows"
      />
      <van-field
        v-model="row.start_time"
        label="从"
        type="time"
        input-align="right"
        @update:model-value="emitRows"
      />
      <van-field
        v-model="row.end_time"
        label="到"
        type="time"
        input-align="right"
        @update:model-value="emitRows"
      />
      <div class="row-actions">
        <span class="hours big-num">{{ rowHours(row) }}</span>
        <van-button size="mini" plain type="danger" @click="remove(idx)">删</van-button>
      </div>
    </div>

    <div v-if="!rows.length" class="empty">今天没人上班？点下面的按钮加一条</div>

    <div class="add">
      <van-button size="small" plain type="primary" icon="plus" @click="add">加一个人</van-button>
      <span class="hint">深夜班照填，比如 22:00 到 06:00</span>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue'])

const rows = ref(props.modelValue.map((r) => ({ ...r })))

// 只在"外部真的换了数据"时同步。
// 注意：不能把还没填完的行过滤掉再 emit —— 那样 add() 刚推入的空行会被
// 回流的 props 立刻清掉，用户点了"加一个人"却什么也没出现（实测 bug）。
watch(
  () => props.modelValue,
  (next) => {
    if (JSON.stringify(next) === JSON.stringify(rows.value)) return
    rows.value = next.map((r) => ({ ...r }))
  },
)

function emitRows() {
  emit(
    'update:modelValue',
    rows.value.map((r) => ({ ...r })),
  )
}

function add() {
  rows.value.push({ worker_name: '', start_time: '18:00', end_time: '22:00' })
  emitRows()
}

function remove(idx) {
  rows.value.splice(idx, 1)
  emitRows()
}

function minutes(hhmm) {
  const [h, m] = String(hhmm).split(':').map(Number)
  if (Number.isNaN(h) || Number.isNaN(m)) return null
  return h * 60 + m
}

function rowHours(row) {
  const a = minutes(row.start_time)
  const b = minutes(row.end_time)
  if (a === null || b === null) return '--'
  let diff = b - a
  if (diff < 0) diff += 24 * 60
  const h = diff / 60
  return h > 0 ? `${Number(h.toFixed(2))} 小时` : '0 小时'
}
</script>

<style scoped>
.shift-row {
  border-bottom: 1px solid var(--line);
  padding-bottom: 4px;
}

.shift-row:last-of-type {
  border-bottom: none;
}

.row-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding: 4px 16px 8px;
}

.hours {
  font-size: 13px;
  color: var(--brand);
  font-weight: 600;
}

.add {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
}
</style>
