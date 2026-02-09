#!/usr/bin/env python3
"""Filesystem activity monitor.

Collect daily filesystem usage/activity metrics and generate a 30-day report
flagging inactive filesystems.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

DEFAULT_EXCLUDE_MOUNTS = {"/", "/boot", "/var", "/usr"}
DEFAULT_EXCLUDE_TYPES = {
    "proc",
    "sysfs",
    "devtmpfs",
    "tmpfs",
    "cgroup",
    "cgroup2",
    "overlay",
    "squashfs",
    "rpc_pipefs",
    "autofs",
    "mqueue",
    "hugetlbfs",
    "securityfs",
    "pstore",
    "debugfs",
    "tracefs",
    "configfs",
    "fusectl",
}


@dataclass
class MountInfo:
    device: str
    mount_point: str
    fs_type: str


@dataclass
class DiskStats:
    read_sectors: int
    write_sectors: int


def parse_mounts() -> List[MountInfo]:
    mounts: List[MountInfo] = []
    with open("/proc/mounts", "r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mount_point, fs_type = parts[:3]
            mounts.append(MountInfo(device=device, mount_point=mount_point, fs_type=fs_type))
    return mounts


def resolve_device_name(device: str) -> Optional[str]:
    if not device.startswith("/dev/"):
        return None
    real_device = os.path.realpath(device)
    base = os.path.basename(real_device)
    if base.startswith("dm-"):
        dm_name_path = Path("/sys/block") / base / "dm/name"
        if dm_name_path.exists():
            try:
                dm_name = dm_name_path.read_text(encoding="utf-8").strip()
            except OSError:
                dm_name = base
            return base
    return base


def read_diskstats() -> Dict[str, DiskStats]:
    stats: Dict[str, DiskStats] = {}
    with open("/proc/diskstats", "r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 14:
                continue
            name = parts[2]
            read_sectors = int(parts[5])
            write_sectors = int(parts[9])
            stats[name] = DiskStats(read_sectors=read_sectors, write_sectors=write_sectors)
    return stats


def disk_usage_bytes(mount_point: str) -> Tuple[int, int, int, float]:
    usage = shutil.disk_usage(mount_point)
    used_percent = (usage.used / usage.total) * 100 if usage.total else 0
    return usage.total, usage.used, usage.free, used_percent


def has_lsof() -> bool:
    return shutil.which("lsof") is not None


def has_open_files_lsof(mount_point: str) -> bool:
    try:
        result = subprocess.run(
            ["lsof", "+f", "--", mount_point],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def has_open_files_procfs(mount_point: str) -> bool:
    mount_point = os.path.normpath(mount_point)
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        proc_path = Path("/proc") / pid
        for link_name in ("cwd", "root"):
            link_path = proc_path / link_name
            try:
                target = os.path.realpath(link_path)
            except OSError:
                continue
            if target.startswith(mount_point + os.sep) or target == mount_point:
                return True
        fd_path = proc_path / "fd"
        try:
            fds = os.listdir(fd_path)
        except OSError:
            continue
        for fd in fds:
            fd_link = fd_path / fd
            try:
                target = os.path.realpath(fd_link)
            except OSError:
                continue
            if target.startswith(mount_point + os.sep) or target == mount_point:
                return True
    return False


def has_open_files(mount_point: str) -> bool:
    if has_lsof():
        return has_open_files_lsof(mount_point)
    return has_open_files_procfs(mount_point)


def list_target_mounts(exclude_mounts: Iterable[str], exclude_types: Iterable[str]) -> List[MountInfo]:
    excluded_mounts = {os.path.normpath(mnt) for mnt in exclude_mounts}
    excluded_types = set(exclude_types)
    mounts = []
    for mount in parse_mounts():
        if os.path.normpath(mount.mount_point) in excluded_mounts:
            continue
        if mount.fs_type in excluded_types:
            continue
        mounts.append(mount)
    return mounts


def load_state(state_path: Path) -> Dict[str, Dict[str, int]]:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state_path: Path, state: Dict[str, Dict[str, int]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def collect(
    data_dir: Path,
    exclude_mounts: Iterable[str],
    exclude_types: Iterable[str],
    activity_threshold_sectors: int,
) -> None:
    mounts = list_target_mounts(exclude_mounts, exclude_types)
    diskstats = read_diskstats()
    state_path = data_dir / "state.json"
    prev_state = load_state(state_path)
    today = dt.date.today().isoformat()
    daily_dir = data_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    daily_path = daily_dir / f"{today}.json"

    results = []
    new_state: Dict[str, Dict[str, int]] = {}
    for mount in mounts:
        total, used, free, used_percent = disk_usage_bytes(mount.mount_point)
        device_name = resolve_device_name(mount.device)
        read_sectors = 0
        write_sectors = 0
        delta_read = None
        delta_write = None
        active_io = None
        if device_name and device_name in diskstats:
            current_stats = diskstats[device_name]
            read_sectors = current_stats.read_sectors
            write_sectors = current_stats.write_sectors
            prev = prev_state.get(device_name)
            if prev:
                delta_read = max(read_sectors - prev.get("read_sectors", 0), 0)
                delta_write = max(write_sectors - prev.get("write_sectors", 0), 0)
                active_io = (delta_read + delta_write) >= activity_threshold_sectors
            new_state[device_name] = {
                "read_sectors": read_sectors,
                "write_sectors": write_sectors,
            }

        open_files = has_open_files(mount.mount_point)

        results.append(
            {
                "date": today,
                "device": mount.device,
                "device_name": device_name,
                "mount_point": mount.mount_point,
                "fs_type": mount.fs_type,
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "used_percent": round(used_percent, 2),
                "read_sectors": read_sectors,
                "write_sectors": write_sectors,
                "delta_read_sectors": delta_read,
                "delta_write_sectors": delta_write,
                "active_io": active_io,
                "open_files": open_files,
            }
        )

    daily_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    save_state(state_path, new_state)


def load_daily_files(daily_dir: Path, days: int) -> List[Dict[str, object]]:
    if not daily_dir.exists():
        return []
    cutoff = dt.date.today() - dt.timedelta(days=days)
    records: List[Dict[str, object]] = []
    for path in sorted(daily_dir.glob("*.json")):
        try:
            date = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if date < cutoff:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list):
            records.extend(data)
    return records


def aggregate(records: List[Dict[str, object]], growth_threshold_bytes: int) -> List[Dict[str, object]]:
    by_mount: Dict[str, List[Dict[str, object]]] = {}
    for record in records:
        mount_point = str(record.get("mount_point"))
        by_mount.setdefault(mount_point, []).append(record)

    summary: List[Dict[str, object]] = []
    for mount_point, items in by_mount.items():
        items_sorted = sorted(items, key=lambda item: str(item.get("date")))
        used_values = [int(item.get("used_bytes", 0)) for item in items_sorted]
        max_used = max(used_values) if used_values else 0
        min_used = min(used_values) if used_values else 0
        growth = max_used - min_used

        active_io_days = sum(1 for item in items_sorted if item.get("active_io") is True)
        open_files_days = sum(1 for item in items_sorted if item.get("open_files") is True)
        any_unknown_io = any(item.get("active_io") is None for item in items_sorted)
        inactive = (
            active_io_days == 0
            and open_files_days == 0
            and growth <= growth_threshold_bytes
            and not any_unknown_io
        )

        sample = items_sorted[-1] if items_sorted else {}
        summary.append(
            {
                "mount_point": mount_point,
                "device": sample.get("device"),
                "fs_type": sample.get("fs_type"),
                "total_bytes": sample.get("total_bytes"),
                "used_bytes": sample.get("used_bytes"),
                "used_percent": sample.get("used_percent"),
                "days_observed": len(items_sorted),
                "active_io_days": active_io_days,
                "open_files_days": open_files_days,
                "growth_bytes": growth,
                "status": "Inactive" if inactive else "Active",
                "unknown_io": any_unknown_io,
            }
        )
    return summary


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def print_report(rows: List[Dict[str, object]]) -> None:
    if not rows:
        print("No data available for report.")
        return
    header = (
        f"{'Mount':<30} {'Status':<8} {'Used%':>6} {'Growth(MB)':>12} "
        f"{'IO Days':>7} {'Open Days':>9} {'Observed':>9}"
    )
    print(header)
    print("-" * len(header))
    for row in sorted(rows, key=lambda r: str(r.get("mount_point"))):
        growth_mb = (row.get("growth_bytes", 0) or 0) / (1024 * 1024)
        print(
            f"{row.get('mount_point', ''):<30} {row.get('status', ''):<8} "
            f"{row.get('used_percent', 0):>6} {growth_mb:>12.2f} "
            f"{row.get('active_io_days', 0):>7} {row.get('open_files_days', 0):>9} "
            f"{row.get('days_observed', 0):>9}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Filesystem activity monitor")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("/var/log/fs_monitor"),
        help="Directory for logs and state",
    )
    parser.add_argument(
        "--exclude-mount",
        action="append",
        default=[],
        help="Mount point to exclude (repeatable)",
    )
    parser.add_argument(
        "--exclude-type",
        action="append",
        default=[],
        help="Filesystem type to exclude (repeatable)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="Collect daily metrics")
    collect_parser.add_argument(
        "--activity-threshold-sectors",
        type=int,
        default=2048,
        help="Minimum sectors read+written to count as active I/O",
    )

    report_parser = subparsers.add_parser("report", help="Generate activity report")
    report_parser.add_argument("--days", type=int, default=30)
    report_parser.add_argument(
        "--growth-threshold-bytes",
        type=int,
        default=0,
        help="Allowed growth (bytes) to still be considered inactive",
    )
    report_parser.add_argument("--csv", type=Path, help="Write CSV report")
    report_parser.add_argument("--json", type=Path, help="Write JSON report")

    args = parser.parse_args()

    exclude_mounts = DEFAULT_EXCLUDE_MOUNTS.union(args.exclude_mount)
    exclude_types = DEFAULT_EXCLUDE_TYPES.union(args.exclude_type)

    if args.command == "collect":
        collect(
            args.data_dir,
            exclude_mounts,
            exclude_types,
            args.activity_threshold_sectors,
        )
        return 0
    if args.command == "report":
        records = load_daily_files(args.data_dir / "daily", args.days)
        summary = aggregate(records, args.growth_threshold_bytes)
        print_report(summary)
        if args.csv:
            write_csv(args.csv, summary)
        if args.json:
            write_json(args.json, summary)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
