<template>
  <!-- 6 个原子数字里的"日口径 3 个"。字段列表来自 /api/meta/fields，
       前端不写死字段名 —— 后端插一行 field_def 就会自动多一个输入框。 -->
  <div class="block">
    <van-cell-group :border="false">
      <van-field
        v-for="f in fields"
        :key="f.code"
        :model-value="local[f.code] ?? ''"
        :label="f.label"
        :type="inputType(f)"
        :placeholder="placeholder(f)"
        input-align="right"
        clearable
        @update:model-value="(v) => update(f.code, v)"
      >
        <template #extra>
          <span class="unit">{{ f.unit }}</span>
        </template>
      </van-field>
    </van-cell-group>
    <div v-if="!fields.length" class="empty">字段注册表是空的，先跑一次种子数据</div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  fields: { type: Array, default: () => [] },
  modelValue: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:modelValue'])

// 内部保存"用户输入的原样字符串"，提交时才转数字。
// 这样输入到一半（比如刚敲了 "1."）不会因为 Number() 得到 NaN 就把格子清空。
const local = ref(fromProps(props.modelValue))

function fromProps(obj) {
  const out = {}
  for (const [k, v] of Object.entries(obj || {})) {
    out[k] = v === null || v === undefined ? '' : String(v)
  }
  return out
}

// 只在"外部真的换了数据"时同步（比如切换日期换了一天），
// 避免自己 emit 出去的值绕一圈回来把正在输入的内容顶掉。
watch(
  () => props.modelValue,
  (next) => {
    const incoming = fromProps(next)
    if (JSON.stringify(incoming) === JSON.stringify(local.value)) return
    local.value = incoming
  },
)

function payload() {
  const out = {}
  for (const f of props.fields) {
    const raw = local.value[f.code]
    if (raw === '' || raw === null || raw === undefined) {
      out[f.code] = null
      continue
    }
    const num = Number(raw)
    out[f.code] = Number.isNaN(num) ? null : num
  }
  return out
}

function inputType(field) {
  if (field.data_type === 'integer') return 'digit'
  if (field.data_type === 'number') return 'number'
  return 'text'
}

function placeholder(field) {
  return field.help_text || `填${field.label}`
}

function update(code, raw) {
  // 关键：基线是自己的内部状态，不是 props。
  // 用父组件的 props 做基线时，同一轮事件里连填多个字段会因为 props 还没回流，
  // 后一次覆盖前一次 —— 实测只有最后一个格子提交上去（真浏览器测出来的 bug）。
  local.value = { ...local.value, [code]: raw === null || raw === undefined ? '' : String(raw) }
  emit('update:modelValue', payload())
}

defineExpose({ reset: (obj) => (local.value = fromProps(obj)) })
</script>

<style scoped>
.unit {
  color: #969799;
  font-size: 13px;
  margin-left: 6px;
}
</style>
