<template>
  <div>
    <!-- 首次使用：还没有店 → 建店表单（原 App 的"先告诉我这是哪家店"搬到这里） -->
    <div v-if="!shop" class="card">
      <p class="card-title">先告诉我这是哪家店</p>
      <label class="frow">
        <span class="flabel">店名</span>
        <input v-model="newShopName" type="text" placeholder="喜洋洋" aria-label="店名" />
      </label>
      <label class="frow">
        <span class="flabel">一小时工钱</span>
        <input
          v-model="newShopRate"
          type="text"
          inputmode="decimal"
          aria-label="一小时工钱"
        />
        <span class="unit">元/小时</span>
      </label>
      <button type="button" class="btn-primary" :disabled="saving" @click="createShop">
        {{ saving ? '正在建…' : '开始' }}
      </button>
    </div>

    <template v-else>
      <div class="head">
        <h1 class="h1">我的店</h1>
        <p class="sub">{{ shop.name }} · {{ categoryLabel }}</p>
      </div>

      <!-- 先填这三样 -->
      <div class="card">
        <p class="card-title">先填这三样</p>
        <p class="card-sub">
          填完，"每天至少做多少才不亏"就能算了。一个月填一次就行。
        </p>
        <label class="frow">
          <span class="flabel">每月房租</span>
          <input
            class="num"
            :value="monthDraft.rent_m ?? ''"
            type="text"
            inputmode="decimal"
            placeholder="填数"
            aria-label="每月房租"
            @input="setMonth('rent_m', $event)"
          />
          <span class="unit">元</span>
        </label>
        <label class="frow">
          <span class="flabel">每月水电燃气</span>
          <input
            class="num"
            :value="monthDraft.utility_m ?? ''"
            type="text"
            inputmode="decimal"
            placeholder="填数"
            aria-label="每月水电燃气"
            @input="setMonth('utility_m', $event)"
          />
          <span class="unit">元</span>
        </label>
        <label class="frow">
          <span class="flabel">一小时工钱</span>
          <input
            class="num"
            v-model="shopDraft.default_hourly_rate"
            type="text"
            inputmode="decimal"
            placeholder="填数"
            aria-label="一小时工钱"
          />
          <span class="unit">元/小时</span>
        </label>
      </div>

      <!-- 以后再补：三组折叠（FE-12：不许 16 个字段平铺） -->
      <div class="card">
        <p class="card-title">以后再补，越全越准</p>
        <p class="card-sub">点开一组填。不填也不影响前面三个数。</p>

        <div v-for="group in groups" :key="group.title" class="group">
          <button
            type="button"
            class="group-head"
            :aria-expanded="openGroup === group.title ? 'true' : 'false'"
            @click="toggleGroup(group.title)"
          >
            <span class="gtitle">{{ group.title }}</span>
            <span class="gstate">{{ groupState(group) }}</span>
            <span class="chev" aria-hidden="true">{{ openGroup === group.title ? '⌄' : '›' }}</span>
          </button>
          <div v-if="openGroup === group.title" class="group-body">
            <label v-for="f in group.fields" :key="f.code" class="frow">
              <span class="flabel">{{ f.label }}</span>
              <input
                class="num"
                :value="monthDraft[f.code] ?? ''"
                type="text"
                inputmode="decimal"
                placeholder="填数"
                :aria-label="f.label"
                @input="setMonth(f.code, $event)"
              />
              <span class="unit">元</span>
            </label>
            <p v-for="f in group.fields" :key="'h' + f.code" class="fhint">{{ f.hint }}</p>
          </div>
        </div>
      </div>

      <!-- 数据是你的 -->
      <div class="card">
        <p class="card-title">数据是你的</p>
        <p class="card-sub">数据只存在你的店的空间里，不用于任何其他用途。</p>
        <button type="button" class="btn-secondary" :disabled="exporting" @click="exportData">
          {{ exporting ? '正在导出…' : '导出成 Excel 表格' }}
        </button>
        <button type="button" class="danger" @click="askDelete">
          联系我们，删除我的全部数据
        </button>
      </div>

      <div class="save-zone">
        <button type="button" class="btn-primary" :disabled="saving" @click="saveSettings">
          {{ saving ? '正在存…' : '存好这些数' }}
        </button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { showToast, showConfirmDialog } from 'vant'
import { api } from '../api'

const props = defineProps({
  shop: { type: Object, default: null },
  monthFields: { type: Array, default: () => [] },
})
const emit = defineEmits(['shop-changed'])

const saving = ref(false)
const exporting = ref(false)
const openGroup = ref('')
const newShopName = ref('我的店')
const newShopRate = ref('25')
const monthDraft = ref({})
const shopDraft = ref({ name: '', default_hourly_rate: 25 })

const CATEGORY_LABEL = {
  catering: '餐饮',
  retail: '零售',
  service: '服务',
}
const categoryLabel = computed(
  () => CATEGORY_LABEL[props.shop?.category] || '小店',
)

// ---- 分组（docs/08 §6.4 D）：按 field_def.code；不在表里的一律归第 3 组 ----
const FIRST = ['rent_m', 'utility_m'] // 先填这三样里的两个月口径
const GROUP_MAP = {
  员工月薪: ['full_time_headcount', 'full_time_monthly'],
  '五险、公积金、员工餐': ['social_insurance_m', 'housing_fund_m', 'welfare_m', 'staff_meal_m'],
  '餐具、维修、其他': ['tableware_m', 'repair_m', 'cash_diff_m', 'returned_m', 'gross_margin'],
}

const groups = computed(() => {
  const byCode = new Map(props.monthFields.map((f) => [f.code, f]))
  const used = new Set([...FIRST, ...Object.values(GROUP_MAP).flat()])
  const out = []
  for (const [title, codes] of Object.entries(GROUP_MAP)) {
    const fields = codes.map((c) => byCode.get(c)).filter(Boolean)
    // 未列出的其他字段自动进第 3 组 —— 将来加字段不用改这页（元数据驱动）
    if (title === '餐具、维修、其他') {
      for (const f of props.monthFields) {
        if (!used.has(f.code)) fields.push(f)
      }
    }
    out.push({ title, fields })
  }
  return out
})

function groupState(group) {
  const filled = group.fields.filter(
    (f) => monthDraft.value[f.code] !== null && monthDraft.value[f.code] !== undefined && monthDraft.value[f.code] !== '',
  ).length
  return filled ? `已填 ${filled}/${group.fields.length}` : '还没填'
}

function toggleGroup(title) {
  openGroup.value = openGroup.value === title ? '' : title
}

function setMonth(code, e) {
  let v = e.target.value.replace(/[^\d.]/g, '')
  e.target.value = v
  monthDraft.value[code] = v === '' ? null : v
}

async function loadAll() {
  if (!props.shop) return
  const data = await api.getMonthly(props.shop.id, currentMonth())
  monthDraft.value = { ...data.values }
  shopDraft.value = {
    name: props.shop.name,
    default_hourly_rate: props.shop.default_hourly_rate,
  }
}

function currentMonth() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

async function createShop() {
  saving.value = true
  try {
    await api.createShop({
      name: newShopName.value || '我的店',
      default_hourly_rate: Number(newShopRate.value) || 25,
    })
    showToast('建好了，先填房租和水电')
    emit('shop-changed')
  } catch (err) {
    showToast(err.message)
  } finally {
    saving.value = false
  }
}

async function saveSettings() {
  if (!props.shop) return
  saving.value = true
  try {
    await api.patchShop(props.shop.id, {
      name: shopDraft.value.name,
      default_hourly_rate: Number(shopDraft.value.default_hourly_rate) || 25,
    })
    await api.putMonthly(props.shop.id, currentMonth(), monthDraft.value)
    showToast('存好了')
    emit('shop-changed')
  } catch (err) {
    showToast(err.message)
  } finally {
    saving.value = false
  }
}

/** 导出：带访问码的 fetch → Blob → 临时 <a download>。
     不能直接 <a href> —— 要带 X-Access-Key 头（docs/08 §6.4 D）。 */
async function exportData() {
  exporting.value = true
  try {
    const res = await fetch(`/api/shops/${props.shop.id}/export`)
    if (!res.ok) throw new Error(`导出失败（HTTP ${res.status}）`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `门店数据导出-${new Date().toISOString().slice(0, 10)}.xlsx`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
    showToast('已下载')
  } catch (err) {
    showToast(err.message)
  } finally {
    exporting.value = false
  }
}

/** 删除：只弹说明。不调用任何删除接口、不出现管理口令（F5）。
     删除权是管理员的，不能下放到浏览器。 */
async function askDelete() {
  try {
    await showConfirmDialog({
      title: '删除我的全部数据',
      message:
        '请联系给你安装的人，核实后为你彻底删除。删除后这里的一切都找不回来。',
      confirmButtonText: '知道了',
      showCancelButton: false,
    })
  } catch {
    // 用户关闭 = 什么都不做
  }
}

onMounted(loadAll)
watch(() => props.shop?.id, loadAll)

defineExpose({ reload: loadAll })
</script>

<style scoped>
.head {
  margin: 4px 0 12px;
}
.h1 {
  font-size: 26px;
  font-weight: 700;
  margin: 0;
}
.sub {
  font-size: 13px;
  color: var(--muted);
  margin: 2px 0 0;
}
.frow {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 48px;
  border-bottom: 1px solid var(--line-soft);
}
.flabel {
  font-size: 16px;
  font-weight: 700;
  flex: 1;
}
.frow input {
  border: 0;
  outline: none;
  text-align: right;
  font-size: 17px;
  font-weight: 700;
  background: transparent;
  width: 120px;
}
.unit {
  font-size: 13px;
  color: var(--muted);
  min-width: 44px;
}
.group {
  border-bottom: 1px solid var(--line-soft);
}
.group:last-child {
  border-bottom: 0;
}
.group-head {
  width: 100%;
  min-height: 52px;
  display: flex;
  align-items: center;
  gap: 8px;
  border: 0;
  background: transparent;
  padding: 0;
}
.gtitle {
  flex: 1;
  text-align: left;
  font-size: 16px;
  font-weight: 700;
  color: var(--ink);
}
.gstate {
  font-size: 13px;
  color: var(--muted);
}
.chev {
  font-size: 18px;
  color: var(--muted);
}
.group-body {
  padding-bottom: 12px;
}
.fhint {
  font-size: 12px;
  color: var(--muted);
  margin: 2px 0 8px;
  text-align: right;
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
.danger {
  width: 100%;
  height: 48px;
  margin-top: 10px;
  border: 1px solid var(--danger-line);
  border-radius: var(--radius-btn);
  background: #fff;
  color: var(--danger-ink);
  font-size: 15px;
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
</style>
