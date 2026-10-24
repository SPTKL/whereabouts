"""Verify full-pass accounting for blank addresses and duplicate address labels."""

import importlib.util
import json
from pathlib import Path
import sys

import duckdb
import pytest


ROOT = Path(__file__).parents[1]
BINARY = ROOT / "extensions/duckdb/build/whereabouts.duckdb_extension"
SCRIPT = ROOT / "extensions/duckdb/benchmarks/mappluto.py"


@pytest.mark.skipif(not BINARY.exists(), reason="Build native extension first")
def test_complete_pass_preserves_blanks_and_duplicate_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.parquet"
    with duckdb.connect() as con:
        con.execute(
            """COPY (SELECT * FROM (VALUES
            (1000000001, '10 PARK PLACE', 'MN', 10007, 40.71, -74.00, 'fixture'),
            (1000000002, '10 PARK PLACE', 'MN', 10007, 40.72, -74.00, 'fixture'),
            (1000000003, '20 WORTH STREET', 'MN', 10013, 40.73, -74.00, 'fixture'),
            (1000000004, NULL, 'MN', 10013, 40.74, -74.00, 'fixture'),
            (1000000005, ' ', 'MN', 10013, 40.75, -74.00, 'fixture')
            ) t(BBL,Address,Borough,ZipCode,Latitude,Longitude,Version))
            TO ? (FORMAT PARQUET)""",
            [str(source)],
        )
    spec = importlib.util.spec_from_file_location("mappluto_benchmark", SCRIPT)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    out = tmp_path / "run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--source",
            str(source),
            "--extension",
            str(BINARY),
            "--out",
            str(out),
            "--batch-size",
            "2",
        ],
    )
    benchmark.main()
    summary = json.loads((out / "summary.json").read_text())
    assert summary["status"] == "ok"
    assert summary["input_rows"] == 5
    assert summary["reference_rows"] == 3
    assert summary["blank_address_rows"] == 2
    assert [batch["rows"] for batch in summary["batches"]] == [2, 2, 1]
    assert summary["retrieval"] == {
        "matched": 3,
        "unmatched": 2,
        "same_bbl": 2,
        "same_normalized_address": 3,
        "score_one": 3,
        "blank_input_matched": 0,
    }
    with duckdb.connect() as con:
        assert (
            con.read_parquet(str(out / "results.parquet")).count("*").fetchone()[0] == 5
        )
        assert (
            con.read_parquet(str(out / "different_bbl.parquet"))
            .count("*")
            .fetchone()[0]
            == 3
        )
