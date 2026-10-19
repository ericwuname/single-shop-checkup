<template>
  <!-- M7 外壳（docs/08 §6.3）：三页签（今天/本周/我的店）+ 底部导航 + 访问码门。
       旧的三页（录入/报告/设置）按设计稿重做为 views/Today|Week|Shop；
       旧组件 DailyInput/ShiftTable/ResultCard 在新页面全部验收通过前不删。 -->
  <!-- 访问码门（M7 WP-1）：只有公网模式后端返回 401 时才出现（docs/08 §4.3）。
       locked 时主界面整体不渲染 —— 不给"半加载"的假象。 -->
  <AccessGate v-if="locked" @unlocked="retryBootstrap" />
  <template v-else>
    <div class="page-m7">
      <Today
        v-if="tab === 'today' && booted"
        ref="todayRef"
        :shop="shop"
        :refresh-key="refreshKey"
      />
      <Week
        v-if="tab === 'week' && booted"
        ref="weekRef"
        :shop="shop"
        @go-today="switchTo('today')"
        @go-shop="switchTo('shop')"
      />
      <Shop
        v-if="tab === 'shop' && booted"
        ref="shopRef"
        :shop="shop"
        :month-fields="monthFields"
        @shop-changed="onShopChanged"
      />
      <p v-if="booted && !shop && tab !== 'shop'" class="noshop">
        还没有店。去「我的店」建一个。
      </p>
    </div>

    <BottomNav v-model="tab" />
  </template>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { showToast } from 'vant'
import { api, AccessDenied } from './api'
import { flushQueue, pendingCount } from './offline_queue'
import AccessGate from './components/AccessGate.vue'
import BottomNav from './components/BottomNav.vue'
import Today from './views/Today.vue'
import Week from './views/Week.vue'
import Shop from './views/Shop.vue'

const tab = ref('today')
const locked = ref(false)
const booted = ref(false)
const shop = ref(null)
const monthFields = ref([])
const refreshKey = ref(0)

const todayRef = ref(null)
const weekRef = ref(null)
const shopRef = ref(null)

async function bootstrap() {
  const [fields, shops] = await Promise.all([api.metaFields('month'), api.listShops()])
  monthFields.value = fields
  shop.value = shops[0] || null
  booted.value = true
}

async function onShopChanged() {
  // 我的店建店/保存后：刷新店信息与月字段；Today/Week 的数据也要跟着新
  await bootstrap()
  refreshKey.value += 1
  if (tab.value === 'week') weekRef.value?.reload()
}

function switchTo(name) {
  tab.value = name
}

onMounted(async () => {
  try {
    await bootstrap()
  } catch (err) {
    if (err instanceof AccessDenied) {
      locked.value = true
      return
    }
    console.warn('连不上后端：', err.message)
  }

  // M6-4 §5.4：启动时补一次队列（昨晚在后厨存的数，今早打开就连上了）
  const before = pendingCount()
  if (before > 0) {
    const r = await flushQueue(api.putDaily)
    if (r.sent > 0) showToastOnce(`补上了之前存的 ${r.sent} 天`)
    else if (r.failed > 0) showToastOnce(`还有 ${r.failed} 天存在手机里没传上去，连上网再试`)
  }
})

let toastTimer = null
function showToastOnce(msg) {
  // controllerchange / 快速切换时避免 toast 循环轰炸：同一条消息 3 秒内只弹一次
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => showToast(msg), 300)
}
</script>

<style scoped>
.noshop {
  text-align: center;
  color: var(--muted);
  font-size: 14px;
  margin-top: 32px;
}
</style>
