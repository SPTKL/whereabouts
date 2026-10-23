"""Compare the SQL API against the existing standard matcher and Python scores."""

from pathlib import Path

import duckdb
import pytest

from whereabouts.matching_queries.common import register_functions
from whereabouts.matching_queries.standard import create_matching_query
from whereabouts.utils import IOU, IOU_min, list_overlap


SQL = (
    Path(__file__).parents[1] / "extensions" / "duckdb" / "sql" / "whereabouts.sql"
).read_text()


@pytest.mark.parametrize(
    "left,right",
    [
        (["1", "2"], ["2", "3"]),
        (["1", "1"], ["1"]),
        (["1"] * 6, ["1"]),
        ([], ["1"]),
        (["ALPHA", "BETA"], ["BETA", "GAMMA"]),
    ],
)
def test_sql_scores_match_python(left: list[str], right: list[str]) -> None:
    with duckdb.connect() as con:
        con.execute(SQL)
        actual = con.execute(
            "SELECT _wa_iou($1, $2), _wa_overlap_min($1, $2), _wa_numeric_gate($1, $2)",
            [left, right],
        ).fetchone()
        assert actual == pytest.approx(
            (IOU(left, right), IOU_min(left, right), list_overlap(left, right, 0.2))
        )


def test_standard_pipeline_parity() -> None:
    """Use the same ASCII data/index for both engines, including ties and no-match."""
    with duckdb.connect() as con:
        con.execute(SQL)
        con.execute("""
            CREATE TABLE raw_reference AS SELECT * FROM (VALUES
                ('1', '125 WORTH STREET NEW YORK NY 10013', 40.715, -74.002),
                ('2', '200 BROADWAY NEW YORK NY 10007', 40.710, -74.010),
                ('3', '125 WORTH STREET NEW YORK NY 10013', 40.716, -74.003),
                ('4', '10 MAIN ROAD LONDON AB12', 51.5, -0.12)
            ) t(address_id, address, latitude, longitude);
            CREATE TABLE reference AS FROM wa_prepare_reference('raw_reference');
            CREATE TABLE phrase_index AS FROM wa_build_index('reference');
            CREATE TABLE input_addresses AS SELECT * FROM (VALUES
                ('1', '125 WORTH STREET NEW YORK NY 10013'),
                ('2', '125 WORTH ST NEW YORK NY 10013'),
                ('3', '200 BROADWAY NEW YORK NY 10007'),
                ('4', '999 UNKNOWN ROAD'), ('5', NULL), ('6', ''),
                ('7', 'BROADWAY NEW YORK'), ('8', '10 MAIN ROAD LONDON AB12'),
                ('9', '125 WORTH STREET NEW YORK NY 10013')
            ) t(address_id, address);
            CREATE SCHEMA remote;
            CREATE VIEW remote.addrtext_with_detail AS
                SELECT reference_id AS addr_id, normalized_address AS addr, numeric_tokens,
                       '' AS suburb, '' AS state, '' AS postcode, latitude, longitude
                FROM reference;
            CREATE VIEW remote.phraseinverted AS
                SELECT phrase AS tokenphrase, reference_ids AS addr_ids, frequency
                FROM phrase_index;
        """)
        register_functions(con)
        # ASCII corpus: avoid requiring a downloaded splink_udfs extension.
        con.execute("CREATE MACRO unaccent(s) AS s;")
        upstream_query = create_matching_query(con).createCTEs()
        upstream = con.execute(
            "SELECT address_id, address_matched, latitude, longitude, similarity FROM ("
            + upstream_query
            + ") ORDER BY address_id"
        ).fetchall()
        port = con.execute("""
            SELECT address_id, matched_address, latitude, longitude, similarity
            FROM wa_geocode('input_addresses','reference','phrase_index') ORDER BY address_id
        """).fetchall()
        assert port == upstream


def test_extension_loads_in_python() -> None:
    """When a local binary is built, also exercise DuckDB's Python-hosted LOAD."""
    binary = (
        Path(__file__).parents[1]
        / "extensions"
        / "duckdb"
        / "build"
        / "whereabouts.duckdb_extension"
    )
    if not binary.exists():
        pytest.skip("Build the native extension to check Python-hosted LOAD")
    with duckdb.connect(config={"allow_unsigned_extensions": "true"}) as con:
        path = str(binary).replace("'", "''")
        con.execute(f"LOAD '{path}'")
        assert con.execute("SELECT wa_version()").fetchone() == ("0.1.0",)
