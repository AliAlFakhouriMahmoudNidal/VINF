"""TSV checks run locally; search integration checks require the PyLucene image."""

import ast
import csv
import importlib.util
import io
import json
import re
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lucene"))
lucene_spec = importlib.util.find_spec("lucene")
# The project directory is also named lucene and may resolve as a namespace.
HAS_LUCENE = lucene_spec is not None and lucene_spec.origin is not None
if HAS_LUCENE:
    import lucene_index as app
else:
    # Execute the actual pure helpers without importing the unavailable JVM bindings.
    root = Path(__file__).resolve().parents[1]
    module = ast.parse((root / "lucene/lucene_index.py").read_text(encoding="utf-8"))
    indexer = ast.parse((root / "indexer.py").read_text(encoding="utf-8"))
    constants = {"TEXT_WEIGHTS", "KEYWORD_FIELDS", "NUMBER_FIELDS", "SOURCED_FIELDS", "JSON_FIELDS", "ENRICHED_COLUMNS"}
    helpers = {"read_records", "field_text", "print_results"}
    nodes = [node for node in indexer.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == "FIELD_WEIGHTS" for target in node.targets)]
    nodes += [node for node in module.body
              if (isinstance(node, ast.FunctionDef) and node.name in helpers)
              or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants
                                                      for target in node.targets))]
    app = types.ModuleType("lucene_data_checks")
    app.__dict__.update(csv=csv, json=json, re=re, Path=Path)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(root / "lucene/lucene_index.py"), "exec"), app.__dict__)


def write_enriched(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write("\t".join(app.ENRICHED_COLUMNS) + "\n")
        for row in rows:
            cells = []
            for name in app.ENRICHED_COLUMNS:
                value = row.get(name, [] if name in app.JSON_FIELDS else None)
                if isinstance(value, list):
                    value = json.dumps(value, ensure_ascii=False)
                text = "" if value is None else str(value)
                cells.append(text.replace("\\", "\\\\").replace("\t", r"\t")
                             .replace("\r", r"\r").replace("\n", r"\n"))
            stream.write("\t".join(cells) + "\n")


class DataTests(unittest.TestCase):
    def test_current_tsv_decoding_and_content(self):
        row = dict.fromkeys(app.ENRICHED_COLUMNS)
        row.update({name: [] for name in app.JSON_FIELDS})
        row.update(canonical_id="1", primary_name="Rose", names=["Žltý kvet", r"Literal\nname"],
                   genus=[{"source": "rhs", "value": "Rosa"}, {"source": "wiki", "value": "Rubus"}],
                   candidate_page_ids=[10, 20], introduction=[{"source": "wiki", "text": "Intro"}],
                   description=[{"source": "rhs", "text": 'A\tB\nC\\nD "quoted"'},
                                {"source": "wiki", "text": r"A literal \N"}])
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            path = Path(temporary) / "fixture.tsv"
            write_enriched(path, [row])
            decoded = list(app.read_records(path))
        self.assertEqual(decoded, [row])
        self.assertEqual(app.field_text(decoded[0], "names"), "Žltý kvet; Literal\\nname")
        self.assertEqual(app.field_text(decoded[0], "genus"), "Rosa; Rubus")
        self.assertEqual(app.field_text(decoded[0], "description"), 'A\tB\nC\\nD "quoted"; A literal \\N')
        self.assertEqual(app.field_text(decoded[0], "habitat"), "")

    def test_every_sourced_field_uses_only_content(self):
        for name, key in app.SOURCED_FIELDS.items():
            with self.subTest(field=name):
                row = {name: [{"source": "rhs", key: "first"}, {"source": "wiki", key: "second"}]}
                self.assertEqual(app.field_text(row, name), "first; second")

    def test_schema_and_searchable_fields(self):
        stored_only = {"source_url", "wiki_url", "candidate_page_ids"}
        indexed = set(app.TEXT_WEIGHTS) | set(app.KEYWORD_FIELDS) | set(app.NUMBER_FIELDS)
        self.assertEqual(len(app.ENRICHED_COLUMNS), 26)
        self.assertEqual(indexed, set(app.ENRICHED_COLUMNS) - stored_only)
        self.assertIn("names", app.TEXT_WEIGHTS)
        self.assertEqual(app.KEYWORD_FIELDS, ("taxonomic_rank",))

    def test_wrong_header_and_malformed_row_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            path = Path(temporary) / "bad.tsv"
            for header, cells in [(app.ENRICHED_COLUMNS + ("extra",), None),
                                  (app.ENRICHED_COLUMNS, "1\ttoo few")]:
                path.write_text("\t".join(header) + "\n" + (cells + "\n" if cells else ""), encoding="utf-8")
                with self.assertRaises(ValueError):
                    list(app.read_records(path))

    def test_preview_uses_text_and_introduction_fallback(self):
        row = dict(canonical_id="1", primary_name="Rose", source_url="https://example.test/1",
                   description=[], introduction=[{"source": "wiki", "text": "Readable introduction"}])
        output = io.StringIO()
        with redirect_stdout(output):
            app.print_results(dict(total_hits=1, search_ms=0, results=[dict(score=1, record=row)]))
        self.assertIn("Readable introduction", output.getvalue())
        self.assertNotIn("'source'", output.getvalue())


@unittest.skipUnless(HAS_LUCENE, "PyLucene is installed in the Docker image")
class LuceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        cls.root = Path(cls.temporary.name)
        source = cls.root / "plants.tsv"
        values = [
            (2, "Abies alba", "White fragrant flowers", "clay loam", "forest", "matched"),
            (9, "Lavandula angustifolia", "Red flowers in summer", "sand", "rocky slopes", "matched"),
            (10, "Rosa alba", "Flowers turn red", "clay", "", "unmatched"),
            (20, "White rose", "White flowers", "clay loam", "", "unmatched"),
            (100, "Blue iris", "Blue flowers", "sand", "", "ambiguous"),
        ]
        rows = []
        for identifier, name, description, soil, habitat, status in values:
            row = dict(canonical_id=identifier, source_url=f"https://example.test/{identifier}",
                       primary_name=name, description=[{"source": "rhs", "text": description}],
                       soil_types=soil, habitat=[{"source": "wiki", "text": habitat}] if habitat else [],
                       taxonomic_rank="species" if habitat else None,
                       wiki_page_id=str(1000 + identifier) if habitat else None,
                       candidate_page_ids=[1000 + identifier] if habitat else ([2000, 3000] if status == "ambiguous" else []))
            if identifier == 2:
                row.update(names=["silver fir", "Žltý kvet"],
                           genus=[{"source": "rhs", "value": "Abies"}, {"source": "wiki", "value": "Pinus"}])
                row["description"].append({"source": "wiki", "text": "Evergreen conifer"})
            rows.append(row)
        write_enriched(source, rows)
        cls.index = cls.root / "index"
        cls.report = app.build_index(source, cls.index)
        cls.searcher = app.PlantSearch(cls.index)

    @classmethod
    def tearDownClass(cls):
        cls.searcher.close()
        cls.temporary.cleanup()

    def ids(self, query, **kwargs):
        return {int(hit["record"]["canonical_id"]) for hit in self.searcher.search(query, **kwargs)["results"]}

    def test_boolean_and_legacy_queries(self):
        self.assertEqual(self.ids("soil_types:clay AND NOT primary_name:rosa"), {2, 20})
        self.assertEqual(self.ids("primary_name:abies OR primary_name:iris"), {2, 100})
        self.assertEqual(self.ids('--and--soil_types="clay loam"'), {2, 20})
        self.assertEqual(self.ids("white flowers", mode="and"), {2, 20})
        self.assertEqual(self.ids("nonexistentword"), set())

    def test_phrase_checks_order_and_adjacency(self):
        self.assertEqual(self.ids('description:"red flowers"'), {9})
        self.assertEqual(self.ids("description:(red AND flowers)"), {9, 10})
        self.assertEqual(self.ids('description:"white flowers"'), {20})

    def test_fuzzy_typo(self):
        self.assertEqual(self.ids("primary_name:lavendula~1"), {9})
        self.assertEqual(self.ids("primary_name:lavendula"), set())

    def test_numeric_range_is_not_lexical(self):
        self.assertEqual(self.ids("canonical_id:[9 TO 20]"), {9, 10, 20})
        self.assertEqual(self.ids("canonical_id:{9 TO 20}"), {10})
        self.assertEqual(self.ids("canonical_id:100"), {100})
        self.assertEqual(self.ids("wiki_page_id:[1000 TO 1009]"), {2, 9})

    def test_wikipedia_and_persistence(self):
        self.assertEqual(self.ids("taxonomic_rank:species AND habitat:forest"), {2})
        self.assertEqual(self.report["documents"], 5)
        self.assertEqual(self.report["matched_documents"], 2)
        other = app.PlantSearch(self.index)
        try:
            result = other.search("canonical_id:2")
            self.assertEqual(result["total_hits"], 1)
            self.assertEqual(result["results"][0]["record"]["primary_name"], "Abies alba")
            self.assertEqual(result["results"][0]["record"]["description"], [
                {"source": "rhs", "text": "White fragrant flowers"},
                {"source": "wiki", "text": "Evergreen conifer"},
            ])
        finally:
            other.close()
        with self.assertRaises(FileExistsError):
            app.build_index(self.root / "plants.tsv", self.index)

    def test_names_and_sourced_content_are_searchable(self):
        for query in ('names:"silver fir"', '"silver fir"', 'names:"Žltý kvet"',
                      "genus:abies", "genus:pinus", "description:conifer", "habitat:forest"):
            with self.subTest(query=query):
                self.assertEqual(self.ids(query), {2})
        for query in ("rhs", "wiki", "source", "value", "text", "candidate_page_ids:2000",
                      "source_url:example", "wiki_url:example"):
            with self.subTest(query=query):
                self.assertEqual(self.ids(query), set())

    def test_duplicate_ids_rejected(self):
        source = self.root / "duplicates.tsv"
        write_enriched(source, [{"canonical_id": 1}, {"canonical_id": 1}])
        with self.assertRaisesRegex(ValueError, "Duplicate canonical_id"):
            app.build_index(source, self.root / "duplicate-index")

    def test_spark_tsv_roundtrip(self):
        root = self.root / "spark"
        (root / "tsv").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "not complete"):
            list(app.read_records(root))
        (root / "tsv/_SUCCESS").touch()
        types = {"canonical_id": "string", "description": "string", "wiki_common_names": {"type": "array"},
                 "wiki_full_text": "string", "wiki_page_id": "long"}
        (root / "schema.json").write_text(json.dumps({"fields": [dict(name=k, type=v) for k, v in types.items()]}))
        values = ["1", "A\tB\nC\\nD", json.dumps(["Žltý kvet", "rose"]), "Omit the entire body", None]
        escaped = [r"\N" if value is None else value.replace("\\", "\\\\").replace("\t", r"\t").replace("\n", r"\n")
                   for value in values]
        (root / "tsv/part-00000.csv").write_text("\t".join(types) + "\n" + "\t".join(escaped) + "\n", encoding="utf-8")
        row = list(app.read_records(root))[0]
        self.assertEqual(row["description"], values[1])
        self.assertEqual(row["wiki_common_names"], ["Žltý kvet", "rose"])
        self.assertIsNone(row["wiki_page_id"])
        self.assertNotIn("wiki_full_text", row)


if __name__ == "__main__":
    unittest.main()
