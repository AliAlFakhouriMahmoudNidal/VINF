import csv
import io
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import indexer


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fields = ["canonical_id", "source_url", *indexer.FIELD_WEIGHTS]
        rows = [
            dict(primary_name="Red rose", soil_types="Clay, loam and sand",
                 description="Red flowers"),
            dict(primary_name="White rose", soil_types="clay",
                 description="Flowers in loam"),
            dict(primary_name="Blue iris", soil_types="loam clay",
                 description="Blue petals"),
            dict(primary_name="Redwood", soil_types="sand"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "plants.tsv"
            with source.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fields, delimiter="\t")
                writer.writeheader()
                for doc_id, values in enumerate(rows, 1):
                    row = dict.fromkeys(fields, "")
                    row.update(values, canonical_id=doc_id,
                               source_url=f"https://example.test/{doc_id}")
                    writer.writerow(row)
            with redirect_stdout(io.StringIO()):
                cls.index, cls.documents = indexer.build_index(source)

    def results(self, query, **kwargs):
        return indexer.search(query, self.index, self.documents, **kwargs)

    def ids(self, query, **kwargs):
        return {doc_id for doc_id, _ in self.results(query, **kwargs)}

    def test_plain_queries_and_scores(self):
        for method in ("classic", "bm25"):
            for mode, expected_ids in (("or", {1, 2}), ("and", {1})):
                with self.subTest(method=method, mode=mode):
                    results = dict(self.results("red flowers", method=method, mode=mode))
                    self.assertEqual(set(results), expected_ids)
                    for doc_id, actual in results.items():
                        expected = 0
                        for term in ("red", "flowers"):
                            postings = self.index[term]
                            df = len(postings)
                            word_idf = (math.log(4 / df) if method == "classic"
                                        else math.log1p((4 - df + 0.5) / (df + 0.5)))
                            for found_id, classic, bm25 in postings:
                                if found_id == doc_id:
                                    weight = (classic if method == "classic" else
                                              bm25 * 2.2 / (bm25 + 1.2))
                                    expected += weight * word_idf
                        self.assertAlmostEqual(actual, expected * 2)
            self.assertEqual(self.results("red red FLOWERS", method=method),
                             self.results("red flowers", method=method))
        self.assertEqual(self.ids("red unknown", mode="and"), set())
        self.assertEqual(self.ids("red unknown"), {1})
        self.assertEqual(self.results("!!!", mode="and"), [])

    def test_field_and_or(self):
        self.assertEqual(self.ids('--and--soil_types="CLAY, loam clay"'), {1, 3})
        self.assertEqual(self.ids('--or--soil_types="clay sand"'), {1, 2, 3, 4})
        self.assertEqual(self.ids('--and--primary_name="red"'), {1})
        self.assertEqual(self.ids('--and--soil_types="red"'), set())
        self.assertEqual(self.ids('--or--soil_types="unknown clay"'), {1, 2, 3})
        self.assertEqual(self.ids('--and--soil_types="unknown clay"'), set())

    def test_all_fields(self):
        for field in indexer.FIELD_WEIGHTS:
            with self.subTest(field=field):
                value = self.documents[1][field]
                if value:
                    term = indexer.tokenize(value)[0]
                    self.assertIn(1, self.ids(f'--and--{field}="{term}"'))
                else:
                    self.assertNotIn(1, self.ids(f'--or--{field}="red"'))

    def test_full_text_is_word_based(self):
        self.assertEqual(self.ids('--and--phrase="flowers rose"'), {1, 2})
        self.assertEqual(self.ids('--or--phrase="red blue"'), {1, 3})

    def test_clauses_intersect_independent_of_order_and_mode(self):
        first = '--and--soil_types="clay loam"'
        second = '--or--phrase="red flowers"'
        for method in ("classic", "bm25"):
            expected = self.results(first + " " + second, method=method)
            self.assertEqual({doc_id for doc_id, _ in expected}, {1})
            for mode in ("and", "or"):
                actual = self.results(second + " " + first, method=method, mode=mode)
                self.assertEqual([doc_id for doc_id, _ in actual], [1])
                self.assertAlmostEqual(actual[0][1], expected[0][1])

    def test_structured_scores_preserve_global_weights(self):
        query = '--and--soil_types="clay loam" --or--phrase="red flowers"'
        for method in ("classic", "bm25"):
            plain = dict(self.results("clay loam red flowers", method=method))
            self.assertEqual(self.results(query, method=method), [(1, plain[1])])
            self.assertEqual(self.results('--or--phrase="red flowers"', method=method),
                             self.results("red flowers", method=method))

    def test_invalid_queries(self):
        queries = [
            '--and--unknown="red"', '--or--canonical_id="1"',
            '--or--source_url="example"', '--and--phrase=""',
            '--and--phrase="!!!"', '--and--phrase=red',
            '--and--phrase="red', '--xor--phrase="red"',
            '--and--phrase="red" extra', 'extra --and--phrase="red"',
            '--and--phrase="red"--or--phrase="blue"',
            '--and--phrase="red\\"flowers"',
        ]
        for query in queries:
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.results(query)

    def test_highlighting_uses_values_only(self):
        documents = {1: dict(self.documents[1], primary_name="and or phrase red")}
        body = indexer.result_text(1, 1, 0, documents, False,
                                   '--and--phrase="red"')
        highlighted = [body.plain[span.start:span.end] for span in body.spans
                       if span.style == "bold red"]
        self.assertEqual(highlighted, ["red", "Red"])

    def test_session_commands_and_error_recovery(self):
        commands = ["help", "mode and", "details on", "limit 2",
                    '--and--unknown="red"', "red flowers",
                    '--or--phrase="red flowers"', "details off", "mode or", ""]
        with patch.object(indexer, "build_index", return_value=(self.index, self.documents)), \
                patch.object(indexer.sys, "argv", ["indexer.py"]), \
                patch("builtins.input", side_effect=commands), \
                patch.object(indexer, "print_results") as display, \
                redirect_stdout(io.StringIO()) as output:
            indexer.main()
        self.assertIn("Searchable fields:", output.getvalue())
        self.assertIn("Unknown field:", output.getvalue())
        self.assertEqual(display.call_count, 2)
        self.assertEqual({doc_id for doc_id, _ in display.call_args_list[0].args[0]}, {1})
        self.assertEqual({doc_id for doc_id, _ in display.call_args_list[1].args[0]}, {1, 2})
        self.assertTrue(display.call_args_list[0].args[3])
        self.assertTrue(all(len(call.args[0]) <= 2 for call in display.call_args_list))


if __name__ == "__main__":
    unittest.main()
