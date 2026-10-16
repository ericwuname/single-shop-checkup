# 单店经营体检 · M1

给没有电脑的餐饮小店老板：**手机 10 秒录入，当天看到「钱花在哪、哪个环节在漏」**。

- 不做小一号 ERP。不做收银、不做会员、不做营销。
- 只做三件事：**账实差异**（后面做）、**隐性成本**、**大白话结论**。
- M1 范围：单门店 / 6 个原子数字 / 5 个指标 / 一句话结论。

---

## 快速开始

### 后端

```bash
cd single-shop-checkup
python -m venv .venv
.venv/Scripts/activate            # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
python -m pytest tests/ -q                        # 应全绿
python -m uvicorn server.main:app --port 8000     # 启动
curl -s localhost:8000/docs                       # Swagger
```

> 镜像说明：施工单验收命令写的是清华镜像，实测在本机 pip 26 下返回
> `from versions: none`（拿不到版本列表），换阿里云镜像正常。两个都行，谁通用谁。

### 前端

```bash
cd web
npm install
npm run dev        # http://localhost:5173
```

手机试用：电脑和手机连同一个 Wi-Fi，手机开 `http://<电脑局域网IP>:5173`
（Vite 已配 `host: 0.0.0.0`，`/api` 自动代理到 127.0.0.1:8000）。

### 端到端自检（服务启动后跑）

```bash
python scripts/smoke_e2e.py
```

它会建店 → 灌 7 天样例 → 出报告 → 把引擎结果和手工 Excel 口径逐个对账。

---

## 目录

```
single-shop-checkup/
├── server/
│   ├── main.py              FastAPI 入口 + /api/health + /api/meta/*
│   ├── db.py                engine / Session / init_db
│   ├── models.py            ORM：7 张表
│   ├── schemas.py           Pydantic
│   ├── metrics_engine.py    ⭐ 公式解析 + 依赖拓扑 + 四道守卫
│   ├── benchmarks.py        业态基准区间 + 大白话话术
│   ├── llm.py               ⭐ LLMProvider 抽象层（M1 只留接口）
│   ├── seed_metrics.py      ⭐ 字段/指标/日历种子
│   └── routers/             shops / daily / report
├── web/                     Vue3 + Vite + Vant + PWA manifest
├── tests/                   60 条单测（含两条硬门禁）
├── scripts/smoke_e2e.py     端到端对账脚本
└── uploads/                 M3 拍照才用
```

---

## 两条地基

### 1. 指标不算在代码里，算在表里

`metric_def` 一张表定义所有指标：

| code | formula | plain_text |
|---|---|---|
| `labor_cost_rate` | `labor_cost / revenue` | 人工占营业额 {value} |
| `efficiency` | `revenue / hours` | 每投一小时换回 {value} 元 |
| `avg_ticket` | `revenue / orders` | 平均每单 {value} 元 |
| `food_cost_rate` | `purchase / revenue` | 食材吃掉 {value} |
| `breakeven_daily` | `(rent_m + utility_m + labor_m) / 30 / gross_margin` | 你每天至少要做 {value} 元才不亏 |

**加一个新指标 = 往 `metric_def` 插一行，不改任何 Python / 前端代码。**

```sql
INSERT INTO metric_def
  (code, name, layer, unit, formula, depends_on, benchmark_key,
   warn_high, warn_low, higher_better, plain_text, enabled, version,
   aggregation, scope, inputs)
VALUES
  ('dine_in_avg_ticket', '堂食客单价', 'derived', '元', 'revenue / orders', NULL, NULL,
   NULL, NULL, NULL, '平均每单 {value} 元', 1, 1, 'ratio', 'day', NULL);
```

刷新页面就有了。这条有单测守着（`tests/test_metrics_engine.py::test_gate3_new_metric_by_single_row_insert`），
做不到就是架构失败。

### 2. 字段也不算在代码里

`field_def` 定义原子字段，`scope` 区分日口径 / 月口径。
前端表单由 `/api/meta/fields` 驱动 —— 加字段同样不用改前端。

---

## 引擎的四道守卫

| 守卫 | 行为 | 为什么 |
|---|---|---|
| 分母为 0 或缺失 | 返回 `None`，**不算成 0** | 0/0 算成 0 会让老板以为"这个环节没问题" |
| 派生**比率**做分母 | 抛 `DerivedMetricAsDivisorError` | 比率跨期不能相加也不能平均，进了分母就是"看着像真的"的错数 |
| 公式引用不存在的名字 | 抛 `UnknownFieldError` | 早炸，不要静默出空值 |
| 跨期聚合 | 先 `Σ原子`，再算比率 | 对比率取平均 = 算错 |

可加派生量（`hours` / `labor_cost`）**允许**做分母 —— 否则
施工单自己列的 `efficiency = revenue / hours` 就会被判死。依据是
`metric_def.aggregation` 列（`additive` / `ratio`）。

---

## M1 边界（不做的事）

登录注册 / 多店 / 供应商 / 入库 / 存货 / 盘点 / 审批 / 拍照 / 财报 / 权限 ——
全部不做。M1 期间**禁止加业务表**（新的运营口径一律先看能不能塞进
`field_def` / `metric_def`）。

指标只有 5 个，输入框只有 6 个。**够用就停。**
