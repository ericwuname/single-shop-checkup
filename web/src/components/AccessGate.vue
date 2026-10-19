<template>
  <!-- 访问码门（M7 WP-1 §4.3 的最小版，WP-3 会按设计令牌美化）。
       只有公网模式下才会出现：非公网模式后端不发 401，bootstrap 直接成功，
       这个组件根本不会被挂载。 -->
  <div class="gate">
    <div class="card">
      <h1>单店体检</h1>
      <p class="hint">这是你店里专属的页面。输入安装时给你的访问码。</p>
      <input
        v-model="key"
        type="password"
        inputmode="text"
        autocomplete="off"
        aria-label="访问码"
        placeholder="访问码"
        @keyup.enter="submit"
      />
      <p v-if="error" class="err">{{ error }}</p>
      <button :disabled="busy || !key" @click="submit">
        {{ busy ? '正在验证…' : '进入' }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { setAccessKey } from '../api'

const emit = defineEmits(['unlocked'])
const key = ref('')
const error = ref('')
const busy = ref(false)

async function submit() {
  if (!key.value || busy.value) return
  busy.value = true
  error.value = ''
  // 先把码存起来，再让父组件重试 bootstrap —— 验证发生在真正的 API 调用里，
  // 这里不做本地校验（本地根本不知道对错）。
  setAccessKey(key.value)
  emit('unlocked')
  busy.value = false
}
</script>

<style scoped>
.gate {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg, #f6f7f5);
}
.card {
  width: 100%;
  max-width: 360px;
  background: var(--card, #fff);
  border-radius: 16px;
  padding: 28px 22px;
  box-shadow: 0 2px 12px rgba(22, 32, 28, 0.06);
}
h1 {
  font-size: 22px;
  margin: 0 0 8px;
  color: var(--ink, #16201c);
}
.hint {
  font-size: 14px;
  color: var(--muted, #55625c);
  margin: 0 0 18px;
}
input {
  width: 100%;
  height: 48px;
  font-size: 17px;
  padding: 0 14px;
  border: 2px solid var(--line, #dce2de);
  border-radius: 14px;
  box-sizing: border-box;
  outline: none;
}
input:focus {
  border-color: var(--accent, #0e6b58);
}
.err {
  color: #9b3a12;
  font-size: 14px;
  margin: 10px 0 0;
}
button {
  width: 100%;
  height: 52px;
  margin-top: 16px;
  border: 0;
  border-radius: 14px;
  background: var(--accent, #0e6b58);
  color: #fff;
  font-size: 17px;
  font-weight: 700;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
