#!/usr/bin/env python3
"""Unit tests for the collector and the report logic.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fleet_report  # noqa: E402
import monitor_fs  # noqa: E402

GB = monitor_fs.BYTES_PER_GB


def record(**overrides):
    base = {
        "date": "2026-01-01",
        "host": "host-a",
        "device": "/dev/sdb1",
        "mount_point": "/data",
        "fs_type": "xfs",
        "total_bytes": 100 * GB,
        "used_bytes": 50 * GB,
        "used_percent": 50.0,
        "active_io": True,
        "open_files": True,
    }
    base.update(overrides)
    return base


def series(days, **overrides):
    start = dt.date(2026, 1, 1)
    return [
        record(date=(start + dt.timedelta(days=offset)).isoformat(), **overrides)
        for offset in range(days)
    ]


class ParsingTests(unittest.TestCase):
    def test_parse_mounts_skips_short_lines_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mounts"
            path.write_text(
                "/dev/sda1 / ext4 rw 0 0\n"
                "garbage\n"
                "/dev/sdb1 /data xfs rw 0 0\n"
                "/dev/sdb1 /data xfs rw 0 0\n"
                "/dev/sdc1 /mnt/my\\040disk xfs rw 0 0\n",
                encoding="utf-8",
            )
            mounts = monitor_fs.parse_mounts(str(path))

        self.assertEqual([m.mount_point for m in mounts], ["/", "/data", "/mnt/my disk"])
        self.assertEqual(mounts[1].fs_type, "xfs")

    def test_read_diskstats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "diskstats"
            path.write_text(
                "   8       0 sda 100 0 4096 0 200 0 8192 0 0 0 0 0 0 0 0\n"
                "   8       1 truncated 1 2 3\n",
                encoding="utf-8",
            )
            stats = monitor_fs.read_diskstats(str(path))

        self.assertEqual(set(stats), {"sda"})
        self.assertEqual(stats["sda"].read_sectors, 4096)
        self.assertEqual(stats["sda"].write_sectors, 8192)

    def test_match_mount_prefers_longest_mount(self):
        mounts = sorted(["/", "/data", "/data/logs"], key=len, reverse=True)
        self.assertEqual(monitor_fs._match_mount("/data/logs/app.log", mounts), "/data/logs")
        self.assertEqual(monitor_fs._match_mount("/data/file", mounts), "/data")
        self.assertEqual(monitor_fs._match_mount("/etc/hosts", mounts), "/")

    def test_match_mount_does_not_match_sibling_prefix(self):
        # /database must not be attributed to /data.
        mounts = sorted(["/data", "/database"], key=len, reverse=True)
        self.assertEqual(monitor_fs._match_mount("/database/x", mounts), "/database")

    def test_list_target_mounts_include_overrides_exclusions(self):
        mounts = monitor_fs.list_target_mounts(
            monitor_fs.DEFAULT_EXCLUDE_MOUNTS,
            monitor_fs.DEFAULT_EXCLUDE_TYPES,
            include_mounts=["/"],
        )
        self.assertEqual([m.mount_point for m in mounts], ["/"])


class AggregateTests(unittest.TestCase):
    def test_idle_filesystem_is_flagged_for_reclaim(self):
        rows = monitor_fs.aggregate(
            series(30, active_io=False, open_files=False),
            cost_per_gb_month=0.10,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], monitor_fs.STATUS_INACTIVE)
        self.assertEqual(rows[0]["action"], monitor_fs.ACTION_RECLAIM)
        self.assertEqual(rows[0]["reclaimable_bytes"], 100 * GB)
        self.assertAlmostEqual(rows[0]["monthly_saving"], 10.0)

    def test_short_history_is_not_judged(self):
        rows = monitor_fs.aggregate(series(3, active_io=False, open_files=False), min_days=7)
        self.assertEqual(rows[0]["status"], monitor_fs.STATUS_UNKNOWN)
        self.assertEqual(rows[0]["action"], monitor_fs.ACTION_WAIT)
        self.assertEqual(rows[0]["reclaimable_bytes"], 0)

    def test_open_files_keep_a_quiet_filesystem_active(self):
        rows = monitor_fs.aggregate(series(30, active_io=False, open_files=True))
        self.assertEqual(rows[0]["status"], monitor_fs.STATUS_ACTIVE)

    def test_growth_beyond_threshold_keeps_it_active(self):
        records = series(30, active_io=False, open_files=False)
        records[-1]["used_bytes"] = 60 * GB
        rows = monitor_fs.aggregate(records, growth_threshold_bytes=0)
        self.assertEqual(rows[0]["status"], monitor_fs.STATUS_ACTIVE)

    def test_unknown_io_only_history_is_not_judged(self):
        rows = monitor_fs.aggregate(series(30, active_io=None, open_files=False))
        self.assertEqual(rows[0]["status"], monitor_fs.STATUS_UNKNOWN)

    def test_oversized_active_filesystem_is_a_shrink_candidate(self):
        rows = monitor_fs.aggregate(
            series(30, used_bytes=10 * GB, used_percent=10.0),
            cost_per_gb_month=0.10,
            shrink_below_percent=40,
            headroom_percent=30,
        )
        self.assertEqual(rows[0]["action"], monitor_fs.ACTION_SHRINK)
        # 100 GB provisioned, 10 GB used, target 13 GB -> 87 GB reclaimable.
        self.assertEqual(rows[0]["reclaimable_bytes"], 87 * GB)
        self.assertAlmostEqual(rows[0]["monthly_saving"], 8.7)

    def test_well_used_filesystem_is_left_alone(self):
        rows = monitor_fs.aggregate(series(30, used_bytes=80 * GB, used_percent=80.0))
        self.assertEqual(rows[0]["action"], monitor_fs.ACTION_KEEP)
        self.assertEqual(rows[0]["monthly_saving"], 0.0)

    def test_tiny_savings_are_ignored(self):
        rows = monitor_fs.aggregate(
            series(30, total_bytes=GB // 2, used_bytes=0, used_percent=0.0,
                   active_io=False, open_files=False),
            min_saving_gb=1.0,
        )
        self.assertEqual(rows[0]["action"], monitor_fs.ACTION_KEEP)

    def test_same_mount_on_different_hosts_stays_separate(self):
        records = series(30, host="host-a") + series(30, host="host-b", active_io=False, open_files=False)
        rows = monitor_fs.aggregate(records)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["host"] for row in rows}, {"host-a", "host-b"})

    def test_totals_roll_up_cost(self):
        rows = monitor_fs.aggregate(
            series(30, active_io=False, open_files=False),
            cost_per_gb_month=0.10,
        )
        summary = monitor_fs.totals(rows)
        self.assertEqual(summary["hosts"], 1)
        self.assertEqual(summary["reclaimable_gb"], 100.0)
        self.assertAlmostEqual(summary["annual_saving"], 120.0)


class CollectTests(unittest.TestCase):
    def test_collect_writes_daily_file_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            meta = monitor_fs.collect(
                data_dir,
                monitor_fs.DEFAULT_EXCLUDE_MOUNTS,
                monitor_fs.DEFAULT_EXCLUDE_TYPES,
                activity_threshold_sectors=2048,
                include_mounts=["/"],
                skip_open_files=True,
                host="test-host",
            )

            today = dt.date.today().isoformat()
            daily = json.loads((data_dir / "daily" / f"{today}.json").read_text(encoding="utf-8"))

        self.assertEqual(meta["host"], "test-host")
        self.assertEqual(meta["filesystems"], 1)
        self.assertEqual(daily[0]["mount_point"], "/")
        self.assertEqual(daily[0]["host"], "test-host")
        self.assertGreater(daily[0]["total_bytes"], 0)
        # First run has no baseline to compare against.
        self.assertIsNone(daily[0]["active_io"])
        self.assertIsNone(daily[0]["open_files"])

    def test_counter_reset_reports_unknown_not_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            monitor_fs.save_state(
                data_dir / "state.json",
                {name: {"read_sectors": 10**12, "write_sectors": 10**12} for name in monitor_fs.read_diskstats()},
            )
            monitor_fs.collect(
                data_dir,
                monitor_fs.DEFAULT_EXCLUDE_MOUNTS,
                monitor_fs.DEFAULT_EXCLUDE_TYPES,
                activity_threshold_sectors=2048,
                include_mounts=["/"],
                skip_open_files=True,
                host="test-host",
            )
            today = dt.date.today().isoformat()
            daily = json.loads((data_dir / "daily" / f"{today}.json").read_text(encoding="utf-8"))

        # A rebooted host must never look idle just because counters went backwards.
        self.assertIsNone(daily[0]["active_io"])

    def test_load_state_survives_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(monitor_fs.load_state(path), {})

    def test_prune_removes_only_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp)
            today = dt.date.today()
            fresh = daily / f"{today.isoformat()}.json"
            stale = daily / f"{(today - dt.timedelta(days=500)).isoformat()}.json"
            junk = daily / "notadate.json"
            for path in (fresh, stale, junk):
                path.write_text("[]", encoding="utf-8")

            removed = monitor_fs.prune_daily_files(daily, retention_days=400)

            self.assertEqual(removed, 1)
            self.assertTrue(fresh.exists())
            self.assertFalse(stale.exists())
            self.assertTrue(junk.exists())

    def test_load_daily_files_respects_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp)
            today = dt.date.today()
            (daily / f"{today.isoformat()}.json").write_text(json.dumps([record()]), encoding="utf-8")
            (daily / f"{(today - dt.timedelta(days=90)).isoformat()}.json").write_text(
                json.dumps([record()]), encoding="utf-8"
            )
            self.assertEqual(len(monitor_fs.load_daily_files(daily, days=30)), 1)
            self.assertEqual(len(monitor_fs.load_daily_files(daily, days=180)), 2)


class CliTests(unittest.TestCase):
    def test_report_writes_csv_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            daily_dir = data_dir / "daily"
            daily_dir.mkdir(parents=True)
            today = dt.date.today()
            for offset in range(10):
                day = today - dt.timedelta(days=offset)
                records = [record(date=day.isoformat(), active_io=False, open_files=False)]
                (daily_dir / f"{day.isoformat()}.json").write_text(json.dumps(records), encoding="utf-8")

            csv_path = Path(tmp) / "out.csv"
            json_path = Path(tmp) / "out.json"
            code = monitor_fs.main(
                [
                    "--data-dir", str(data_dir),
                    "report", "--days", "30",
                    "--csv", str(csv_path),
                    "--json", str(json_path),
                    "--fail-on-findings",
                ]
            )
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            csv_text = csv_path.read_text(encoding="utf-8")

        self.assertEqual(code, 2)  # findings present
        self.assertIn("mount_point", csv_text)
        self.assertEqual(payload["totals"]["filesystems"], 1)
        self.assertEqual(payload["filesystems"][0]["action"], monitor_fs.ACTION_RECLAIM)

    def test_report_with_no_data_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(monitor_fs.main(["--data-dir", tmp, "report"]), 0)


class FleetReportTests(unittest.TestCase):
    def _write_host_report(self, directory: Path, host: str, rows) -> Path:
        path = directory / f"{host}.json"
        path.write_text(
            json.dumps({"totals": monitor_fs.totals(rows), "filesystems": rows}, indent=2),
            encoding="utf-8",
        )
        return path

    def test_merge_hosts_and_render_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            reports.mkdir()
            for host in ("host-a", "host-b"):
                rows = monitor_fs.aggregate(
                    series(30, host=host, active_io=False, open_files=False),
                    cost_per_gb_month=0.10,
                )
                self._write_host_report(reports, host, rows)

            markdown = Path(tmp) / "FLEET.md"
            merged = Path(tmp) / "fleet.json"
            code = fleet_report.main(
                [str(reports), "--markdown", str(markdown), "--json", str(merged),
                 "--quiet", "--fail-on-findings"]
            )
            payload = json.loads(merged.read_text(encoding="utf-8"))
            text = markdown.read_text(encoding="utf-8")

        self.assertEqual(code, 2)
        self.assertEqual(payload["totals"]["hosts"], 2)
        self.assertAlmostEqual(payload["totals"]["monthly_saving"], 20.0)
        self.assertAlmostEqual(payload["totals"]["annual_saving"], 240.0)
        self.assertIn("Fleet storage report", text)
        self.assertIn("host-a", text)
        self.assertIn("host-b", text)

    def test_missing_host_field_falls_back_to_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            reports.mkdir()
            (reports / "db-01.example.com.json").write_text(
                json.dumps([{"mount_point": "/data", "total_bytes": GB, "used_bytes": 0,
                             "reclaimable_bytes": GB, "monthly_saving": 1.0, "monthly_cost": 1.0}]),
                encoding="utf-8",
            )
            rows = fleet_report.load_reports(fleet_report.discover([reports]))

        self.assertEqual(rows[0]["host"], "db-01.example.com")

    def test_corrupt_report_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            reports.mkdir()
            (reports / "broken.json").write_text("{oops", encoding="utf-8")
            good = monitor_fs.aggregate(series(30, host="ok", active_io=False, open_files=False))
            self._write_host_report(reports, "ok", good)

            rows = fleet_report.load_reports(fleet_report.discover([reports]))

        self.assertEqual({row["host"] for row in rows}, {"ok"})

    def test_previous_fleet_output_is_not_reingested(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            reports.mkdir()
            rows = monitor_fs.aggregate(
                series(30, host="host-a", active_io=False, open_files=False),
                cost_per_gb_month=0.10,
            )
            self._write_host_report(reports, "host-a", rows)

            args = [str(reports), "--json", str(reports / "fleet.json"),
                    "--csv", str(reports / "fleet.csv"),
                    "--markdown", str(reports / "FLEET.md"), "--quiet"]
            fleet_report.main(args)
            fleet_report.main(args)  # rerun over a directory that now holds outputs
            payload = json.loads((reports / "fleet.json").read_text(encoding="utf-8"))

        # Still one host, one filesystem - the roll-up did not eat its own output.
        self.assertEqual(payload["totals"]["hosts"], 1)
        self.assertEqual(payload["totals"]["filesystems"], 1)
        self.assertAlmostEqual(payload["totals"]["monthly_saving"], 10.0)


if __name__ == "__main__":
    unittest.main()
