"""Reproducible standard-matcher benchmarks; see the adjacent README."""

from __future__ import annotations

import argparse
import contextlib
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import resource
import statistics
import subprocess
import sys
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

from whereabouts.AddressLoader import AddressLoader  # noqa: E402
from whereabouts.Matcher import Matcher  # noqa: E402
from whereabouts.matching_queries.common import register_functions  # noqa: E402
from whereabouts.matching_queries.standard import create_matching_query  # noqa: E402

ENGINES = ("python_query", "python_api", "extension_query")
SQL_PATH = ROOT / "extensions/duckdb/sql/whereabouts.sql"


def canonical(value: str | None) -> str:
    """Compare labels ignoring case/spacing/punctuation, but NOT accents or tokens."""
    if value is None:
        return ""
    return " ".join(
        re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", value).upper()).split()
    )


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def nullable_float(value: object) -> float | None:
    return float(value) if value is not None and math.isfinite(float(value)) else None


def accuracy(cases: list[dict], predictions: list[dict], threshold: float) -> dict:
    """No-match negatives are separate from positive recall and returned precision."""
    if len(cases) != len(predictions):
        raise ValueError("Prediction cardinality does not match cases")
    counts = dict(
        total=len(cases),
        positives=0,
        covered_positives=0,
        negatives=0,
        returned=0,
        correct=0,
        covered_correct=0,
        false_positives=0,
        correct_rejections=0,
        wrong_matches=0,
    )
    for case, prediction in zip(cases, predictions, strict=True):
        if case["case_id"] != prediction["case_id"]:
            raise ValueError("Predictions must be aligned by case_id")
        expected = canonical(case["expected_address"])
        got = canonical(prediction["address"])
        accepted = (
            bool(got)
            and prediction["score"] is not None
            and prediction["score"] >= threshold
        )
        counts["returned"] += accepted
        if expected:
            counts["positives"] += 1
            counts["covered_positives"] += case["covered"]
            correct = accepted and expected == got
            counts["correct"] += correct
            counts["covered_correct"] += correct and case["covered"]
            counts["wrong_matches"] += accepted and not correct
        else:
            counts["negatives"] += 1
            counts["false_positives"] += accepted
            counts["correct_rejections"] += not accepted

    def ratio(numerator: str, denominator: str) -> float | None:
        return counts[numerator] / counts[denominator] if counts[denominator] else None

    return {
        **counts,
        "threshold": threshold,
        "positive_recall": ratio("correct", "positives"),
        "covered_positive_recall": ratio("covered_correct", "covered_positives"),
        "returned_precision": ratio("correct", "returned"),
        "negative_false_positive_rate": ratio("false_positives", "negatives"),
    }


def make_cases(reference: pd.DataFrame, samples: int, seed: int) -> list[dict]:
    """Known reference labels + deterministic synthetic edits, NOT held-out truth."""
    population = reference.sort_values("address_id").to_dict("records")
    chosen = random.Random(seed).sample(population, min(samples, len(population)))
    cases = []
    for record in chosen:
        label = record["address"]
        variants = [
            ("exact", label),
            ("formatting", "  " + label.lower().replace(" ", " , ") + "  "),
        ]
        expanded = re.sub(
            r"\b(ST|RD|AVE|AV|DR|CT)\b",
            lambda m: {
                "ST": "STREET",
                "RD": "ROAD",
                "AVE": "AVENUE",
                "AV": "AVENUE",
                "DR": "DRIVE",
                "CT": "COURT",
            }[m[0]],
            label.upper(),
        )
        if canonical(expanded) != canonical(label):
            variants.append(("expanded_abbreviation", expanded))
        typo = re.sub(
            r"\b([^\W\d_]{4,})\b", lambda m: m[0][:1] + "X" + m[0][2:], label, count=1
        )
        if canonical(typo) != canonical(label):
            variants.append(("single_letter_typo", typo))
        postcode = str(record.get("postcode", ""))
        missing = (
            re.sub(r"\b" + re.escape(postcode) + r"\b", "", label)
            if postcode
            else label
        )
        if canonical(missing) != canonical(label):
            variants.append(("missing_postcode", missing))
        unaccented = "".join(
            c
            for c in unicodedata.normalize("NFKD", label)
            if not unicodedata.combining(c)
        )
        if canonical(unaccented) != canonical(label):
            variants.append(("diacritics_removed", unaccented))
        for category, address in variants:
            cases.append(
                {
                    "input_address": address,
                    "expected_address": label,
                    "category": category,
                    "covered": True,
                }
            )
    for number in range(20):
        cases.append(
            {
                "input_address": f"99999999 ZZBENCHUNKNOWNZZ ZZVOIDZZ {90000000 + number}",
                "expected_address": None,
                "category": "synthetic_no_match",
                "covered": False,
            }
        )
    for address in [None, "", "  "]:
        cases.append(
            {
                "input_address": address,
                "expected_address": None,
                "category": "empty_input",
                "covered": False,
            }
        )
    random.Random(seed).shuffle(cases)
    return [{"case_id": i + 1, **case} for i, case in enumerate(cases)]


def read_reference(dataset: str, custom: Path | None) -> tuple[pd.DataFrame, Path]:
    path = custom or ROOT / (
        "example_data/test_data3056.parquet"
        if dataset == "au"
        else "example_data/test_datapoland.csv"
    )
    with duckdb.connect() as con:
        frame = (
            con.read_parquet(str(path))
            if path.suffix == ".parquet"
            else con.read_csv(str(path))
        ).df()
    if dataset != "custom":
        mapping = {
            "row_num" if dataset == "au" else "ADDRESS_DETAIL_PID": "address_id",
            "ADDRESS_LABEL": "address",
            "LOCALITY_NAME": "suburb",
            "POSTCODE": "postcode",
            "STATE": "state",
            "LATITUDE" if dataset == "au" else "LAT": "latitude",
            "LONGITUDE" if dataset == "au" else "LON": "longitude",
        }
        frame = frame.rename(columns=mapping)
    required = {"address_id", "address", "latitude", "longitude"}
    if not required.issubset(frame.columns) or frame[list(required)].isna().any().any():
        raise ValueError(
            "Benchmark reference needs non-NULL address_id,address,latitude,longitude"
        )
    if (
        frame.address_id.astype(str).duplicated().any()
        or frame.address.map(canonical).eq("").any()
    ):
        raise ValueError("Reference IDs must be unique and addresses nonempty")
    for name in ["suburb", "postcode", "state"]:
        frame[name] = frame[name].fillna("").astype(str) if name in frame else ""
    # Match the Python schema's integer IDs, retaining a common deterministic order.
    frame = (
        frame.assign(original_id=frame.address_id.astype(str))
        .sort_values("original_id")
        .reset_index(drop=True)
    )
    frame["address_id"] = range(1, len(frame) + 1)
    # DuckDB 1.4.2 predates pandas 3's inferred `str` extension dtype.
    for name in frame.columns:
        if pd.api.types.is_string_dtype(frame[name].dtype):
            frame[name] = frame[name].astype(object)
    return frame, path


def install_sql(con: duckdb.DuckDBPyConnection, binary: str | None) -> None:
    if binary:
        escaped = str(Path(binary).resolve()).replace("'", "''")
        con.execute(f"LOAD '{escaped}'")
    else:
        con.execute(SQL_PATH.read_text())


def prepare(reference: pd.DataFrame, database: Path, args: argparse.Namespace) -> dict:
    con = duckdb.connect(
        str(database),
        config={
            "threads": args.threads,
            "memory_limit": args.memory_limit,
            "allow_unsigned_extensions": "true",
        },
    )
    try:
        # No downloads during a benchmark. Install splink_udfs explicitly beforehand.
        con.execute("LOAD splink_udfs")
        con.register("source_frame", reference)
        loader = AddressLoader.__new__(AddressLoader)
        loader.con, loader.db = con, str(database)
        start = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            loader.create_geocoder_tables()
            con.execute("""INSERT INTO addrtext SELECT address_id, address, NULL, suburb, postcode, state,
                           latitude, longitude FROM source_frame""")
            loader.create_final_address_table()
            loader.create_phrases()
            loader.create_inverted_index()
        python_seconds = time.perf_counter() - start
        install_sql(con, args.extension)
        start = time.perf_counter()
        con.execute(
            "CREATE TABLE wa_reference AS FROM wa_prepare_reference('source_frame')"
        )
        con.execute("CREATE TABLE wa_index AS FROM wa_build_index('wa_reference')")
        extension_seconds = time.perf_counter() - start
        # Shared-reference control preserves the upstream tokens and index exactly.
        con.execute("""CREATE VIEW shared_reference AS
            SELECT CAST(addr_id AS VARCHAR) AS reference_id, addr AS address, addr AS normalized_address,
                   numeric_tokens, NULLIF(regexp_extract_all(addr,'[A-Z]+'),[]::VARCHAR[]) AS alpha_tokens,
                   latitude, longitude FROM addrtext_with_detail;
            CREATE VIEW shared_index AS
            SELECT tokenphrase AS phrase, CAST(addr_ids AS VARCHAR[]) AS reference_ids, frequency FROM phraseinverted;""")
        counts = con.execute("""SELECT (SELECT count(*) FROM addrtext_with_detail),
            (SELECT count(*) FROM phraseinverted), (SELECT count(*) FROM wa_reference),
            (SELECT count(*) FROM wa_index)""").fetchone()
        con.execute("CHECKPOINT")
        return {
            "python_build_seconds": python_seconds,
            "extension_build_seconds": extension_seconds,
            "python_reference_rows": counts[0],
            "python_index_phrases": counts[1],
            "extension_reference_rows": counts[2],
            "extension_index_phrases": counts[3],
            "database_bytes_combined": database.stat().st_size,
        }
    finally:
        con.close()


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024 * 1024 if sys.platform == "darwin" else 1024)


def worker(job_path: Path, output: Path) -> None:
    job = json.loads(job_path.read_text())
    base = json.loads(Path(job["cases_file"]).read_text())
    cases = [{**base[i % len(base)], "case_id": i + 1} for i in range(job["size"])]
    addresses = [case["input_address"] for case in cases]
    ids = [case["case_id"] for case in cases]
    frame = pd.DataFrame({"address_id": ids, "address": addresses})
    frame["address"] = frame["address"].astype(object)
    setup_start = time.perf_counter()
    con = duckdb.connect(
        config={
            "threads": job["threads"],
            "memory_limit": job["memory_limit"],
            "allow_unsigned_extensions": "true",
        }
    )
    engine = job["engine"]
    try:
        path = job["database"].replace("'", "''")
        con.execute(f"ATTACH '{path}' AS remote (READ_ONLY)")
        if engine.startswith("python"):
            register_functions(con)
            con.execute("LOAD splink_udfs")
            # Execute the unmodified public geocode method with a controlled connection.
            matcher = Matcher.__new__(Matcher)
            matcher.con, matcher.how, matcher.threshold = con, "standard", 0.0
            matcher.pipeline = create_matching_query(con)
            query = (
                "SELECT address_id, address_matched, latitude, longitude, similarity FROM ("
                + matcher.pipeline.createCTEs()
                + ")"
            )
        else:
            install_sql(con, job["extension"])
            prefix = "shared" if job["reference_mode"] == "shared" else "wa"
            query = f"""SELECT CAST(address_id AS BIGINT), matched_address, latitude, longitude, similarity
                FROM wa_geocode('input_addresses','remote.{prefix}_reference','remote.{prefix}_index')"""
        if engine != "python_api":
            con.register("input_frame", frame)
            con.execute("CREATE TABLE input_addresses AS SELECT * FROM input_frame")
        setup_seconds = time.perf_counter() - setup_start
        before_rss = peak_rss_mb()
        timings, predictions = [], None
        for trial in range(1 + job["warmups"] + job["repeats"]):
            start = time.perf_counter()
            result = (
                matcher.geocode(addresses, address_ids=ids)
                if engine == "python_api"
                else con.execute(query).fetchall()
            )
            elapsed = time.perf_counter() - start
            timings.append(elapsed)
            if engine == "python_api":
                result = [
                    (
                        r["address_id"],
                        r["address_matched"],
                        r["latitude"],
                        r["longitude"],
                        r["similarity"],
                    )
                    for r in result
                ]
            current = [
                {
                    "case_id": int(row[0]),
                    "address": row[1] if isinstance(row[1], str) else None,
                    "latitude": nullable_float(row[2]),
                    "longitude": nullable_float(row[3]),
                    "score": nullable_float(row[4]),
                }
                for row in sorted(result, key=lambda row: int(row[0]))
            ]
            if [r["case_id"] for r in current] != ids:
                raise AssertionError("Missing/duplicate/reordered case IDs")
            if predictions is not None and predictions != current:
                raise AssertionError("Results changed between repetitions")
            predictions = current
        measured = timings[1 + job["warmups"] :]
        summary = {
            "status": "ok",
            "engine": engine,
            "reference_mode": job["reference_mode"],
            "rows": len(cases),
            "setup_seconds": setup_seconds,
            "first_call_seconds": timings[0],
            "warm_seconds": measured,
            "median_seconds": statistics.median(measured),
            "rows_per_second": len(cases) / statistics.median(measured),
            "worker_peak_rss_mb": peak_rss_mb(),
            "pre_query_peak_rss_mb": before_rss,
            "duckdb_version": duckdb.__version__,
            "predictions": predictions,
        }
        output.write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    finally:
        con.close()


def compare_predictions(
    cases: list[dict], left: list[dict], right: list[dict]
) -> tuple[dict, list[dict]]:
    disagreements, score_differences, distances = [], [], []
    for case, a, b in zip(cases, left, right, strict=True):
        same = canonical(a["address"]) == canonical(b["address"])
        delta = (
            abs(a["score"] - b["score"])
            if a["score"] is not None and b["score"] is not None
            else None
        )
        if delta is not None:
            score_differences.append(delta)
        if a["latitude"] is not None and b["latitude"] is not None:
            lat1, lat2 = math.radians(a["latitude"]), math.radians(b["latitude"])
            dlat, dlon = lat2 - lat1, math.radians(b["longitude"] - a["longitude"])
            h = (
                math.sin(dlat / 2) ** 2
                + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
            )
            distances.append(6371000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, h)))))
        if (
            not same
            or (delta is not None and delta > 1e-10)
            or ((a["score"] is None) != (b["score"] is None))
        ):
            disagreements.append(
                {
                    **case,
                    "same_label": same,
                    "python_address": a["address"],
                    "extension_address": b["address"],
                    "python_score": a["score"],
                    "extension_score": b["score"],
                }
            )
    return {
        "rows": len(cases),
        "label_agreement": sum(
            canonical(a["address"]) == canonical(b["address"])
            for a, b in zip(left, right, strict=True)
        )
        / len(cases),
        "label_or_score_disagreement_rows": sum(
            not r["same_label"] or r["python_score"] != r["extension_score"]
            for r in disagreements
        ),
        "max_score_difference_when_both_returned": max(score_differences, default=None),
        "max_coordinate_difference_m_when_both_returned": max(distances, default=None),
    }, disagreements


def run(args: argparse.Namespace) -> int:
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    summary = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "threads": args.threads,
        "memory_limit": args.memory_limit,
        "seed": args.seed,
        "repeats": args.repeats,
        "warmups": args.warmups,
        "runner_sha256": file_hash(Path(__file__)),
        "sql_sha256": file_hash(SQL_PATH),
        "distribution": "native" if args.extension else "sql",
        "extension_sha256": file_hash(Path(args.extension)) if args.extension else None,
        "datasets": {},
    }
    datasets = ["au", "pl"] if args.dataset == "all" else [args.dataset]
    for dataset in datasets:
        print(f"Preparing {dataset}", flush=True)
        directory = out / dataset
        directory.mkdir()
        reference, source_path = read_reference(dataset, args.reference)
        cases = make_cases(reference, args.samples, args.seed)
        labels_path = args.cases or (
            ROOT / "evaluation_data/test_set_au.csv" if dataset == "au" else None
        )
        if labels_path:
            keys = {canonical(label) for label in reference.address}
            with labels_path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                fields = set(reader.fieldnames or [])
                if "input_address" not in fields or not fields.intersection(
                    {"expected_address", "best_match"}
                ):
                    raise ValueError(
                        "Labeled CSV requires input_address and expected_address (or best_match)"
                    )
                for row in reader:
                    expected = (
                        row.get("expected_address", row.get("best_match")) or None
                    )
                    cases.append(
                        {
                            "case_id": len(cases) + 1,
                            "input_address": row["input_address"],
                            "expected_address": expected,
                            "covered": bool(expected) and canonical(expected) in keys,
                            "category": "supplied_labels",
                        }
                    )
        cases_file = directory / "cases.json"
        cases_file.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n")
        database = directory / "reference.duckdb"
        preparation = prepare(reference, database, args)
        data = {
            "reference_sha256": file_hash(source_path),
            "reference_rows": len(reference),
            "labels_sha256": file_hash(labels_path) if labels_path else None,
            "cases_sha256": file_hash(cases_file),
            "case_count": len(cases),
            "preparation": preparation,
            "runs": [],
            "accuracy": {},
            "parity": {},
        }
        base_predictions = {}
        jobs = [
            (size, engine, "independent")
            for size in sorted(set(args.sizes + [len(cases)]))
            for engine in ENGINES
        ]
        jobs.append((len(cases), "extension_query", "shared"))
        random.Random(args.seed).shuffle(jobs)
        for size, engine, mode in jobs:
            print(f"  {engine} {mode} rows={size}", flush=True)
            stem = f"{engine}-{mode}-{size}"
            job = {
                "engine": engine,
                "reference_mode": mode,
                "size": size,
                "cases_file": str(cases_file),
                "database": str(database),
                "threads": args.threads,
                "memory_limit": args.memory_limit,
                "extension": args.extension,
                "repeats": args.repeats,
                "warmups": args.warmups,
            }
            job_file, result_file = (
                directory / f"{stem}-job.json",
                directory / f"{stem}.json",
            )
            job_file.write_text(json.dumps(job))
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                str(job_file),
                "--worker-output",
                str(result_file),
            ]
            try:
                process = subprocess.run(
                    command, capture_output=True, text=True, timeout=args.timeout
                )
                (directory / f"{stem}.log").write_text(process.stdout + process.stderr)
                result = (
                    json.loads(result_file.read_text())
                    if result_file.exists()
                    else {
                        "status": "error",
                        "error_type": "WorkerExit",
                        "error": f"Worker exited {process.returncode}",
                    }
                )
                if process.returncode and result.get("status") == "ok":
                    result = {
                        "status": "error",
                        "error_type": "WorkerExit",
                        "error": f"Worker exited {process.returncode}",
                    }
            except subprocess.TimeoutExpired:
                result = {
                    "status": "error",
                    "error_type": "Timeout",
                    "error": f"Exceeded {args.timeout} seconds",
                }
            result.update(engine=engine, reference_mode=mode, rows=size)
            predictions = result.pop("predictions", None)
            data["runs"].append(result)
            if result["status"] != "ok":
                print(f"    FAILED: {result['error_type']}", flush=True)
                continue
            if size == len(cases):
                key = f"{engine}/{mode}"
                base_predictions[key] = predictions
                categories = {
                    "all": list(range(len(cases))),
                    "generated": [
                        i
                        for i, case in enumerate(cases)
                        if case["category"] != "supplied_labels"
                    ],
                }
                for i, case in enumerate(cases):
                    categories.setdefault(case["category"], []).append(i)
                data["accuracy"][key] = {
                    category: [
                        accuracy(
                            [cases[i] for i in indices],
                            [predictions[i] for i in indices],
                            threshold,
                        )
                        for threshold in [0.0, 0.25, 0.5, 0.75, 0.9]
                    ]
                    for category, indices in categories.items()
                }
        baseline = base_predictions.get("python_query/independent")
        for key, predictions in base_predictions.items():
            if baseline is None:
                data["parity"][key] = {
                    "unavailable": "Python query accuracy run failed"
                }
                continue
            parity, disagreements = compare_predictions(cases, baseline, predictions)
            data["parity"][key] = parity
            if disagreements:
                with (directory / (key.replace("/", "-") + "-disagreements.csv")).open(
                    "w", newline=""
                ) as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(disagreements[0]))
                    writer.writeheader()
                    writer.writerows(disagreements)
        data["runs"].sort(key=lambda r: (r["rows"], r["engine"], r["reference_mode"]))
        summary["datasets"][dataset] = data
        (out / "summary.json").write_text(
            json.dumps(summary, indent=2, allow_nan=False) + "\n"
        )
    failures = sum(
        r["status"] != "ok"
        for data in summary["datasets"].values()
        for r in data["runs"]
    )
    print(
        f"Results: {out / 'summary.json'} ({failures} failed worker jobs)", flush=True
    )
    return 2 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--out", type=Path, default=ROOT / "extensions/duckdb/build/benchmarks"
    )
    parser.add_argument(
        "--dataset", choices=["all", "au", "pl", "custom"], default="all"
    )
    parser.add_argument(
        "--reference", type=Path, help="Custom canonical reference CSV/Parquet"
    )
    parser.add_argument("--cases", type=Path, help="Optional independently labeled CSV")
    parser.add_argument(
        "--extension",
        type=str,
        help="Native binary path; omit to test SQL distribution",
    )
    parser.add_argument("--samples", type=int, default=128)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--memory-limit", default="1GB")
    parser.add_argument("--seed", type=int, default=20261023)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if args.worker:
        try:
            worker(args.worker, args.worker_output)
        except Exception as exc:
            args.worker_output.write_text(
                json.dumps(
                    {
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "worker_peak_rss_mb": peak_rss_mb(),
                    },
                    indent=2,
                )
                + "\n"
            )
            raise
        return
    if (
        min(args.samples, args.repeats, args.threads, *args.sizes) < 1
        or args.warmups < 0
    ):
        parser.error(
            "samples, repeats, threads, sizes must be positive; warmups must be nonnegative"
        )
    if (args.dataset == "custom") != bool(args.reference):
        parser.error("--reference is required only for --dataset custom")
    if args.cases and args.dataset == "all":
        parser.error("Choose one dataset when supplying --cases")
    if args.extension:
        args.extension = str(Path(args.extension).resolve(strict=True))
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
