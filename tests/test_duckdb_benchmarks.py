"""Check benchmark accounting independently of either geocoder's implementation."""

import importlib.util
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = Path(__file__).parents[1] / "extensions/duckdb/benchmarks/compare.py"
SPEC = importlib.util.spec_from_file_location("whereabouts_benchmark_compare", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_label_comparison_does_not_hide_different_addresses() -> None:
    assert benchmark.canonical(" 10 Main-Street ") == benchmark.canonical(
        "10 MAIN STREET"
    )
    assert benchmark.canonical("10 Main St") != benchmark.canonical("10 Main Street")
    assert benchmark.canonical("10 École") != benchmark.canonical("10 Ecole")
    assert benchmark.canonical(None) == ""


def test_precision_recall_coverage_and_negative_controls() -> None:
    cases = [
        {"case_id": 1, "expected_address": "10 MAIN ST", "covered": True},
        {"case_id": 2, "expected_address": "20 MAIN ST", "covered": True},
        {"case_id": 3, "expected_address": "30 MAIN ST", "covered": False},
        {"case_id": 4, "expected_address": None, "covered": False},
        {"case_id": 5, "expected_address": None, "covered": False},
    ]
    predictions = [
        {"case_id": 1, "address": "10 Main-St", "score": 0.9},
        {"case_id": 2, "address": "20 MAIN ST", "score": 0.4},
        {"case_id": 3, "address": "10 MAIN ST", "score": 0.6},
        {"case_id": 4, "address": "10 MAIN ST", "score": 0.8},
        {"case_id": 5, "address": None, "score": None},
    ]
    result = benchmark.accuracy(cases, predictions, 0.5)
    assert result["positives"] == 3
    assert result["covered_positives"] == 2
    assert result["returned"] == 3
    assert result["correct"] == 1
    assert result["positive_recall"] == pytest.approx(1 / 3)
    assert result["covered_positive_recall"] == 0.5
    assert result["returned_precision"] == pytest.approx(1 / 3)
    assert result["wrong_matches"] == 1
    assert result["false_positives"] == 1
    assert result["negative_false_positive_rate"] == 0.5
    assert result["correct_rejections"] == 1
    stricter = benchmark.accuracy(cases, predictions, 1.0)
    assert stricter["returned_precision"] is None
    assert stricter["positive_recall"] == 0


def test_alignment_errors_are_not_silently_zipped() -> None:
    cases = [{"case_id": 1, "expected_address": "10 MAIN ST", "covered": True}]
    with pytest.raises(ValueError, match="cardinality"):
        benchmark.accuracy(cases, [], 0.0)
    with pytest.raises(ValueError, match="aligned"):
        benchmark.accuracy(cases, [{"case_id": 2}], 0.0)


def test_case_generation_is_repeatable_and_does_not_train_on_labels() -> None:
    frame = pd.DataFrame(
        [
            {"address_id": 1, "address": "10 MAIN ST CITY 12345", "postcode": "12345"},
            {
                "address_id": 2,
                "address": "20 Rue de l’École 75005",
                "postcode": "75005",
            },
        ]
    )
    first = benchmark.make_cases(frame, 2, 123)
    assert first == benchmark.make_cases(frame, 2, 123)
    assert first != benchmark.make_cases(frame, 2, 124)
    assert {row["expected_address"] for row in first} == set(frame.address) | {None}
    assert {row["category"] for row in first} >= {
        "exact",
        "formatting",
        "expanded_abbreviation",
        "single_letter_typo",
        "missing_postcode",
        "diacritics_removed",
        "synthetic_no_match",
        "empty_input",
    }
    assert [r["case_id"] for r in first] == list(range(1, len(first) + 1))


def test_agreement_is_not_correctness() -> None:
    cases = [{"case_id": 1, "expected_address": "10 MAIN ST", "covered": True}]
    wrong = [
        {
            "case_id": 1,
            "address": "20 MAIN ST",
            "score": 0.9,
            "latitude": 40.0,
            "longitude": -74.0,
        }
    ]
    parity, disagreements = benchmark.compare_predictions(cases, wrong, wrong)
    assert parity["label_agreement"] == 1
    assert disagreements == []
    assert benchmark.accuracy(cases, wrong, 0.0)["correct"] == 0


def test_coordinate_displacement_is_reported() -> None:
    cases = [{"case_id": 1, "expected_address": "10 MAIN ST", "covered": True}]
    first = [
        {
            "case_id": 1,
            "address": "10 MAIN ST",
            "score": 1.0,
            "latitude": 0.0,
            "longitude": 0.0,
        }
    ]
    second = [{**first[0], "longitude": 1.0}]
    parity, _ = benchmark.compare_predictions(cases, first, second)
    assert parity["max_coordinate_difference_m_when_both_returned"] == pytest.approx(
        111194.9, abs=1
    )
