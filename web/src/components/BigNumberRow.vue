<template>
  <!-- 一行大数输入（docs/08 §6.4 A）：标题 + 辅助句 + 右对齐大数 + 单位。
       输入规则：inputmode=decimal；只允许数字和一个小数点；不许负数；最多 9 位；
       留空 = 没填（保存时不写入），填 0 = 明确是 0 —— 两者必须区分。 -->
  <div class="bignum">
    <div class="head">
      <span class="title">{{ title }}</span>
      <span class="unit">{{ unit }}</span>
    </div>
    <p v-if="hint" class="hint">{{ hint }}</p>
    <input
      class="num"
      :value="modelValue"
      type="text"
      inputmode="decimal"
      :placeholder="placeholder"
      :aria-label="title"
      :maxlength="10"
      @input="onInput"
    />
  </div>
</template>

<script setup>
const props = defineProps({
  title: { type: String, required: true },
  hint: { type: String, default: '' },
  unit: { type: String, default: '元' },
  modelValue: { type: [String, Number], default: '' },
  placeholder: { type: String, default: '填数' },
})
const emit = defineEmits(['update:modelValue'])

function onInput(e) {
  let v = e.target.value
  // 只留数字和第一个小数点；去掉负号与其他字符
  v = v.replace(/[^\d.]/g, '')
  const first = v.indexOf('.')
  if (first !== -1) {
    v = v.slice(0, first + 1) + v.slice(first + 1).replace(/\./g, '')
  }
  // 最多 9 位整数
  const [int, dot, dec] = v.split(/(\.)/)
  if (int && int.length > 9) {
    v = int.slice(0, 9) + (dot || '') + (dec || '')
  }
  e.target.value = v
  emit('update:modelValue', v)
}
</script>

<style scoped>
.bignum {
  padding: 10px 0 14px;
  border-bottom: 1px solid var(--line-soft);
}
.bignum:last-child {
  border-bottom: 0;
}
.head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
.title {
  font-size: 17px;
  font-weight: 700;
}
.unit {
  font-size: 13px;
  color: var(--muted);
}
.hint {
  font-size: 13px;
  color: var(--muted);
  margin: 2px 0 0;
}
input {
  width: 100%;
  border: 0;
  outline: none;
  background: transparent;
  text-align: right;
  font-size: 32px;
  font-weight: 700;
  color: var(--ink);
  padding: 8px 0 0;
}
input::placeholder {
  color: var(--pill-off);
  font-weight: 500;
}
</style>
