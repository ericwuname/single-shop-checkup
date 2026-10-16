"""pytest 公共夹具。

每个测试函数一套**独立的临时 SQLite 库**：
- 不碰 data/checkup.db（那是真库）
- 不依赖测试执行顺序
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# M6-4：测试里把写限流放到极大，否则一次跑几百个写请求全被 429 挡住。
# 限流本身有自己的专项测试（test_m6.py），这里只保证它别干扰别的测试。
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "100000")

from server import db as db_module  # noqa: E402
from server.models import Shop  # noqa: E402
from server.seed_metrics import seed_all  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: 施工单 §3.4 的样例口径：月房租 6000、月水电 1500、毛利率 0.6、时薪 25
DEFAULT_RENT_M = 6000.0
DEFAULT_UTILITY_M = 1500.0
DEFAULT_GROSS_MARGIN = 0.6
DEFAULT_HOURLY_RATE = 25.0

PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 10, 7)


@pytest.fixture()
def session(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db_module.init_db(url)
    s = db_module.get_session()
    seed_all(s)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def shop(session) -> Shop:
    row = Shop(
        name="测试店-喜洋洋",
        category="catering",
        default_hourly_rate=DEFAULT_HOURLY_RATE,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@pytest.fixture()
def client(session, shop):
    """TestClient。lifespan 里的 ensure_db() 会复用上面已绑好的临时库。"""
    from fastapi.testclient import TestClient

    from server.main import app

    with TestClient(app) as c:
        yield c


def load_sample(session, shop) -> None:
    """把 fixtures/sample.json 灌进库。走 ORM，不走 HTTP —— 保持测试与路由解耦。"""
    import json

    from server.metrics_engine import shift_hours
    from server.models import FactDaily, Shift

    data = json.loads((FIXTURES / "sample.json").read_text(encoding="utf-8"))

    monthly = data["monthly"]
    year, mon = monthly["month"].split("-")
    session.add(
        FactDaily(
            shop_id=shop.id,
            stat_date=date(int(year), int(mon), 1),
            period_type="month",
            **monthly["values"],
        )
    )

    for day in data["daily"]:
        stat = date.fromisoformat(day["stat_date"])
        session.add(
            FactDaily(shop_id=shop.id, stat_date=stat, period_type="day", **day["values"])
        )
        for s in day["shifts"]:
            hours = shift_hours(s["start_time"], s["end_time"])
            if hours <= 0:
                continue
            session.add(
                Shift(
                    shop_id=shop.id,
                    work_date=stat,
                    worker_name=s["worker_name"],
                    start_time=s["start_time"],
                    end_time=s["end_time"],
                    hours=hours,
                    hourly_rate=s.get("hourly_rate", shop.default_hourly_rate),
                )
            )
    session.commit()


@pytest.fixture()
def sample_shop(session, shop):
    """shop + sample.json 已灌库。"""
    load_sample(session, shop)
    return shop
