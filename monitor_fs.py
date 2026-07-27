#!/usr/bin/env python3
"""Filesystem activity monitor.

Collects daily filesystem usage/activity metrics on a Linux host and turns them
into a report that flags idle or over-provisioned filesystems, with an estimate
of the monthly storage spend each one represents.

The collector is designed to be cheap enough to run on production hosts:
no external binaries, a single pass over /proc, and self-imposed nice/ionice
priorities so it never competes with the workload it is measuring.

    monitor_fs.py collect
    monitor_fs.py report --days 30 --json report.json
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import ctypes
import datetime as dt
import json
import os
import platform
import shutil
import signal
import socket
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

__version__ = "2.0.0"

DEFAULT_DATA_DIR = Path("/var/log/fs_monitor")

DEFAULT_EXCLUDE_MOUNTS = {"/", "/boot", "/boot/efi", "/var", "/usr"}
DEFAULT_EXCLUDE_TYPES = {
    "proc",
    "sysfs",
    "devtmpfs",
    "tmpfs",
    "devpts",
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
    "binfmt_misc",
    "bpf",
    "nsfs",
    "ramfs",
    "efivarfs",
}

BYTES_PER_SECTOR = 512
BYTES_PER_GB = 1024**3

# Report verdicts.
STATUS_ACTIVE = "Active"
STATUS_INACTIVE = "Inactive"
STATUS_UNKNOWN = "Insufficient data"

ACTION_KEEP = "keep"
ACTION_RECLAIM = "reclaim"
ACTION_SHRINK = "shrink"
ACTION_WAIT = "wait"


@dataclass
class MountInfo:
    device: str
    mount_point: str
    fs_type: str


@dataclass
class DiskStats:
    read_sectors: int
    write_sectors: int


# --------------------------------------------------------------------------
# Low-overhead guarantees
# --------------------------------------------------------------------------


def lower_priority(nice_level: int = 10) -> dict[str, object]:
    """Drop CPU and I/O priority so collection cannot disturb the workload.

    Returns a dict describing what was applied; failures are non-fatal because
    the collector must never take a host down with it.
    """
    applied: dict[str, object] = {"nice": None, "ionice": False}
    try:
        os.nice(nice_level)
        applied["nice"] = os.nice(0)
    except OSError:
        pass

    # ioprio_set(IOPRIO_WHO_PROCESS=1, self=0, IOPRIO_CLASS_IDLE=3 << 13).
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        ioprio_class_idle = 3 << 13
        if libc.syscall(251, 1, 0, ioprio_class_idle) == 0:
            applied["ionice"] = True
    except (OSError, AttributeError):
        pass
    return applied


def load_average() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except (OSError, AttributeError):
        return None


# --------------------------------------------------------------------------
# Collection primitives
# --------------------------------------------------------------------------


def parse_mounts(mounts_path: str = "/proc/mounts") -> list[MountInfo]:
    mounts: list[MountInfo] = []
    seen: set = set()
    with open(mounts_path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mount_point, fs_type = parts[:3]
            mount_point = mount_point.replace("\\040", " ")
            if mount_point in seen:
                continue
            seen.add(mount_point)
            mounts.append(MountInfo(device=device, mount_point=mount_point, fs_type=fs_type))
    return mounts


def resolve_device_name(device: str) -> str | None:
    """Map a device path to the kernel name used in /proc/diskstats."""
    if not device.startswith("/dev/"):
        return None
    return os.path.basename(os.path.realpath(device))


def read_diskstats(diskstats_path: str = "/proc/diskstats") -> dict[str, DiskStats]:
    stats: dict[str, DiskStats] = {}
    with open(diskstats_path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 14:
                continue
            try:
                stats[parts[2]] = DiskStats(
                    read_sectors=int(parts[5]),
                    write_sectors=int(parts[9]),
                )
            except ValueError:
                continue
    return stats


def disk_usage_bytes(mount_point: str) -> tuple[int, int, int, float]:
    usage = shutil.disk_usage(mount_point)
    used_percent = (usage.used / usage.total) * 100 if usage.total else 0.0
    return usage.total, usage.used, usage.free, used_percent


def _match_mount(path: str, mount_points: Sequence[str]) -> str | None:
    """Return the longest mount point that contains ``path``."""
    for mount_point in mount_points:
        if path == mount_point or path.startswith(mount_point.rstrip(os.sep) + os.sep):
            return mount_point
    return None


def scan_open_files(mount_points: Iterable[str]) -> dict[str, bool]:
    """Report which mount points have a process holding something open.

    One pass over /proc covers every mount point at once, instead of shelling
    out to ``lsof`` once per filesystem. On a host with 400 processes and 12
    monitored filesystems that is one walk rather than twelve fork/exec cycles.
    """
    ordered = sorted({os.path.normpath(m) for m in mount_points}, key=len, reverse=True)
    result = dict.fromkeys(ordered, False)
    if not ordered:
        return result

    remaining = set(ordered)
    self_pid = str(os.getpid())

    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == self_pid or not remaining:
            continue
        proc_path = Path("/proc") / pid
        candidates: list[Path] = [proc_path / "cwd", proc_path / "root", proc_path / "exe"]
        fd_path = proc_path / "fd"
        with contextlib.suppress(OSError):
            candidates.extend(fd_path / fd for fd in os.listdir(fd_path))

        for link in candidates:
            try:
                target = os.path.realpath(link)
            except OSError:
                continue
            matched = _match_mount(target, ordered)
            if matched and not result[matched]:
                result[matched] = True
                remaining.discard(matched)
                if not remaining:
                    return result
    return result


def list_target_mounts(
    exclude_mounts: Iterable[str],
    exclude_types: Iterable[str],
    include_mounts: Iterable[str] | None = None,
) -> list[MountInfo]:
    excluded_mounts = {os.path.normpath(mount) for mount in exclude_mounts}
    excluded_types = set(exclude_types)
    included = {os.path.normpath(mount) for mount in include_mounts or []}

    mounts: list[MountInfo] = []
    for mount in parse_mounts():
        normalized = os.path.normpath(mount.mount_point)
        if included:
            if normalized in included:
                mounts.append(mount)
            continue
        if normalized in excluded_mounts or mount.fs_type in excluded_types:
            continue
        mounts.append(mount)
    return mounts


def load_state(state_path: Path) -> dict[str, dict[str, int]]:
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state_path: Path, state: dict[str, dict[str, int]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp_path.replace(state_path)


def collect(
    data_dir: Path,
    exclude_mounts: Iterable[str],
    exclude_types: Iterable[str],
    activity_threshold_sectors: int,
    include_mounts: Iterable[str] | None = None,
    skip_open_files: bool = False,
    retention_days: int = 400,
    host: str | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    mounts = list_target_mounts(exclude_mounts, exclude_types, include_mounts)
    diskstats = read_diskstats()
    hostname = host or socket.getfqdn() or platform.node()

    state_path = data_dir / "state.json"
    prev_state = load_state(state_path)
    today = dt.date.today().isoformat()
    daily_dir = data_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)

    if skip_open_files:
        open_files_map: dict[str, bool] = {}
    else:
        open_files_map = scan_open_files(mount.mount_point for mount in mounts)

    results: list[dict[str, object]] = []
    new_state: dict[str, dict[str, int]] = {}

    for mount in mounts:
        try:
            total, used, free, used_percent = disk_usage_bytes(mount.mount_point)
        except OSError:
            # Mount vanished or is unreachable (stale NFS handle, unmounted mid-run).
            continue

        device_name = resolve_device_name(mount.device)
        read_sectors = 0
        write_sectors = 0
        delta_read: int | None = None
        delta_write: int | None = None
        active_io: bool | None = None

        if device_name and device_name in diskstats:
            current = diskstats[device_name]
            read_sectors = current.read_sectors
            write_sectors = current.write_sectors
            prev = prev_state.get(device_name)
            if prev:
                # Counters reset on reboot; a negative delta means "unknown", not "idle".
                raw_read = read_sectors - int(prev.get("read_sectors", 0))
                raw_write = write_sectors - int(prev.get("write_sectors", 0))
                if raw_read < 0 or raw_write < 0:
                    active_io = None
                else:
                    delta_read = raw_read
                    delta_write = raw_write
                    active_io = (delta_read + delta_write) >= activity_threshold_sectors
            new_state[device_name] = {
                "read_sectors": read_sectors,
                "write_sectors": write_sectors,
            }

        normalized_mount = os.path.normpath(mount.mount_point)
        results.append(
            {
                "date": today,
                "host": hostname,
                "device": mount.device,
                "device_name": device_name,
                "mount_point": normalized_mount,
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
                "open_files": None if skip_open_files else open_files_map.get(normalized_mount, False),
            }
        )

    daily_path = daily_dir / f"{today}.json"
    daily_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    save_state(state_path, new_state)
    prune_daily_files(daily_dir, retention_days)

    meta = {
        "host": hostname,
        "date": today,
        "filesystems": len(results),
        "duration_seconds": round(time.monotonic() - started, 3),
        "loadavg": load_average(),
        "version": __version__,
    }
    (data_dir / "last_run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def prune_daily_files(daily_dir: Path, retention_days: int) -> int:
    """Keep the data directory bounded so the monitor never becomes the problem."""
    if retention_days <= 0 or not daily_dir.exists():
        return 0
    cutoff = dt.date.today() - dt.timedelta(days=retention_days)
    removed = 0
    for path in daily_dir.glob("*.json"):
        try:
            file_date = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def load_daily_files(daily_dir: Path, days: int) -> list[dict[str, object]]:
    if not daily_dir.exists():
        return []
    cutoff = dt.date.today() - dt.timedelta(days=days)
    records: list[dict[str, object]] = []
    for path in sorted(daily_dir.glob("*.json")):
        try:
            file_date = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list):
            records.extend(item for item in data if isinstance(item, dict))
    return records


def monthly_cost(total_bytes: int, cost_per_gb_month: float) -> float:
    return round((total_bytes / BYTES_PER_GB) * cost_per_gb_month, 2)


def recommend(
    status: str,
    total_bytes: int,
    used_bytes: int,
    used_percent: float,
    growth_bytes: int,
    cost_per_gb_month: float,
    shrink_below_percent: float,
    headroom_percent: float,
    min_saving_gb: float,
) -> tuple[str, int, float, str]:
    """Turn a verdict into an action, the bytes it frees and the money it saves."""
    if status == STATUS_UNKNOWN:
        return ACTION_WAIT, 0, 0.0, "Not enough observation days yet"

    if status == STATUS_INACTIVE:
        reclaimable = total_bytes
        saving = monthly_cost(reclaimable, cost_per_gb_month)
        if reclaimable / BYTES_PER_GB < min_saving_gb:
            return ACTION_KEEP, 0, 0.0, "Idle but too small to be worth reclaiming"
        return (
            ACTION_RECLAIM,
            reclaimable,
            saving,
            "No I/O, no open files and no growth over the window - archive and detach",
        )

    # Active, but paying for capacity nobody is using.
    if used_percent < shrink_below_percent:
        target = int(used_bytes * (1 + headroom_percent / 100))
        reclaimable = max(total_bytes - target, 0)
        saving = monthly_cost(reclaimable, cost_per_gb_month)
        if reclaimable / BYTES_PER_GB >= min_saving_gb:
            return (
                ACTION_SHRINK,
                reclaimable,
                saving,
                f"In use but only {used_percent:.0f}% full - resize with {headroom_percent:.0f}% headroom",
            )
    return ACTION_KEEP, 0, 0.0, "Active and appropriately sized"


def aggregate(
    records: list[dict[str, object]],
    growth_threshold_bytes: int = 0,
    min_days: int = 7,
    cost_per_gb_month: float = 0.08,
    shrink_below_percent: float = 40.0,
    headroom_percent: float = 30.0,
    min_saving_gb: float = 1.0,
) -> list[dict[str, object]]:
    by_key: dict[tuple[str, str], list[dict[str, object]]] = {}
    for record in records:
        key = (str(record.get("host", "")), str(record.get("mount_point")))
        by_key.setdefault(key, []).append(record)

    summary: list[dict[str, object]] = []
    for (host, mount_point), items in by_key.items():
        items_sorted = sorted(items, key=lambda item: str(item.get("date")))
        used_values = [int(item.get("used_bytes", 0) or 0) for item in items_sorted]
        growth = (max(used_values) - min(used_values)) if used_values else 0

        active_io_days = sum(1 for item in items_sorted if item.get("active_io") is True)
        open_files_days = sum(1 for item in items_sorted if item.get("open_files") is True)
        unknown_io_days = sum(1 for item in items_sorted if item.get("active_io") is None)
        days_observed = len(items_sorted)

        sample = items_sorted[-1]
        total_bytes = int(sample.get("total_bytes", 0) or 0)
        used_bytes = int(sample.get("used_bytes", 0) or 0)
        used_percent = float(sample.get("used_percent", 0) or 0)

        if days_observed < min_days or unknown_io_days == days_observed:
            status = STATUS_UNKNOWN
        elif active_io_days == 0 and open_files_days == 0 and growth <= growth_threshold_bytes:
            status = STATUS_INACTIVE
        else:
            status = STATUS_ACTIVE

        action, reclaimable, saving, rationale = recommend(
            status,
            total_bytes,
            used_bytes,
            used_percent,
            growth,
            cost_per_gb_month,
            shrink_below_percent,
            headroom_percent,
            min_saving_gb,
        )

        summary.append(
            {
                "host": host,
                "mount_point": mount_point,
                "device": sample.get("device"),
                "fs_type": sample.get("fs_type"),
                "total_bytes": total_bytes,
                "used_bytes": used_bytes,
                "used_percent": used_percent,
                "days_observed": days_observed,
                "active_io_days": active_io_days,
                "open_files_days": open_files_days,
                "unknown_io_days": unknown_io_days,
                "growth_bytes": growth,
                "status": status,
                "action": action,
                "reclaimable_bytes": reclaimable,
                "monthly_cost": monthly_cost(total_bytes, cost_per_gb_month),
                "monthly_saving": saving,
                "rationale": rationale,
            }
        )

    summary.sort(key=lambda row: (-float(row["monthly_saving"]), str(row["host"]), str(row["mount_point"])))
    return summary


def totals(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "hosts": len({str(row.get("host", "")) for row in rows}),
        "filesystems": len(rows),
        "provisioned_gb": round(sum(int(r.get("total_bytes", 0)) for r in rows) / BYTES_PER_GB, 2),
        "used_gb": round(sum(int(r.get("used_bytes", 0)) for r in rows) / BYTES_PER_GB, 2),
        "reclaimable_gb": round(sum(int(r.get("reclaimable_bytes", 0)) for r in rows) / BYTES_PER_GB, 2),
        "monthly_cost": round(sum(float(r.get("monthly_cost", 0)) for r in rows), 2),
        "monthly_saving": round(sum(float(r.get("monthly_saving", 0)) for r in rows), 2),
        "annual_saving": round(sum(float(r.get("monthly_saving", 0)) for r in rows) * 12, 2),
        "idle": sum(1 for r in rows if r.get("status") == STATUS_INACTIVE),
        "oversized": sum(1 for r in rows if r.get("action") == ACTION_SHRINK),
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, rows: list[dict[str, object]], summary: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": __version__,
        "totals": summary,
        "filesystems": rows,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def human_gb(num_bytes: object) -> str:
    return f"{int(num_bytes or 0) / BYTES_PER_GB:,.1f}"


def print_report(rows: list[dict[str, object]], currency: str = "USD") -> None:
    if not rows:
        print("No data available for report. Run 'collect' for at least a few days first.")
        return

    header = (
        f"{'Host':<20} {'Mount':<24} {'Status':<18} {'Used%':>6} "
        f"{'Size(GB)':>9} {'Reclaim(GB)':>12} {'Action':<8} {'Save/mo':>9}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        reclaim_gb = int(row.get("reclaimable_bytes", 0)) / BYTES_PER_GB
        print(
            f"{str(row.get('host', ''))[:20]:<20} "
            f"{str(row.get('mount_point', ''))[:24]:<24} "
            f"{str(row.get('status', '')):<18} "
            f"{float(row.get('used_percent', 0)):>6.1f} "
            f"{human_gb(row.get('total_bytes')):>9} "
            f"{reclaim_gb:>12,.1f} "
            f"{str(row.get('action', '')):<8} "
            f"{float(row.get('monthly_saving', 0)):>9,.2f}"
        )

    summary = totals(rows)
    print("-" * len(header))
    print(
        f"{summary['filesystems']} filesystems on {summary['hosts']} host(s) | "
        f"provisioned {summary['provisioned_gb']:,.1f} GB, used {summary['used_gb']:,.1f} GB"
    )
    print(
        f"Idle: {summary['idle']} | Oversized: {summary['oversized']} | "
        f"Reclaimable: {summary['reclaimable_gb']:,.1f} GB"
    )
    print(
        f"Current spend {summary['monthly_cost']:,.2f} {currency}/mo -> "
        f"potential saving {summary['monthly_saving']:,.2f} {currency}/mo "
        f"({summary['annual_saving']:,.2f} {currency}/yr)"
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Filesystem activity and storage-cost monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"monitor_fs {__version__}")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Directory for logs and state")
    parser.add_argument("--exclude-mount", action="append", default=[], help="Mount point to exclude (repeatable)")
    parser.add_argument("--exclude-type", action="append", default=[], help="Filesystem type to exclude (repeatable)")
    parser.add_argument(
        "--include-mount",
        action="append",
        default=[],
        help="Only monitor these mount points (repeatable; overrides exclusions)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="Collect one day of metrics")
    collect_parser.add_argument(
        "--activity-threshold-sectors",
        type=int,
        default=2048,
        help="Sectors read+written since last run before a filesystem counts as active (512B sectors)",
    )
    collect_parser.add_argument("--host", help="Override the recorded hostname")
    collect_parser.add_argument(
        "--skip-open-files",
        action="store_true",
        help="Skip the /proc scan for open files (lowest possible overhead)",
    )
    collect_parser.add_argument("--nice", type=int, default=10, help="Nice level to run collection at")
    collect_parser.add_argument("--no-renice", action="store_true", help="Do not lower CPU/IO priority")
    collect_parser.add_argument("--retention-days", type=int, default=400, help="Delete daily files older than this")
    collect_parser.add_argument("--quiet", action="store_true", help="Suppress the run summary line")

    report_parser = subparsers.add_parser("report", help="Generate an activity and cost report")
    report_parser.add_argument("--days", type=int, default=30, help="Observation window")
    report_parser.add_argument(
        "--min-days", type=int, default=7, help="Days of data required before judging a filesystem"
    )
    report_parser.add_argument(
        "--growth-threshold-bytes",
        type=int,
        default=0,
        help="Growth allowed while still counting as inactive",
    )
    report_parser.add_argument(
        "--cost-per-gb-month", type=float, default=0.08, help="Blended storage price per GB-month"
    )
    report_parser.add_argument("--currency", default="USD", help="Currency label for the report")
    report_parser.add_argument(
        "--shrink-below-percent",
        type=float,
        default=40.0,
        help="Active filesystems under this fill level are shrink candidates",
    )
    report_parser.add_argument(
        "--headroom-percent", type=float, default=30.0, help="Headroom kept when proposing a shrink"
    )
    report_parser.add_argument("--min-saving-gb", type=float, default=1.0, help="Ignore savings smaller than this")
    report_parser.add_argument("--csv", type=Path, help="Write CSV report")
    report_parser.add_argument("--json", type=Path, help="Write JSON report")
    report_parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Exit 2 when reclaimable capacity is found (useful in CI)",
    )
    return parser


def allow_sigpipe() -> None:
    """Exit quietly when piped into `head`, instead of dumping a traceback."""
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def main(argv: Sequence[str] | None = None) -> int:
    allow_sigpipe()
    parser = build_parser()
    args = parser.parse_args(argv)

    exclude_mounts = DEFAULT_EXCLUDE_MOUNTS.union(args.exclude_mount)
    exclude_types = DEFAULT_EXCLUDE_TYPES.union(args.exclude_type)

    if args.command == "collect":
        if not args.no_renice:
            lower_priority(args.nice)
        try:
            meta = collect(
                args.data_dir,
                exclude_mounts,
                exclude_types,
                args.activity_threshold_sectors,
                include_mounts=args.include_mount,
                skip_open_files=args.skip_open_files,
                retention_days=args.retention_days,
                host=args.host,
            )
        except PermissionError as exc:
            print(f"error: {exc} (try running as root or pass --data-dir)", file=sys.stderr)
            return 1
        if not args.quiet:
            print(
                f"{meta['host']}: collected {meta['filesystems']} filesystems "
                f"in {meta['duration_seconds']}s -> {args.data_dir}"
            )
        return 0

    if args.command == "report":
        records = load_daily_files(args.data_dir / "daily", args.days)
        summary = aggregate(
            records,
            growth_threshold_bytes=args.growth_threshold_bytes,
            min_days=args.min_days,
            cost_per_gb_month=args.cost_per_gb_month,
            shrink_below_percent=args.shrink_below_percent,
            headroom_percent=args.headroom_percent,
            min_saving_gb=args.min_saving_gb,
        )
        print_report(summary, args.currency)
        if args.csv:
            write_csv(args.csv, summary)
        if args.json:
            write_json(args.json, summary, totals(summary))
        if args.fail_on_findings and any(row.get("reclaimable_bytes") for row in summary):
            return 2
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
