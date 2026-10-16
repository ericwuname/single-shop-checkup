"""M6 测试（docs/06）：稳定性 / 数据安全 / 备份恢复。

授权依据：`docs/06-M6长程施工任务书.md` §3 / §5 / §6。

| 门禁 | 判据 | 测试 |
|---|---|---|
| 1 | pytest 154+ 不回归 | 本文件全部 + 全套 |
| 3 | 审批阈值双向断言 | `test_m5.py::test_approval_threshold_*` |
| 4 | 可解释损耗率 | `test_m5.py::test_explained_loss_rate_*` |
| 5 | 公网可用 | 手工（手机 4G），见交付报告 |
| 6 | 备份恢复演练 | `test_restore_drill_*`（真删真恢复） |
| 8 | 变异验证 | 交付报告 §5 |

**功能冻结令**（docs/06 §1）：M6 不新增业务功能。本文件测的全是
部署/安全/稳定性/缺陷修复，没有新表、新指标、新页面。
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("session")


# =========================================================================== #
# M6-4 · 稳定性
# =========================================================================== #
def test_health_reports_db_writable(client):
    """M6-4 §5.3：`/api/health` 必须报数据库**可写**，不只是"能连上"。

    "能读不能写"（磁盘满、只读挂载）时原来的实现照样返回 ok ——
    监控一片绿，而老板的录入全在失败。这是最坑的一种故障形态。
    """
    data = client.get("/api/health").json()
    assert data["db_writable"] is True
    assert data["status"] == "ok"
    assert data["db_error"] is None


def test_wal_and_busy_timeout_are_set(client, session):
    """M6-4 §5.1：WAL + busy_timeout 必须是连接级 PRAGMA。

    WAL 让读写不互斥（晚上 10 点录入高峰不至于锁死）；
    busy_timeout 让拿不到写锁时等 5 秒，而不是立刻抛
    "database is locked"（老板看到的就是"保存失败"）。
    """
    from sqlalchemy import text

    row = session.execute(text("PRAGMA journal_mode")).scalar()
    assert str(row).lower() == "wal", f"journal_mode 应是 wal，实际 {row}"

    timeout = session.execute(text("PRAGMA busy_timeout")).scalar()
    assert int(timeout) >= 5000, f"busy_timeout 应 ≥ 5000ms，实际 {timeout}"


def test_rate_limit_blocks_excessive_writes(monkeypatch):
    """M6-4 §5.5：单店每分钟写请求超限 → 429 + 大白话。

    为什么限流是**数据质量**保护而不是防攻击：最常见的场景是
    老板手机页面卡了，他连点十几次保存 —— 同一条数据被写十遍。
    """
    # 不能往已启动的全局 app 加中间件（FastAPI 会抛 RuntimeError），
    # 所以造一个只挂限流的小 app 来测 —— 也顺带证明中间件是自包含的。
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.middleware import RateLimitMiddleware

    probe_app = FastAPI()
    probe_app.add_middleware(RateLimitMiddleware, limit=3)

    @probe_app.post("/api/scraps")
    def _scraps(payload: dict):
        return {"ok": True, "shop_id": payload.get("shop_id")}

    client = TestClient(probe_app)

    codes = []
    for i in range(6):
        res = client.post(
            "/api/scraps",
            json={"shop_id": 7, "goods_id": 1, "scrap_date": "2026-03-10",
                  "qty": 1, "reason_code": "T02"},
        )
        codes.append(res.status_code)

    assert codes[:3] == [200, 200, 200], f"前 3 次应该都放行，实际 {codes}"
    assert codes[3] == 429, f"第 4 次应该被限，实际 {codes}"

    body = client.post(
        "/api/scraps",
        json={"shop_id": 7, "goods_id": 1, "scrap_date": "2026-03-10",
              "qty": 1, "reason_code": "T02"},
    )
    assert body.status_code == 429
    assert "太多次" in body.json()["detail"], "429 要说人话，不能只返回一个码"


def test_rate_limit_is_per_shop_not_global(monkeypatch):
    """限流判据是**单店**，不是全局 —— 否则一家店打爆会连累别的店。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.middleware import RateLimitMiddleware

    probe_app = FastAPI()
    probe_app.add_middleware(RateLimitMiddleware, limit=2)

    @probe_app.post("/api/scraps")
    def _scraps(payload: dict):
        return {"ok": True}

    client = TestClient(probe_app)
    for _ in range(2):
        assert client.post("/api/scraps", json={"shop_id": 7}).status_code == 200
    assert client.post("/api/scraps", json={"shop_id": 7}).status_code == 429
    assert client.post("/api/scraps", json={"shop_id": 8}).status_code == 200


def test_rate_limit_does_not_touch_reads(client, shop):
    """读操作（看报告）打不坏数据库，限它只会让老板打不开页面。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.middleware import RateLimitMiddleware

    probe_app = FastAPI()
    probe_app.add_middleware(RateLimitMiddleware, limit=1)

    @probe_app.get("/api/report")
    def _report(shop_id: int = 0):
        return {"ok": True}

    client = TestClient(probe_app)
    for _ in range(5):
        assert client.get("/api/report", params={"shop_id": 1}).status_code == 200


def test_rate_limit_ignores_health_checks(client, shop):
    """监控每秒打一次 /api/health，限它就废了。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.middleware import RateLimitMiddleware

    probe_app = FastAPI()
    probe_app.add_middleware(RateLimitMiddleware, limit=1)

    @probe_app.get("/api/health")
    def _health():
        return {"status": "ok"}

    client = TestClient(probe_app)
    for _ in range(5):
        assert client.get("/api/health").status_code == 200


def test_error_log_does_not_record_business_data(client, shop, caplog):
    """M6-4 §5.2：错误日志**不许出现业务数据**（营业额/采购额是老板的商业秘密）。

    只记：方法、路径、状态码、店 id、异常消息。日志里出现金额数字，
    等于把账本摊在日志文件里。
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="checkup.api"):
        # 故意打一个 400：报废不填原因代码
        g = client.post(
            "/api/goods",
            json={"shop_id": shop.id, "name": "日志测试品", "spec": "散装",
                  "category_code": "02-01", "confirmed_not_duplicate": True},
        ).json()
        client.post(
            "/api/scraps",
            json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
                  "qty": 1, "reason": "坏了"},           # 缺 reason_code → 400
        )

    records = [r for r in caplog.records if r.name == "checkup.api"]
    assert records, "400 应该被记下来"
    text = "\n".join(r.getMessage() for r in records)
    assert "/api/scraps" in text and "400" in text
    # 关键断言：日志里不能有请求体内容
    assert "坏了" not in text, "错误日志泄漏了请求体内容"
    assert f"shop={shop.id}" in text, "要能按店回溯（老板说『我保存失败了』时，得知道是哪家店）"


# =========================================================================== #
# M6-2 · 数据安全
# =========================================================================== #
def test_export_shop_data_returns_excel(client, shop):
    """M6-2 §3.2：老板随时能把自己的数据导成 Excel 拿走。

    **数据是他的，不是我们的** —— 这一条不是锦上添花，
    是"他敢不敢把真实数据交给你"的前提。
    """
    from datetime import timedelta

    for i in range(3):
        client.put(
            "/api/daily",
            json={"shop_id": shop.id, "stat_date": (date(2026, 3, 1) + timedelta(days=i)).isoformat(),
                  "values": {"revenue": 4200 + i, "purchase": 1100}},
        )

    res = client.get(f"/api/shops/{shop.id}/export")
    assert res.status_code == 200, res.text
    assert "spreadsheet" in res.headers["content-type"]
    body = res.content
    # xlsx 是 zip 包，头两个字节是 PK
    assert body[:2] == b"PK", "导出物应该是 xlsx（zip 容器）"
    assert len(body) > 2000, f"导出内容太少（{len(body)} 字节），数据没进去？"


def test_delete_shop_data_requires_token(client, shop, monkeypatch):
    """M6-2 §3.3：删除权是重操作，必须带管理口令（任务书 §2.3：不做完整用户体系）。"""
    from datetime import timedelta

    # 先配一个口令，才能测"没带/带错"这两种拒绝（没配时是 503，另有一条测试管）
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")

    for i in range(3):
        client.put(
            "/api/daily",
            json={"shop_id": shop.id, "stat_date": (date(2026, 3, 1) + timedelta(days=i)).isoformat(),
                  "values": {"revenue": 4200, "purchase": 1100}},
        )

    # 没带头 → 401
    bad = client.request("DELETE", f"/api/shops/{shop.id}/data")
    assert bad.status_code == 401, f"没带头应被拒，实际 {bad.status_code}"

    # 口令不对 → 拒
    wrong = client.request(
        "DELETE", f"/api/shops/{shop.id}/data",
        headers={"X-Admin-Token": "wrong-token"},
    )
    assert wrong.status_code == 401

    # 数据还在
    rows = client.get(
        "/api/daily", params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"}
    ).json()
    assert rows, "被拒的删除不该动数据"


def test_delete_shop_data_wipes_everything(client, shop, monkeypatch):
    """M6-2 §3.3：给了正确口令 → 该店全部业务数据真的没了（含 shop 行本身）。"""
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    from datetime import timedelta

    for i in range(3):
        client.put(
            "/api/daily",
            json={"shop_id": shop.id, "stat_date": (date(2026, 3, 1) + timedelta(days=i)).isoformat(),
                  "values": {"revenue": 4200, "purchase": 1100}},
        )
    client.post(
        "/api/goods",
        json={"shop_id": shop.id, "name": "待删商品", "spec": "散装",
              "category_code": "02-01", "confirmed_not_duplicate": True},
    )
    shop_id = shop.id

    ok = client.request(
        "DELETE", f"/api/shops/{shop_id}/data",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["deleted_shops"] == 1

    # 店没了 → 后续查询该店的数据应该是空/404
    # 店行本身也没了 → 后续查该店直接 404
    gone = client.get(
        "/api/daily", params={"shop_id": shop_id, "start": "2026-03-01", "end": "2026-03-31"}
    )
    assert gone.status_code == 404, f"店已删，应 404，实际 {gone.status_code}"


# =========================================================================== #
# M6-2 §3.5 · 备份与恢复演练（门禁 6）
# =========================================================================== #
def test_backup_script_creates_valid_sqlite_copy(tmp_path):
    """备份用的是 `VACUUM INTO`（不是文件拷贝）—— 后者在 WAL 模式下会漏掉未合并的页。"""
    from server.backup import backup_database

    # 造一个真库：灌几条数据再备
    db_file = tmp_path / "src.db"
    os.environ["CHECKUP_DB"] = str(db_file)
    from server import db as db_module

    engine = db_module.init_db(f"sqlite:///{db_file.as_posix()}")
    with db_module.session_scope() as s:
        from server.models import Shop

        s.add(Shop(name="备份演练店", category="catering", default_hourly_rate=25.0))
    engine.dispose()

    out = tmp_path / "backups"
    created = backup_database(str(db_file), str(out), keep=30)
    assert created.exists(), "备份文件没生成"
    assert created.stat().st_size > 0

    # 备份必须是**能打开的合法 SQLite**
    import sqlite3

    conn = sqlite3.connect(str(created))
    try:
        shops = conn.execute("SELECT name FROM shops").fetchall()
        assert any("备份演练店" in n for (n,) in shops), "备份里没有刚灌的店"
    finally:
        conn.close()


def test_backup_rotation_keeps_only_recent(tmp_path):
    """保留最近 30 份，多的删掉 —— 否则半年后磁盘被备份吃光。"""
    from server.backup import backup_database, rotate_backups

    db_file = tmp_path / "src.db"
    from server import db as db_module

    engine = db_module.init_db(f"sqlite:///{db_file.as_posix()}")
    engine.dispose()

    out = tmp_path / "backups"
    for _ in range(35):
        backup_database(str(db_file), str(out), keep=30)
    files = sorted(out.glob("checkup-*.db"))
    assert len(files) <= 30, f"应只保留 30 份，实际 {len(files)}"


def test_restore_drill_delete_then_recover(tmp_path):
    """🔴 门禁 6：**真删一次、真恢复一次**。

    任务书原话："没演练过的备份等于没有备份。"
    这条测试就是演练本身 —— 删掉数据 → 从备份恢复 → 数据回来了。
    交付报告里会附上它的真实运行输出。
    """
    import sqlite3

    from server.backup import backup_database, restore_database

    db_file = tmp_path / "drill.db"
    from server import db as db_module

    engine = db_module.init_db(f"sqlite:///{db_file.as_posix()}")
    with db_module.session_scope() as s:
        from server.models import Shop

        s.add(Shop(name="演练店-删除前", category="catering", default_hourly_rate=25.0))
    engine.dispose()

    backup = backup_database(str(db_file), str(tmp_path / "b"), keep=30)

    # 真删：把整库清掉（模拟最坏情况）
    conn = sqlite3.connect(str(db_file))
    conn.execute("DELETE FROM shops")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM shops").fetchone()[0] == 0
    conn.close()

    # 真恢复
    restore_database(str(backup), str(db_file))
    conn = sqlite3.connect(str(db_file))
    try:
        names = [n for (n,) in conn.execute("SELECT name FROM shops").fetchall()]
    finally:
        conn.close()
    assert "演练店-删除前" in names, "恢复后数据没回来 —— 备份是假的"
