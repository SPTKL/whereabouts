"""Exercise the actual CLI/extension using only the Python standard library."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


CLI = None
INSTALL_SQL = None
FIXTURE = """
CREATE TABLE reference_source AS SELECT * FROM (VALUES
 ('a', '125 WORTH STREET NEW YORK NY 10013', 40.715, -74.002),
 ('b', '125 WORTH STREET NEW YORK NY 10013', 40.716, -74.003),
 ('c', '200 BROADWAY NEW YORK NY 10007', 40.710, -74.010),
 ('fr', '10 Rue de l’École Paris 75005', 48.849, 2.345),
 ('pl', '12 Łódzka Warszawa 00100', 52.23, 21.01),
 ('gr', '10 ΟΔΟΣ ΑΘΗΝΑ', 37.98, 23.72)
) t(address_id, address, latitude, longitude);
CREATE TABLE reference AS FROM wa_prepare_reference('reference_source');
CREATE TABLE phrase_index AS FROM wa_build_index('reference');
CREATE TABLE inputs AS SELECT * FROM (VALUES
 ('1', '125 Worth Street, New York NY 10013'),
 ('2', '125 WORTH ST NEW YORK NY 10013'),
 ('3', '200 Broadway New York NY 10007'),
 ('4', '999 UNKNOWN ROAD'), ('5', NULL), ('6', ''),
 ('7', '125 WORTH STREET NEW YORK NY 10013'),
 ('8', '10 rue de l école Paris 75005'),
 ('9', '12 Łódzka Warszawa 00100'), ('10', '10 ΟΔΟΣ ΑΘΗΝΑ')
) t(address_id, address);
"""


class ExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if CLI is None:
            raise unittest.SkipTest("Run with make test or make test-sql")

    def run_sql(self, sql, database=None, install=True, fixture=True, error=None):
        command = [CLI, "-unsigned", "-bail", "-json", "-init", "/dev/null"]
        if database is not None:
            command.append(str(database))
        script = (INSTALL_SQL if install else "") + (FIXTURE if fixture else "") + sql
        result = subprocess.run(
            command, input=script, text=True, capture_output=True, timeout=60
        )
        if error is not None:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn(error, result.stderr)
            return None
        self.assertEqual(result.returncode, 0, result.stderr)
        # DuckDB 1.5.0's CLI emits this malformed JSON for a zero-row result.
        if result.stdout.strip() == "[{]":
            return []
        return json.loads(result.stdout) if result.stdout.strip() else []

    def test_exact_abbreviated_unmatched_and_unicode(self):
        rows = self.run_sql(
            "SELECT * FROM wa_geocode('inputs','reference','phrase_index') ORDER BY address_id;"
        )
        by_id = {r["address_id"]: r for r in rows}
        self.assertEqual(len(rows), 10)
        self.assertEqual(by_id["1"]["reference_id"], "a")
        self.assertEqual(by_id["1"]["similarity"], 1)
        self.assertAlmostEqual(by_id["2"]["similarity"], 0.8)
        self.assertEqual(by_id["3"]["reference_id"], "c")
        for key in ["4", "5", "6"]:
            self.assertIsNone(by_id[key]["reference_id"])
            self.assertIsNone(by_id[key]["match_rank"])
        self.assertEqual(by_id["7"]["reference_id"], "a")
        self.assertEqual(by_id["8"]["reference_id"], "fr")
        self.assertEqual(by_id["9"]["reference_id"], "pl")
        self.assertEqual(by_id["10"]["reference_id"], "gr")
        self.assertEqual(
            by_id["1"]["input_address"], "125 Worth Street, New York NY 10013"
        )

    def test_top_n_and_deterministic_ties(self):
        rows = self.run_sql(
            "SELECT * FROM wa_geocode('inputs','reference','phrase_index',top_n:=2) WHERE address_id='1' ORDER BY match_rank;"
        )
        self.assertEqual([r["reference_id"] for r in rows], ["a", "b"])
        self.assertEqual([r["match_rank"] for r in rows], [1, 2])

    def test_threshold_keeps_unmatched_input(self):
        rows = self.run_sql(
            "SELECT * FROM wa_geocode('inputs','reference','phrase_index',min_similarity:=0.9) WHERE address_id='2';"
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["reference_id"])

    def test_frequency_filter(self):
        rows = self.run_sql(
            "SELECT * FROM wa_geocode('inputs','reference','phrase_index',max_phrase_frequency:=1);"
        )
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(r["reference_id"] is None for r in rows))

    def test_parameters_rejected(self):
        for parameter, expected in [
            ("top_n:=0", "top_n"),
            ("top_n:=1.5", "top_n"),
            ("top_n:=NULL", "top_n"),
            ("top_n:=1001", "top_n"),
            ("min_similarity:=-0.1", "min_similarity"),
            ("min_similarity:='NaN'::DOUBLE", "min_similarity"),
            ("min_similarity:=1.1", "min_similarity"),
            ("max_phrase_frequency:=0", "max_phrase_frequency"),
        ]:
            with self.subTest(parameter=parameter):
                self.run_sql(
                    f"FROM wa_geocode('inputs','reference','phrase_index',{parameter});",
                    error=expected,
                )

    def test_duplicate_input_ids_rejected(self):
        self.run_sql(
            "INSERT INTO inputs VALUES ('1','another'); FROM wa_geocode('inputs','reference','phrase_index');",
            error="Input address_id must be unique",
        )

    def test_null_input_id_rejected(self):
        self.run_sql(
            "INSERT INTO inputs VALUES (NULL,'another'); FROM wa_geocode('inputs','reference','phrase_index');",
            error="Input address_id must be nonempty",
        )

    def test_invalid_references_rejected(self):
        for mutation, expected in [
            (
                "INSERT INTO reference_source SELECT * FROM reference_source WHERE address_id='a'",
                "must be unique",
            ),
            (
                "UPDATE reference_source SET address_id=NULL WHERE address_id='a'",
                "must be nonempty",
            ),
            (
                "UPDATE reference_source SET address='!!!' WHERE address_id='a'",
                "Reference address must be nonempty",
            ),
            (
                "UPDATE reference_source SET latitude=91 WHERE address_id='a'",
                "finite WGS84",
            ),
            (
                "UPDATE reference_source SET longitude=NULL WHERE address_id='a'",
                "both be present",
            ),
        ]:
            with self.subTest(mutation=mutation):
                self.run_sql(
                    mutation + "; FROM wa_prepare_reference('reference_source');",
                    error=expected,
                )

    def test_null_coordinates_are_allowed(self):
        rows = self.run_sql(
            "UPDATE reference_source SET latitude=NULL,longitude=NULL; SELECT * FROM wa_prepare_reference('reference_source');"
        )
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r["latitude"] is None for r in rows))

    def test_empty_input_and_reference(self):
        self.assertEqual(
            self.run_sql(
                "DELETE FROM inputs; FROM wa_geocode('inputs','reference','phrase_index');"
            ),
            [],
        )
        rows = self.run_sql(
            "DELETE FROM reference; DELETE FROM phrase_index; FROM wa_geocode('inputs','reference','phrase_index');"
        )
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(r["reference_id"] is None for r in rows))

    def test_metadata_joins_and_views(self):
        rows = self.run_sql("""
            CREATE VIEW mapped_input AS SELECT address_id, address FROM inputs WHERE address_id='3';
            SELECT m.reference_id, s.address FROM wa_geocode('mapped_input','reference','phrase_index') m
            JOIN reference_source s ON m.reference_id=s.address_id;
        """)
        self.assertEqual(rows[0]["reference_id"], "c")

    def test_numeric_gate(self):
        rows = self.run_sql(
            "DELETE FROM inputs; INSERT INTO inputs VALUES ('x','BROADWAY NEW YORK'),('y','999 BROADWAY NEW YORK NY 99999'); FROM wa_geocode('inputs','reference','phrase_index');"
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["reference_id"] is None for r in rows))

    def test_repeated_numeric_token_semantics(self):
        rows = self.run_sql(
            "SELECT CAST(_wa_numeric_gate(['1','1','1','1','1','1'],['1']) AS INTEGER) AS gate, _wa_iou(['1','1'],['1']) AS iou;"
        )
        self.assertEqual(rows, [{"gate": 0, "iou": 1.0}])

    def test_persisted_reference_read_only_attach(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reference.duckdb"
            self.run_sql("", database=path)
            rows = self.run_sql(
                f"ATTACH '{path}' AS external_ref (READ_ONLY); CREATE TABLE inputs AS SELECT * FROM external_ref.inputs; "
                "SELECT * FROM wa_geocode('inputs','external_ref.reference','external_ref.phrase_index');",
                fixture=False,
            )
            self.assertEqual(len(rows), 10)
            # The SQL-only API remains available in a reopened writable database.
            rows = self.run_sql(
                "SELECT wa_version() AS version;",
                database=path,
                install=False,
                fixture=False,
            )
            self.assertEqual(rows[0]["version"], "0.1.0")

    def test_table_names_are_not_executed_as_sql(self):
        self.run_sql(
            "FROM wa_geocode('inputs; DROP TABLE reference','reference','phrase_index');",
            error="does not exist",
        )

    def test_batch(self):
        rows = self.run_sql("""
            CREATE TABLE batch AS SELECT i AS address_id, '125 WORTH STREET NEW YORK NY 10013' AS address FROM range(1000) t(i);
            SELECT count(*) AS n, min(similarity) AS score FROM wa_geocode('batch','reference','phrase_index');
        """)
        self.assertEqual(rows, [{"n": 1000, "score": 1.0}])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duckdb", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--extension", type=Path)
    mode.add_argument("--sql", type=Path)
    args = parser.parse_args()
    CLI = str(Path(args.duckdb).resolve())
    if args.extension:
        path = str(args.extension.resolve()).replace("'", "''")
        INSTALL_SQL = f"LOAD '{path}';\n"
    else:
        INSTALL_SQL = args.sql.read_text() + "\n"
    unittest.main(argv=[__file__], verbosity=2)
