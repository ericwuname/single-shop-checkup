"""数据库备份与恢复（M6-2，docs/06 §3.1 / §3.5）。

## 为什么用 `VACUUM INTO` 而不是文件拷贝

WAL 模式下（M6-4 已开），最近的写入可能还在 `-wal` 文件里没合并进主库。
直接 `cp checkup.db` 会漏掉那一部分 —— **备份看起来成功，恢复出来缺数据**。
`VACUUM INTO` 是 SQLite 官方的在线备份入口，它自己处理 WAL，产出的是一个
完整、紧凑、可直接打开的库文件。

## 保留策略

默认留最近 30 份（一天一份 = 一个月），多的按时间戳删。
不轮转的话，半年后磁盘被备份吃光 —— 那时候再发现就晚了。

## 恢复

`restore_database` 先把目标库移开成 `.bak-<时间戳>` 再覆盖，
而不是直接删 —— 万一恢复错了，还有一次反悔的机会。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

#: 备份文件名前缀
PREFIX = "checkup-"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_database(db_path: str | Path, backup_dir: str | Path, keep: int = 30) -> Path:
    """把 SQLite 库备份成一份新文件，返回该文件路径。

    `keep` = 保留最近多少份（多了删掉）。
    """
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"要备份的库不存在：{src}")

    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{PREFIX}{_timestamp()}.db"

    # 同一秒内连备两次会撞名（`VACUUM INTO` 不允许覆盖已存在的文件）。
    # 加序号而不是报错 —— 备份任务不该因为"备得太快"而失败。
    counter = 1
    while target.exists():
        target = out_dir / f"{PREFIX}{_timestamp()}-{counter}.db"
        counter += 1

    # VACUUM INTO 的路径要转成 SQLite 认的正斜杠形式（Windows 上反斜杠会被当转义）
    conn = sqlite3.connect(str(src))
    try:
        conn.execute("VACUUM INTO ?", (target.as_posix(),))
    finally:
        conn.close()

    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError(f"备份文件异常：{target}")

    rotate_backups(out_dir, keep=keep)
    return target


def rotate_backups(backup_dir: str | Path, keep: int = 30) -> list[Path]:
    """只保留最近 `keep` 份，返回被删掉的列表（按时间戳排序，老的在前面）。"""
    out_dir = Path(backup_dir)
    files = sorted(out_dir.glob(f"{PREFIX}*.db"))
    removed: list[Path] = []
    while len(files) > keep:
        victim = files.pop(0)
        try:
            victim.unlink()
            removed.append(victim)
        except OSError:
            # 删不掉就跳过（可能被占用），不要让备份任务整体失败
            continue
    return removed


def restore_database(backup_file: str | Path, target_db: str | Path) -> Path:
    """用备份覆盖目标库。

    先把目标库移开成 `.bak-<时间戳>` 再拷过去 —— **不直接删**。
    万一恢复错了（比如拿错了备份），还有一次反悔的机会。
    """
    src = Path(backup_file)
    dst = Path(target_db)
    if not src.exists():
        raise FileNotFoundError(f"备份文件不存在：{src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        spare = dst.with_name(f"{dst.name}.bak-{_timestamp()}")
        shutil.move(str(dst), str(spare))

    shutil.copy2(str(src), str(dst))
    # WAL 附属文件如果残留，会让恢复出来的库读到旧页 —— 一起清掉
    for suffix in ("-wal", "-shm"):
        stale = dst.with_name(dst.name + suffix)
        if stale.exists():
            stale.unlink()
    return dst


def upload_to_object_storage(backup_file: str | Path, bucket_config: dict | None = None) -> str | None:
    """把备份传到云对象存储（**异机存放**，docs/06 §3.1）。

    ⚠️ **M6 未实现**：任务书 §7 明令"采购付费服务前不问 —— 先报预算再买"。
    对象存储（COS/OSS）要花钱，所以这里只留接口与说明，
    实际配置由用户确认预算后填（见交付报告「待用户决定」一节）。

    配置齐了之后实现很短（COS Python SDK，`put_object` 一个调用），
    但**现在不写** —— 写了也跑不了，还会给人"已经异地备份"的错觉。
    """
    raise NotImplementedError(
        "异机备份未实现：需要先报预算并采购对象存储（docs/06 §7 禁止先买后问）。"
        "本地备份已就绪；把备份文件手动拷到另一台机器/U 盘也能满足『异机』要求。"
    )


def env_backup_dir() -> Path:
    """备份目录。默认 `<项目>/data/backups`，可用 `BACKUP_DIR` 覆盖。"""
    raw = os.environ.get("BACKUP_DIR")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent / "data" / "backups"
