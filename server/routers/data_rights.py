"""数据归属与删除（M6-2，docs/06 §3.2 / §3.3 / §3.4）。

## 这一模块只有一件事：让老板真正拥有自己的数据

| 权 | 端点 | 为什么必须有 |
|---|---|---|
| 导出 | `GET /api/shops/{id}/export` | **数据是他的，不是我们的。** 他随时能导成 Excel 拿走 —— 这是"他敢不敢把真实数据交给你"的前提 |
| 删除 | `DELETE /api/shops/{id}/data` | 他要求删的时候能**完整**删掉（不是软删一个标志位） |

## 为什么删除要管理口令

删除是不可逆的。任务书 §2.3 明确「**不做完整用户体系**（M0 不需要）」——
所以用一个环境变量里的管理口令（`ADMIN_TOKEN`）就够，不建用户表、不做登录页。
没配口令时端点直接拒绝（**不能默认放行**）。

## 页面声明（§3.4）

「数据只存在你的店的空间里，不用于任何其他用途」—— 这句话由前端展示，
后端在 `/api/privacy-notice` 提供同一份文案，两边不会说不一样的话。
"""

from __future__ import annotations

import io
import os
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Bom,
    BomVariance,
    FactDaily,
    Goods,
    GoodsAlias,
    Payable,
    Purchase,
    Receipt,
    Scrap,
    Shift,
    Shop,
    Stocktake,
    Supplier,
)

router = APIRouter(tags=["data-rights"])

#: 页面声明文案（§3.4）。前后端共用一份，避免"页面上说一套、接口说另一套"。
PRIVACY_NOTICE = (
    "数据只存在你的店的空间里，不用于任何其他用途。"
    "你随时可以把自己的数据导成 Excel 拿走，也可以要求完整删除。"
)


@router.get("/api/privacy-notice", summary="数据归属声明（页面展示用）")
def privacy_notice() -> dict[str, str]:
    return {"notice": PRIVACY_NOTICE}


@router.get("/api/shops/{shop_id}/export", summary="导出本店全部数据为 Excel")
def export_shop_data(shop_id: int, session: Session = Depends(get_db)) -> dict:
    """一个 xlsx，多个 sheet：每日录入 / 排班 / 商品 / 采购 / 入库 / 盘点 / 报废 / 应付。

    设计成"一个文件全拿走"而不是分端点：老板要的是"我的数据"，
    不是"你的七个表"。他拿去能自己打开看、能发给会计，目的就达到了。
    """
    try:
        from openpyxl import Workbook
    except ImportError as exc:                      # pragma: no cover - 依赖缺失分支
        raise HTTPException(
            status_code=503,
            detail="导出需要 openpyxl（pip install openpyxl）。当前环境没装。",
        ) from exc

    shop = session.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(status_code=404, detail=f"门店 {shop_id} 不存在")

    wb = Workbook()
    wb.remove(wb.active)

    def _sheet(title: str, headers: list[str], rows: list[list]) -> None:
        ws = wb.create_sheet(title=title[:31])       # Excel sheet 名最长 31 字符
        ws.append(headers)
        for row in rows:
            ws.append(row)

    # ---- 每日录入 ----
    facts = session.scalars(
        select(FactDaily)
        .where(FactDaily.shop_id == shop_id)
        .order_by(FactDaily.stat_date)
    ).all()
    _sheet(
        "每日录入",
        ["日期", "类型", "营业额", "采购", "订单数", "月租", "水电"],
        [
            [
                f.stat_date.isoformat(),
                f.period_type,
                f.revenue,
                f.purchase,
                f.orders,
                f.rent_m,
                f.utility_m,
            ]
            for f in facts
        ],
    )

    # ---- 排班 ----
    shifts = session.scalars(
        select(Shift).where(Shift.shop_id == shop_id).order_by(Shift.work_date)
    ).all()
    _sheet(
        "排班",
        ["日期", "姓名", "开始", "结束", "小时", "时薪"],
        [
            [s.work_date.isoformat(), s.worker_name, s.start_time, s.end_time,
             s.hours, s.hourly_rate]
            for s in shifts
        ],
    )

    # ---- 商品 ----
    goods = session.scalars(select(Goods).where(Goods.shop_id == shop_id)).all()
    _sheet(
        "商品",
        ["编码", "名称", "规格", "分类", "库存单位", "进货单位", "换算率", "单价", "ABC"],
        [
            [g.code, g.name, g.spec, g.category_code, g.unit, g.purchase_unit,
             g.unit_conversion, g.unit_price, g.abc_class]
            for g in goods
        ],
    )

    # ---- 采购 ----
    purchases = session.scalars(
        select(Purchase).where(Purchase.shop_id == shop_id).order_by(Purchase.purchase_date)
    ).all()
    _sheet(
        "采购",
        ["日期", "商品", "数量", "单位", "单价", "金额", "折价", "审批人", "备注"],
        [
            [p.purchase_date.isoformat(), p.goods_id, p.qty, p.unit, p.unit_price,
             p.amount, p.discount_amount, p.approver, p.note]
            for p in purchases
        ],
    )

    # ---- 入库 ----
    receipts = session.scalars(
        select(Receipt).where(Receipt.shop_id == shop_id).order_by(Receipt.receipt_date)
    ).all()
    _sheet(
        "入库",
        ["日期", "商品", "数量", "单位", "换算后", "实际净重", "验收"],
        [
            [r.receipt_date.isoformat(), r.goods_id, r.qty, r.unit,
             r.qty_in_stock_unit, r.actual_net_qty, r.accepted]
            for r in receipts
        ],
    )

    # ---- 盘点 ----
    takes = session.scalars(
        select(Stocktake).where(Stocktake.shop_id == shop_id).order_by(Stocktake.take_date)
    ).all()
    _sheet(
        "盘点",
        ["日期", "商品", "老板自报账面", "系统推算账面", "实盘", "差异", "原因", "审批人"],
        [
            [t.take_date.isoformat(), t.goods_id, t.book_qty, t.computed_book_qty,
             t.actual_qty,
             (None if t.actual_qty is None or t.computed_book_qty is None
              else round(t.actual_qty - t.computed_book_qty, 4)),
             t.reason_code, t.approver]
            for t in takes
        ],
    )

    # ---- 报废 ----
    scraps = session.scalars(
        select(Scrap).where(Scrap.shop_id == shop_id).order_by(Scrap.scrap_date)
    ).all()
    _sheet(
        "报废",
        ["日期", "商品", "数量", "单价", "金额", "原因代码", "审批人"],
        [
            [s.scrap_date.isoformat(), s.goods_id, s.qty, s.unit_price, s.amount,
             s.reason_code, s.approver]
            for s in scraps
        ],
    )

    # ---- 供应商与应付 ----
    suppliers = session.scalars(select(Supplier).where(Supplier.shop_id == shop_id)).all()
    _sheet(
        "供应商",
        ["名称", "主联系人", "电话", "账期(天)", "信用额度"],
        [
            [s.name, s.main_contact, s.main_phone, s.credit_days, s.credit_limit]
            for s in suppliers
        ],
    )
    payables = session.scalars(
        select(Payable).where(Payable.shop_id == shop_id).order_by(Payable.due_date)
    ).all()
    _sheet(
        "应付",
        ["供应商", "采购日", "到期日", "应付", "折价", "已付", "待付"],
        [
            [p.supplier_id, p.biz_date.isoformat(), p.due_date.isoformat(), p.amount,
             p.discount_amount, p.paid_amount, p.outstanding]
            for p in payables
        ],
    )

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    from fastapi.responses import StreamingResponse
    from urllib.parse import quote

    filename = f"门店{shop_id}-数据导出-{date.today().isoformat()}.xlsx"
    # 中文文件名要走 RFC 5987（filename*=UTF-8''...），
    # 直接塞进 Content-Disposition 会被 latin-1 编码炸掉 UnicodeEncodeError。
    # 同时给一个 ASCII 的 fallback，老浏览器不至于完全看不到文件名。
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"shop-{shop_id}-export.xlsx\"; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.delete("/api/shops/{shop_id}/data", summary="完整删除本店全部数据（需管理口令）")
def delete_shop_data(
    shop_id: int,
    session: Session = Depends(get_db),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    """M6-2 §3.3：老板要求删除时，**完整**删掉该店全部数据。

    * 必须带 `X-Admin-Token` 头，且与 `ADMIN_TOKEN` 环境变量一致
    * 没配 `ADMIN_TOKEN` 时**一律拒绝**（不能默认放行）
    * 删的是行，不是软删标志位 —— "完整删除"是这个端点的全部意义

    ⚠️ 备份里的数据不在这里处理：见交付报告「备份中的处理说明」。
    """
    expected = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="服务端没配 ADMIN_TOKEN，删除功能不可用（宁可持续不可用，也不默认放行）。",
        )
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="管理口令不对。")

    shop = session.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(status_code=404, detail=f"门店 {shop_id} 不存在")

    # 按外键依赖顺序删：先删引用 shop/goods/supplier 的，最后删 shop 自己。
    # shops 上是 ON DELETE CASCADE，但显式删更可控（也便于统计删了多少）。
    counts: dict[str, int] = {}

    def _wipe(model, label: str) -> None:
        result = session.execute(delete(model).where(model.shop_id == shop_id))
        counts[label] = result.rowcount or 0

    _wipe(BomVariance, "bom_variance")
    _wipe(Bom, "bom")
    _wipe(Payable, "payables")
    _wipe(Receipt, "receipts")
    _wipe(Purchase, "purchases")
    _wipe(Scrap, "scraps")
    _wipe(Stocktake, "stocktakes")
    _wipe(GoodsAlias, "goods_aliases")
    _wipe(Goods, "goods")
    _wipe(Supplier, "suppliers")
    _wipe(Shift, "shifts")
    _wipe(FactDaily, "fact_daily")

    # shops 没有 shop_id 列，按主键删
    result = session.execute(delete(Shop).where(Shop.id == shop_id))
    counts["shops"] = result.rowcount or 0

    session.commit()
    return {
        "deleted_shops": counts["shops"],
        "deleted_rows": counts,
        "note": (
            "本店数据已完整删除。备份文件里的副本不会自动消失 —— "
            "见交付报告「备份中的处理说明」。"
        ),
    }
