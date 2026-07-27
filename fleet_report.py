#!/usr/bin/env python3
"""Fleet roll-up for monitor_fs.

Takes the per-host JSON reports gathered by the Ansible ``collect`` play and
merges them into one view: which hosts hold idle or over-provisioned storage,
and what the fleet is paying for it every month.

    ./fleet_report.py reports/ --markdown FLEET.md --csv fleet.csv
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from monitor_fs import BYTES_PER_GB, __version__, allow_sigpipe, totals, write_csv


def load_reports(paths: Sequence[Path]) -> list[dict[str, object]]:
    """Read host report files; accepts both the report payload and a bare list."""
    rows: list[dict[str, object]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skipping {path}: {exc}", file=sys.stderr)
            continue

        if isinstance(data, dict):
            filesystems = data.get("filesystems", [])
        elif isinstance(data, list):
            filesystems = data
        else:
            continue

        fallback_host = path.stem
        for row in filesystems:
            if not isinstance(row, dict):
                continue
            if not row.get("host"):
                row["host"] = fallback_host
            rows.append(row)
    return rows


# Outputs of this script, which live next to the inputs it reads. Ingesting them
# would count the whole fleet a second time on every rerun.
OUTPUT_NAMES = {"fleet.json", "fleet.csv", "FLEET.md"}


def discover(inputs: Sequence[Path]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        if item.is_dir():
            paths.extend(
                path for path in sorted(item.glob("*.json")) if path.name not in OUTPUT_NAMES
            )
        elif item.exists():
            paths.append(item)
        else:
            print(f"warning: {item} not found", file=sys.stderr)
    return paths


def by_host(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("host", "unknown")), []).append(row)

    hosts = []
    for host, host_rows in grouped.items():
        host_totals = totals(host_rows)
        host_totals["host"] = host
        hosts.append(host_totals)
    hosts.sort(key=lambda item: -float(item["monthly_saving"]))
    return hosts


def print_fleet(rows: list[dict[str, object]], hosts: list[dict[str, object]], currency: str, top: int) -> None:
    summary = totals(rows)
    print(f"Fleet storage report - {summary['hosts']} hosts, {summary['filesystems']} filesystems")
    print("=" * 78)
    print(f"Provisioned : {summary['provisioned_gb']:>12,.1f} GB")
    print(f"Actually used: {summary['used_gb']:>11,.1f} GB")
    print(f"Reclaimable : {summary['reclaimable_gb']:>12,.1f} GB")
    print(f"Spend       : {summary['monthly_cost']:>12,.2f} {currency}/mo")
    print(
        f"Saving      : {summary['monthly_saving']:>12,.2f} {currency}/mo  "
        f"({summary['annual_saving']:,.2f} {currency}/yr)"
    )
    print()

    print(f"{'Host':<28} {'FS':>4} {'Prov(GB)':>10} {'Reclaim(GB)':>12} {'Save/mo':>10}")
    print("-" * 78)
    for host in hosts[:top]:
        print(
            f"{str(host['host'])[:28]:<28} {host['filesystems']:>4} "
            f"{host['provisioned_gb']:>10,.1f} {host['reclaimable_gb']:>12,.1f} "
            f"{host['monthly_saving']:>10,.2f}"
        )

    candidates = [row for row in rows if row.get("reclaimable_bytes")]
    if candidates:
        print()
        print(f"Top {min(top, len(candidates))} filesystems by saving")
        print("-" * 78)
        for row in candidates[:top]:
            reclaim_gb = int(row.get("reclaimable_bytes", 0)) / BYTES_PER_GB
            print(
                f"{str(row.get('host'))[:24]:<24} {str(row.get('mount_point'))[:24]:<24} "
                f"{str(row.get('action')):<8} {reclaim_gb:>9,.1f} GB "
                f"{float(row.get('monthly_saving', 0)):>9,.2f} {currency}/mo"
            )


def render_markdown(rows: list[dict[str, object]], hosts: list[dict[str, object]], currency: str, top: int) -> str:
    summary = totals(rows)
    generated = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        "# Fleet storage report",
        "",
        f"_Generated {generated} by monitor_fs {__version__}_",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Hosts | {summary['hosts']} |",
        f"| Filesystems | {summary['filesystems']} |",
        f"| Provisioned | {summary['provisioned_gb']:,.1f} GB |",
        f"| Used | {summary['used_gb']:,.1f} GB |",
        f"| Idle filesystems | {summary['idle']} |",
        f"| Over-provisioned filesystems | {summary['oversized']} |",
        f"| Reclaimable | {summary['reclaimable_gb']:,.1f} GB |",
        f"| Current spend | {summary['monthly_cost']:,.2f} {currency}/mo |",
        f"| **Potential saving** | **{summary['monthly_saving']:,.2f} {currency}/mo "
        f"({summary['annual_saving']:,.2f} {currency}/yr)** |",
        "",
        "## Savings by host",
        "",
        "| Host | Filesystems | Provisioned (GB) | Reclaimable (GB) | Saving/mo |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for host in hosts:
        lines.append(
            f"| {host['host']} | {host['filesystems']} | {host['provisioned_gb']:,.1f} | "
            f"{host['reclaimable_gb']:,.1f} | {host['monthly_saving']:,.2f} {currency} |"
        )

    candidates = [row for row in rows if row.get("reclaimable_bytes")]
    lines += [
        "",
        "## Action list",
        "",
        "| Host | Mount | Status | Used % | Size (GB) | Action | Saving/mo | Why |",
        "| --- | --- | --- | ---: | ---: | --- | ---: | --- |",
    ]
    if not candidates:
        lines.append("| - | - | - | - | - | - | - | Nothing to reclaim |")
    for row in candidates[:top]:
        lines.append(
            f"| {row.get('host')} | `{row.get('mount_point')}` | {row.get('status')} | "
            f"{float(row.get('used_percent', 0)):.1f} | {int(row.get('total_bytes', 0)) / BYTES_PER_GB:,.1f} | "
            f"{row.get('action')} | {float(row.get('monthly_saving', 0)):,.2f} {currency} | {row.get('rationale')} |"
        )

    lines += [
        "",
        "> `reclaim` = idle for the whole window (no I/O, no open files, no growth): archive and detach.",
        "> `shrink` = in use but far larger than it needs to be: resize with headroom.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    allow_sigpipe()
    parser = argparse.ArgumentParser(
        description="Merge per-host monitor_fs reports into a fleet-wide storage cost report",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="Report files or directories containing *.json")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--top", type=int, default=20, help="Rows to show in the action list")
    parser.add_argument("--markdown", type=Path, help="Write a Markdown report")
    parser.add_argument("--csv", type=Path, help="Write the merged rows as CSV")
    parser.add_argument("--json", type=Path, help="Write the merged rows as JSON")
    parser.add_argument("--quiet", action="store_true", help="Do not print to stdout")
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Exit 2 when the fleet has reclaimable capacity",
    )
    args = parser.parse_args(argv)

    paths = discover(args.inputs)
    if not paths:
        print("error: no report files found", file=sys.stderr)
        return 1

    rows = load_reports(paths)
    if not rows:
        # A fleet where every host's storage is excluded, or that has not built
        # up history yet, is a legitimate state - report it and still produce
        # the artifacts downstream jobs expect, rather than failing the run.
        print(
            f"warning: {len(paths)} report(s) contained no filesystem rows - "
            "nothing is being monitored yet",
            file=sys.stderr,
        )

    rows.sort(key=lambda row: -float(row.get("monthly_saving", 0) or 0))
    hosts = by_host(rows)

    if not args.quiet:
        print_fleet(rows, hosts, args.currency, args.top)

    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(rows, hosts, args.currency, args.top), encoding="utf-8")
    if args.csv:
        write_csv(args.csv, rows)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "totals": totals(rows),
                    "hosts": hosts,
                    "filesystems": rows,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    if args.fail_on_findings and any(row.get("reclaimable_bytes") for row in rows):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
