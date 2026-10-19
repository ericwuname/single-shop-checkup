<template>
  <!-- 区间标尺（docs/08 §6.4 C）：轨道 + 常见区间浅绿带 + 当前值游标。
       区间数值来自后端 benchmark.low/high，**不写死 28/38**（FE-11）。 -->
  <div class="rangebar" role="img" :aria-label="ariaText">
    <div class="track">
      <div class="band" :style="bandStyle"></div>
      <div v-if="markerLeft !== null" class="pin" :style="{ left: markerLeft + '%' }"></div>
    </div>
    <div class="scale">
      <span>{{ fmt(low) }}</span>
      <span>{{ fmt(high) }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  low: { type: Number, default: null },
  high: { type: Number, default: null },
  value: { type: Number, default: null },
  unit: { type: String, default: '%' },
})

function pct(v) {
  if (props.low === null || props.high === null || props.high <= props.low) return null
  return Math.max(0, Math.min(100, ((v - props.low) / (props.high - props.low)) * 100))
}

const bandStyle = computed(() => ({
  left: '0%',
  right: '0%',
}))

const markerLeft = computed(() => (props.value === null ? null : pct(props.value)))

const ariaText = computed(() => {
  const lo = props.low === null ? '' : props.low
  const hi = props.high === null ? '' : props.high
  return `常见区间 ${lo} 到 ${hi}${props.unit}，当前 ${props.value ?? '--'}${props.unit}`
})

function fmt(v) {
  if (v === null || v === undefined) return '--'
  return `${Math.round(v * 100) / 100}${props.unit}`
}
</script>

<style scoped>
.rangebar {
  margin-top: 10px;
}
.track {
  position: relative;
  height: 10px;
  background: var(--line-soft);
  border-radius: 5px;
}
.band {
  position: absolute;
  top: 0;
  bottom: 0;
  background: var(--accent-soft);
  border-radius: 5px;
}
.pin {
  position: absolute;
  top: -4px;
  width: 4px;
  height: 18px;
  border-radius: 2px;
  background: var(--accent);
  transform: translateX(-2px);
}
.scale {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--muted);
  margin-top: 4px;
}
</style>
