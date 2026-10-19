<template>
  <!-- 判断胶囊（docs/08 §6.2）：状态不能只靠颜色 —— 偏高 ↑ / 正常 ✓ / 偏低 ↓，
       图标 + 文字 + 颜色三路同时表达。无 status 就不渲染（不制造噪音）。 -->
  <span v-if="status" class="chip" :class="cls" role="status">
    <span aria-hidden="true">{{ icon }}</span>
    {{ label }}
  </span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  status: { type: String, default: '' }, // high / ok / low / ''
})

const cls = computed(() =>
  props.status === 'high' ? 'high' : props.status === 'low' ? 'low' : 'ok',
)
const icon = computed(() =>
  props.status === 'high' ? '↑' : props.status === 'low' ? '↓' : '✓',
)
const label = computed(() =>
  props.status === 'high' ? '偏高' : props.status === 'low' ? '偏低' : '正常',
)
</script>

<style scoped>
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 28px;
  padding: 0 12px;
  border-radius: 16px;
  font-size: 14px;
  font-weight: 700;
  white-space: nowrap;
}
.high {
  background: var(--warn-bg);
  color: var(--warn-ink);
}
.ok {
  background: var(--accent-soft);
  color: var(--accent-ink);
}
.low {
  background: var(--low-bg);
  color: var(--low-ink);
}
</style>
