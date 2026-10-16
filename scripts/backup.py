#!/usr/bin/env python
"""每日备份（M6-2，docs/06 §3.1）。

用法：
    python scripts/backup.py                 # 备份默认库
    python scripts/backup.py --db data/checkup.db --dir data/backups --keep 30
    python scripts/backup.py --restore <备份文件>     # 恢复

**为什么要用 `VACUUM INTO` 而不是直接拷文件**：WAL 模式下（M6-4 已开），
最近的写入可能还在 `-wal` 文件里没合并进主库。直接 `cp` 会漏掉那一部分 ——
备份看起来成功，恢复出来缺数据。`VACUUM INTO` 自己处理 WAL。

**异机存放**：任务书要求备份放另一台机器。这一步**没有自动实现** ——
对象存储要花钱，任务书 §7 明令"采购付费服务前不问"。
现在满足"异机"的方式：把 `data/backups/` 定时同步到另一台电脑/U 盘/网盘
（Windows 可用 `robocopy`，脚本里有示例命令）。见交付报告「待用户决定」。

**定时**：Windows 用任务计划程序，每天一次，命令见 `docs/M6-部署手册.md`。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "checkup.db"
DEFAULT_DIR = ROOT / "data" / "backups"


def main() -> int:
    parser = argparse.ArgumentParser(description="单店经营体检 · 每日备份/恢复")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="要备份的 SQLite 库")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="备份目录")
    parser.add_argument("--keep", type=int, default=30, help="保留最近几份（默认 30）")
    parser.add_argument("--restore", metavar="BACKUP_FILE", help="从指定备份恢复")
    args = parser.parse_args()

    from server.backup import backup_database, restore_database

    if args.restore:
        target = restore_database(args.restore, args.db)
        print(f"已恢复：{args.restore} -> {target}")
        print("提示：恢复前会把原库移成 .bak-<时间戳>，确认无误后可以手动删掉它。")
        return 0

    if not Path(args.db).exists():
        print(f"库不存在：{args.db}", file=sys.stderr)
        return 1

    created = backup_database(args.db, args.dir, keep=args.keep)
    size_kb = created.stat().st_size / 1024
    print(f"已备份：{created}  ({size_kb:.0f} KB)")

    files = sorted(Path(args.dir).glob("checkup-*.db"))
    print(f"当前共 {len(files)} 份备份（上限 {args.keep}）")
    print()
    print("异机同步示例（Windows，把备份拷到另一台机器或 U 盘）：")
    print(f'  robocopy "{Path(args.dir)}" "\\\\另一台机器\\backups" /MIR')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
