<template>
  <!-- 本周 7 根柱（docs/08 §6.4 C）：没数据的天画浅灰虚线空槽；
       最高柱用主色，其余次绿；柱高 = 当日 ÷ 最高 × 96px。 -->
  <div class="bars">
    <div v-for="(d, i) in days" :key="i" class="col">
      <div class="slot">
        <div
          v-if="d.value !== null"
          class="bar"
          :class="{ top: d.value === maxValue && maxValue > 0 }"
          :style="{ height: barHeight(d.value) + 'px' }"
          :aria-label="`${d.label} ${d.value} 元`"
        ></div>
        <div v-else class="empty" aria-hidden="true"></div>
      </div>
      <span class="label" :class="{ today: d.isToday }">{{ d.label }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  // [{ label: '周一', value: 4200|null, isToday: bool }]
  days: { type: Array, required: true },
})

const maxValue = computed(() =>
  Math.max(0, ...props.days.map((d) => d.value ?? 0)),
)

function barHeight(v) {
  if (!maxValue.value) return 0
  return Math.max(6, Math.round((v / maxValue.value) * 96))
}
</script>

<style scoped>
.bars {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  margin-top: 8px;
}
.col {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}
.slot {
  height: 96px;
  width: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}
.bar {
  width: 60%;
  max-width: 36px;
  background: var(--accent-mid);
  border-radius: 6px 6px 2px 2px;
}
.bar.top {
  background: var(--accent);
}
.empty {
  width: 60%;
  max-width: 36px;
  height: 6px;
  border: 1px dashed var(--pill-off);
  border-radius: 4px;
}
.label {
  font-size: 12px;
  color: var(--muted);
}
.label.today {
  color: var(--accent-ink);
  font-weight: 700;
}
</style>
