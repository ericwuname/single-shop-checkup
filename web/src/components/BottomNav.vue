<template>
  <!-- 底部导航（docs/08 §6.4 E）：今天（铅笔）/ 本周（柱状）/ 我的店（店铺）。
       高 72px 含安全区；选中 = 主色 + 700；aria-current 表达当前页。 -->
  <nav class="nav" aria-label="页面导航">
    <button
      v-for="t in tabs"
      :key="t.name"
      type="button"
      class="item"
      :class="{ on: modelValue === t.name }"
      :aria-current="modelValue === t.name ? 'page' : undefined"
      @click="$emit('update:modelValue', t.name)"
    >
      <span class="icon" aria-hidden="true" v-html="t.svg"></span>
      <span class="label">{{ t.label }}</span>
    </button>
  </nav>
</template>

<script setup>
defineProps({
  modelValue: { type: String, default: 'today' },
})
defineEmits(['update:modelValue'])

// 图标取设计稿的线条风格（内联 SVG，24×24，stroke 1.8）
const tabs = [
  {
    name: 'today',
    label: '今天',
    svg: '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
  },
  {
    name: 'week',
    label: '本周',
    svg: '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 20V12"/><path d="M12 20V5"/><path d="M19 20v-6"/></svg>',
  },
  {
    name: 'shop',
    label: '我的店',
    svg: '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16l-1.3 12.2a2 2 0 0 1-2 1.8H7.3a2 2 0 0 1-2-1.8L4 7Z"/><path d="M9 10V6a3 3 0 0 1 6 0v4"/></svg>',
  },
]
</script>

<style scoped>
.nav {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  height: calc(72px + env(safe-area-inset-bottom));
  padding-bottom: env(safe-area-inset-bottom);
  background: var(--card);
  border-top: 1px solid var(--line);
  z-index: 10;
}
.item {
  flex: 1;
  border: 0;
  background: transparent;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  color: var(--muted);
  font-weight: 500;
}
.item.on {
  color: var(--accent);
  font-weight: 700;
}
.label {
  font-size: 12px;
}
</style>
