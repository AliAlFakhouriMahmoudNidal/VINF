"""Run with python -m unittest -v test_extract_tsv.py (standard library only)."""

import contextlib
import copy
import csv
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import extract_tsv as extractor


ROOT = Path(__file__).resolve().parent


def page(plant_id="10", name="Camellia japonica", description="A shrub with <b>white</b> flowers.", aliases=None):
    if aliases is None:
        aliases = ["common camellia", "Japanese rose", "Camellia hozanensis", name]
    metadata = json.dumps({
        "@type": "Taxon", "alternateName": aliases,
        "description": "Find help & information from the RHS",
        "parentTaxon": {"taxonRank": "genus", "name": "Camellia", "parentTaxon": {
            "name": "Theaceae", "taxonRank": "family",
        }},
    })
    return f'''<!DOCTYPE html><html><head>
      <title>{name} | common camellia</title>
      <link href="https://www.rhs.org.uk/plants/{plant_id}/plant/details" rel="canonical">
      <script type="application/ld+json">{metadata}</script>
      <style>advertising text</style></head><body>
      <h1><i>{name}</i></h1>
      <main class="plant-profile__main">
        <p class="plant-profile__names-text">Japanese rose</p>
        <span class="plant-profile__type plant-profile__type--6">Shrubs</span>
        <p data-astro-cid-example>{description}</p>
        <div class="cards">
          <h2>Position</h2><div class="card__bottom">Full sun, Partial shade</div>
          <h2>Soil Types</h2><div class="card__bottom">Clay, Loam, Sand</div>
          <h2>Max Height</h2><div class="card__bottom"><p>8 metres</p></div>
        </div>
      </main>
      <dl><dt>Family</dt><dd>Theaceae</dd>
        <dt>Foliage</dt><dd>Evergreen</dd>
        <dt>Habit</dt><dd>Bushy, Columnar upright</dd>
        <dt>Moisture</dt><dd>Moist but well\u2013drained or Well\u2013drained</dd>
        <dt>Exposure</dt><dd>Exposed or Sheltered</dd>
        <dt>Plant Range</dt><dd>China, Japan, Korea</dd></dl>
      <dialog>Unrelated modal text</dialog><footer>Join the RHS</footer>
    </body></html>'''


class ExtractorTests(unittest.TestCase):
    def test_rich_profile_and_escaped_metadata(self):
        aliases = ['Camellia [XXL] "Snow"', "rose \u00d7 flower", "name, with comma", "ROSE \u00d7 FLOWER"]
        record = extractor.extract_plant(page(aliases=aliases))
        self.assertEqual(record["primary_name"], "Camellia japonica")
        self.assertEqual(record["alternative_names"], aliases[:3])
        self.assertEqual(record["genus"], "Camellia")
        self.assertEqual(record["family"], "Theaceae")
        self.assertEqual(record["description"], "A shrub with white flowers.")
        self.assertEqual(record["moisture"], ["Moist but well-drained", "Well-drained"])
        self.assertEqual(record["habit"], ["Bushy", "Columnar upright"])
        self.assertEqual(record["exposure"], ["Exposed", "Sheltered"])
        self.assertEqual(record["soil_types"], ["Clay", "Loam", "Sand"])
        self.assertEqual(record["position"], ["Full sun", "Partial shade"])
        self.assertEqual(record["plant_types"], ["Shrubs"])
        self.assertEqual(record["plant_range"], "China, Japan, Korea")

    def test_sparse_profile_does_not_use_names_or_seo_as_description(self):
        html = page(description="")
        html = re.sub(r'<div class="cards">.*?</main>', "</main>", html, flags=re.S)
        record = extractor.extract_plant(html)
        self.assertEqual(record["description"], "")
        self.assertEqual(record["position"], [])
        self.assertEqual(record["alternative_names"], ["common camellia", "Japanese rose", "Camellia hozanensis"])

    def test_inline_text_and_list_round_trip(self):
        self.assertEqual(extractor.clean_text('semi-<a>hardwood</a><br>cuttings &amp; seeds'),
                         "semi-hardwood cuttings & seeds")
        values = ["A|B\\C", "trailing\\", "\u00d7 rose", '"quoted"']
        cell = extractor.serialize_value(values)
        decoded = [re.sub(r"\\([\\|])", r"\1", item) for item in re.findall(r"(?:\\.|[^|])+", cell)]
        self.assertEqual(decoded, values)
        self.assertEqual(extractor.serialize_value("one\ttwo\nthree &lt;four&gt;"), "one two three &lt;four&gt;")

    def test_canonical_values_win_in_both_orders_and_aliases_fill_gaps(self):
        canonical = extractor.extract_plant(page(description=""))
        alias = extractor.extract_plant(page(name="Old botanical name", description="Useful description"))
        alias["genus"] = "OldGenus"
        alias["moisture"] = ["Poorly-drained"]
        for order in [(canonical, alias), (alias, canonical)]:
            with self.subTest(first=order[0]["primary_name"]):
                plants = {}
                for record in order:
                    filename = "10.html" if record is canonical else "20.html"
                    extractor.merge_plant(plants, copy.deepcopy(record), Path(filename))
                self.assertEqual(len(plants), 1)
                merged = plants["10"]
                self.assertEqual(merged["primary_name"], "Camellia japonica")
                self.assertEqual(merged["genus"], "Camellia")
                self.assertEqual(merged["moisture"], ["Moist but well-drained", "Well-drained"])
                self.assertEqual(merged["description"], "Useful description")
                self.assertIn("Old botanical name", merged["alternative_names"])
                self.assertNotIn("Camellia japonica", merged["alternative_names"])
                self.assertTrue(merged["_canonical"])

    def test_cli_merges_files_and_logs_bad_input(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            folder = Path(folder)
            (folder / "10.html").write_text(page(), encoding="utf-8")
            (folder / "20.html").write_text(page(name="Old name"), encoding="utf-8")
            (folder / "30.html").write_bytes(b"\xff")
            (folder / "40.html").mkdir()
            output = folder / "plants.tsv"
            results = []
            for readers in [1, 4]:
                result = subprocess.run([sys.executable, "-B", str(ROOT / "extract_tsv.py"),
                                         "--input", str(folder), "--output", str(output),
                                         "--readers", str(readers)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                results.append(output.read_bytes())
            self.assertEqual(results[0], results[1])
            with output.open(encoding="utf-8", newline="") as source:
                rows = list(csv.reader(source, delimiter="\t", quoting=csv.QUOTE_NONE))
            self.assertEqual(rows[0], list(extractor.COLUMNS))
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(rows[1]), 15)
            self.assertIn("Old name", rows[1][3])
            self.assertIn("30.html\terror\t", output.with_suffix(".errors.tsv").read_text(encoding="utf-8"))
            self.assertIn("40.html\terror\t", output.with_suffix(".errors.tsv").read_text(encoding="utf-8"))
            self.assertFalse(output.with_suffix(".tsv.tmp").exists())

            # A capped threaded run must exclude later aliases and bad files.
            result = subprocess.run([sys.executable, "-B", str(ROOT / "extract_tsv.py"),
                                     "--input", str(folder), "--output", str(output),
                                     "--readers", "4", "--limit", "1"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Parsing 1/1 (100.00%)", result.stdout)
            self.assertNotIn("Old name", output.read_text(encoding="utf-8"))
            self.assertEqual(output.with_suffix(".errors.tsv").read_text(encoding="utf-8"),
                             "source_file\tlevel\tmessage\n")

    def test_all_bad_input_preserves_existing_output(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            folder = Path(folder)
            (folder / "1.html").write_text("Not a plant", encoding="utf-8")
            output = folder / "plants.tsv"
            output.write_text("previous complete result", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                extractor.run(folder, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous complete result")

    def test_interrupted_export_preserves_existing_output(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            output = Path(folder) / "plants.tsv"
            output.write_text("previous complete result", encoding="utf-8")
            plants = {"10": extractor.extract_plant(page())}
            with contextlib.redirect_stdout(io.StringIO()), patch.object(
                extractor, "serialize_value", side_effect=KeyboardInterrupt,
            ), self.assertRaises(KeyboardInterrupt):
                extractor.write_tsv(plants, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous complete result")
            self.assertFalse(output.with_suffix(".tsv.tmp").exists())


if __name__ == "__main__":
    unittest.main()
