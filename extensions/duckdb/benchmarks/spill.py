"""Compare complete MapPLUTO passes at different batch sizes with spill telemetry."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import resource
import subprocess
import sys
import threading
import time

import duckdb


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
        1024**2 if platform.system() == "Darwin" else 1024
    )


class SpillMonitor:
    """Sample logical spill-file bytes; profiler high-water metrics are authoritative."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.peak_bytes = 0
        self.samples = 0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.poll, daemon=True)

    def sample(self) -> None:
        total = 0
        for path in self.directory.glob("*"):
            try:
                total += path.stat().st_size if path.is_file() else 0
            except FileNotFoundError:
                pass  # DuckDB can remove a spill file between enumeration and stat.
        self.peak_bytes = max(self.peak_bytes, total)
        self.samples += 1

    def poll(self) -> None:
        while not self.stop_event.is_set():
            self.sample()
            self.stop_event.wait(0.25)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join()
        self.sample()


def worker(args: argparse.Namespace) -> int:
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    spill_directory = out / "spill"
    spill_directory.mkdir()
    monitor = SpillMonitor(spill_directory)
    summary = {
        "status": "running",
        "batch_size": args.batch_size,
        "duckdb": duckdb.__version__,
        "memory_limit": args.memory_limit,
        "max_temp_directory_size": args.max_spill,
        "threads": args.threads,
        "rows_completed": 0,
        "batches": [],
        "profile_peak_buffer_bytes": 0,
        "profile_peak_spill_bytes": 0,
    }

    def save() -> None:
        summary["peak_process_rss_mib"] = peak_rss_mib()
        summary["sampled_peak_spill_bytes"] = monitor.peak_bytes
        summary["spill_samples"] = monitor.samples
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    con = duckdb.connect(
        str(out / "output.duckdb"),
        config={
            "threads": args.threads,
            "memory_limit": args.memory_limit,
            "temp_directory": str(spill_directory),
            "max_temp_directory_size": args.max_spill,
            "allow_unsigned_extensions": "true",
        },
    )
    stage = "setup"
    started = time.perf_counter()
    matching_start = None
    try:
        binary = str(args.extension.resolve()).replace("'", "''")
        database = str(args.database.resolve()).replace("'", "''")
        con.execute(f"LOAD '{binary}'")
        con.execute(f"ATTACH '{database}' AS baseline (READ_ONLY)")
        total = con.execute("SELECT count(*) FROM baseline.source").fetchone()[0]
        summary["input_rows"] = total
        summary["reference_rows"] = con.execute(
            "SELECT count(*) FROM baseline.wa_reference"
        ).fetchone()[0]
        con.execute("CREATE TABLE results AS SELECT * FROM baseline.results LIMIT 0")
        profile_path = out / "last-query-profile.json"
        profile_literal = str(profile_path).replace("'", "''")
        con.execute(f"SET profiling_output='{profile_literal}'")
        summary["setup_seconds"] = time.perf_counter() - started
        save()
        monitor.start()
        stage = "matching"
        matching_start = time.perf_counter()
        last_progress = matching_start
        for first in range(1, total + 1, args.batch_size):
            last = min(first + args.batch_size - 1, total)
            summary["active_batch"] = {"first": first, "last": last}
            tick = time.perf_counter()
            con.execute(
                """CREATE OR REPLACE TEMP TABLE input_addresses AS
                SELECT address_id,address FROM baseline.source WHERE seq BETWEEN ? AND ?""",
                [first, last],
            )
            input_seconds = time.perf_counter() - tick
            con.execute("SET enable_profiling='json'")
            tick = time.perf_counter()
            con.execute("""INSERT INTO results SELECT * FROM wa_geocode(
                'input_addresses','baseline.wa_reference','baseline.wa_index')""")
            query_seconds = time.perf_counter() - tick
            con.execute("PRAGMA disable_profiling")
            profile = json.loads(profile_path.read_text())
            peak_buffer = profile["system_peak_buffer_memory"]
            peak_spill = profile["system_peak_temp_dir_size"]
            summary["profile_peak_buffer_bytes"] = max(
                summary["profile_peak_buffer_bytes"], peak_buffer
            )
            summary["profile_peak_spill_bytes"] = max(
                summary["profile_peak_spill_bytes"], peak_spill
            )
            summary["batches"].append(
                {
                    "first": first,
                    "last": last,
                    "rows": last - first + 1,
                    "input_seconds": input_seconds,
                    "query_and_insert_seconds": query_seconds,
                    "profile_peak_buffer_bytes": peak_buffer,
                    "profile_peak_spill_bytes": peak_spill,
                }
            )
            summary["rows_completed"] = last
            elapsed = time.perf_counter() - matching_start
            if first == 1 or last == total or time.perf_counter() - last_progress >= 30:
                print(
                    f"batch={args.batch_size}: {last:,}/{total:,} rows, {elapsed:.1f}s, "
                    f"{last / elapsed:.1f} rows/s, spill peak {max(peak_spill, monitor.peak_bytes) / 1024**2:.1f} MiB, "
                    f"RSS peak {peak_rss_mib():.0f} MiB",
                    flush=True,
                )
                save()
                last_progress = time.perf_counter()
        summary["matching_seconds"] = time.perf_counter() - matching_start
        summary["query_and_insert_seconds"] = sum(
            b["query_and_insert_seconds"] for b in summary["batches"]
        )
        summary["rows_per_second"] = total / summary["matching_seconds"]
        monitor.stop()
        summary["matching_peak_process_rss_mib"] = peak_rss_mib()
        stage = "validation"
        tick = time.perf_counter()
        count, unique_ids = con.execute(
            "SELECT count(*),count(DISTINCT address_id) FROM results"
        ).fetchone()
        if (count, unique_ids) != (total, total):
            raise AssertionError("Result count or unique IDs differ from full input")
        differences = con.execute("""SELECT count(*) FROM results r FULL JOIN baseline.results b USING(address_id)
            WHERE r.address_id IS DISTINCT FROM b.address_id OR
            (r.input_address,r.reference_id,r.matched_address,r.latitude,r.longitude,r.similarity,r.match_rank)
            IS DISTINCT FROM
            (b.input_address,b.reference_id,b.matched_address,b.latitude,b.longitude,b.similarity,b.match_rank)""").fetchone()[
            0
        ]
        summary["rows_different_from_baseline"] = differences
        if differences:
            raise AssertionError(f"{differences} result rows differ from baseline")
        con.execute("CHECKPOINT")
        summary["validation_and_checkpoint_seconds"] = time.perf_counter() - tick
        summary["status"] = "ok"
        summary.pop("active_batch", None)
        return 0
    except Exception as exc:
        summary.update(
            status="error",
            failed_stage=stage,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        if stage == "matching" and matching_start is not None:
            summary["matching_seconds_before_error"] = (
                time.perf_counter() - matching_start
            )
        print(
            f"batch={args.batch_size}: FAILED in {stage}: {type(exc).__name__}: {exc}",
            flush=True,
        )
        return 2
    finally:
        if monitor.thread.is_alive():
            monitor.stop()
        summary["total_seconds"] = time.perf_counter() - started
        save()
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, required=True, help="Completed mappluto.py run.duckdb"
    )
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[10000, 50000, 1000])
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--memory-limit", default="1GB")
    parser.add_argument("--max-spill", default="20GB")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if (
        args.threads < 1
        or any(n < 1 for n in args.sizes)
        or (args.batch_size is not None and args.batch_size < 1)
    ):
        parser.error("Thread counts and batch sizes must be positive")
    if args.worker:
        if args.batch_size is None:
            parser.error("Worker requires --batch-size")
        sys.exit(worker(args))
    if len(set(args.sizes)) != len(args.sizes):
        parser.error("Batch sizes must be unique")
    args.out.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[3]
    report = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=root, text=True
            ).strip()
        ),
        "runner_sha256": sha256(Path(__file__)),
        "database_sha256": sha256(args.database),
        "extension_sha256": sha256(args.extension),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "order": args.sizes,
        "threads": args.threads,
        "memory_limit": args.memory_limit,
        "max_temp_directory_size": args.max_spill,
        "measurement": "Single sequential full pass per size; reused reference; profiler enabled; no warmup",
        "runs": [],
    }
    for size in args.sizes:
        out = args.out / f"batch-{size}"
        command = [
            sys.executable,
            "-u",
            str(Path(__file__).resolve()),
            "--worker",
            "--database",
            str(args.database.resolve()),
            "--extension",
            str(args.extension.resolve()),
            "--out",
            str(out.resolve()),
            "--batch-size",
            str(size),
            "--threads",
            str(args.threads),
            "--memory-limit",
            args.memory_limit,
            "--max-spill",
            args.max_spill,
        ]
        result = subprocess.run(command, check=False)
        if (out / "summary.json").exists():
            run = json.loads((out / "summary.json").read_text())
        else:
            run = {
                "status": "error",
                "batch_size": size,
                "error": "Worker produced no summary",
            }
        run["exit_code"] = result.returncode
        if result.returncode != 0:
            run["status"] = "error"
        report["runs"].append(run)
        (args.out / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    sys.exit(2 if any(run["status"] != "ok" for run in report["runs"]) else 0)


if __name__ == "__main__":
    main()
