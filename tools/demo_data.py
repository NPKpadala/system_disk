#!/usr/bin/env python3
"""Generate a synthetic fleet so the reports can be demonstrated offline.

Nothing here touches a real system: it writes the same daily JSON files the
collector would have written, so `monitor_fs.py report` and `fleet_report.py`
can be run end to end without waiting 30 days for evidence.

    tools/demo_data.py --out demo/ --hosts 4 --days 30
    python3 monitor_fs.py --data-dir demo report --days 30
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
from pathlib import Path
from typing import Dict, List

GB = 1024**3

# (mount, size_gb, fill_ratio, profile)
PROFILES = [
    ("/data", 500, 0.62, "busy"),
    ("/var/lib/mysql", 200, 0.71, "busy"),
    ("/opt/app", 100, 0.18, "oversized"),
    ("/mnt/backup-2019", 1000, 0.44, "idle"),
    ("/mnt/legacy-nfs", 250, 0.09, "idle"),
    ("/srv/logs", 300, 0.33, "oversized"),
]


def build_records(host: str, day: dt.date, index: int, rng: random.Random) -> List[Dict[str, object]]:
    records = []
    for position, (mount, size_gb, fill, profile) in enumerate(PROFILES):
        total = size_gb * GB
        if profile == "busy":
            used = int(total * fill) + index * rng.randint(50, 400) * 1024 * 1024
            active_io = True
            open_files = True
        elif profile == "oversized":
            used = int(total * fill) + rng.randint(0, 40) * 1024 * 1024
            active_io = True
            open_files = True
        else:  # idle
            used = int(total * fill)
            active_io = False
            open_files = False

        used = min(used, total)
        records.append(
            {
                "date": day.isoformat(),
                "host": host,
                "device": f"/dev/sd{'bcdefg'[position % 6]}1",
                "device_name": f"sd{'bcdefg'[position % 6]}1",
                "mount_point": mount,
                "fs_type": "xfs",
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": total - used,
                "used_percent": round(used / total * 100, 2),
                "read_sectors": 1_000_000 + index * 5000,
                "write_sectors": 900_000 + index * 4000,
                "delta_read_sectors": rng.randint(4000, 90000) if active_io else 0,
                "delta_write_sectors": rng.randint(4000, 90000) if active_io else 0,
                "active_io": active_io,
                "open_files": open_files,
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate demo data for monitor_fs")
    parser.add_argument("--out", type=Path, default=Path("demo"), help="Data directory to populate")
    parser.add_argument("--hosts", type=int, default=4)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    daily_dir = args.out / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    hosts = [f"app-{number:02d}.example.com" for number in range(1, args.hosts + 1)]
    today = dt.date.today()

    for index in range(args.days):
        day = today - dt.timedelta(days=args.days - 1 - index)
        records: List[Dict[str, object]] = []
        for host in hosts:
            records.extend(build_records(host, day, index, rng))
        (daily_dir / f"{day.isoformat()}.json").write_text(json.dumps(records, indent=2), encoding="utf-8")

    print(f"Wrote {args.days} days x {len(hosts)} hosts x {len(PROFILES)} filesystems to {daily_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
