"""Real Spark integration check, separate from Windows unittest discovery."""

import argparse
import bz2
import csv
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.sax.saxutils import escape, quoteattr

from pyspark.sql import SparkSession, functions as F

BASE_DIR = Path(__file__).resolve().parents[1] / "spark"
sys.path[:0] = [str(BASE_DIR.parent), str(BASE_DIR)]

from extract_tsv import COLUMNS
from extract_wiki import extract, resolve_plant_redirects
from join_plants import SHARED_FIELDS, combine, join, merge_fields, read_rhs
from spark_data import ARTICLE_SCHEMA, WIKI_SCHEMA, read_tsv, read_xml_rows, tsv_unescape, write_tsv
from test_wiki_utils import ROSEMARY, TAXONOMY


def xml_page(page_id, title, text, namespace=0, redirect=None):
    red = "<redirect title=" + quoteattr(redirect) + "/>" if redirect else ""
    return (f"<page><title>{escape(title)}</title><ns>{namespace}</ns><id>{page_id}</id>{red}"
            f"<revision><model>wikitext</model>"
            f"<text xml:space=\"preserve\">{escape(text)}</text></revision></page>")


def check(spark, directory):
    csv.field_size_limit(2**31 - 1)  # Long descriptions can exceed CSV's 128 KiB default.
    long_body = "Description čľá, with a tab\tand a line break.\n" * 6000 + "END_OF_LONG_DESCRIPTION"
    pages = [
        xml_page(1, "Rosemary", ROSEMARY.replace("== Distribution and habitat ==",
                 long_body + "\n== Distribution and habitat ==")),
        xml_page(2, "Almond", "{{Speciesbox|genus=Prunus|species=amygdalus|synonyms={{Species list|Prunus dulcis|Mill.}}}}Almond tree."),
        xml_page(3, "Peppermint", "{{Speciesbox|genus=Mentha|species=× piperita}}Peppermint."),
        xml_page(4, "Olive", "{{Infraspeciesbox|genus=Olea|species=europaea|subspecies=europaea}}Olive."),
        xml_page(5, "Wye rosemary", "{{Infobox cultivar|genus=Salvia|species=Salvia rosmarinus|cultivar='Wye'}}An exact cultivar."),
        xml_page(6, "Proboscidea", "{{Automatic taxobox|taxon=Proboscidea}}Elephants."),
        xml_page(7, "Unknown plant", "{{Speciesbox|taxon=Unknown unknown}}Unknown."),
        xml_page(8, "Talk:Rosemary", ROSEMARY, namespace=1),
        xml_page(9, "Rosemary disambiguation", "{{disambiguation}}" + ROSEMARY),
        xml_page(10, "Rosmarinus antiquus", "#REDIRECT [[Rosemary]]", redirect="Rosemary"),
        xml_page(11, "Rosmarinus vetustus", "#REDIRECT [[Rosmarinus antiquus]]", redirect="Rosmarinus antiquus"),
        xml_page(12, "Rosmarinus cyclicus", "#REDIRECT [[Rosmarinus circularis]]", redirect="Rosmarinus circularis"),
        xml_page(13, "Rosmarinus circularis", "#REDIRECT [[Rosmarinus cyclicus]]", redirect="Rosmarinus cyclicus"),
        xml_page(14, "Rosmarinus missingus", "#REDIRECT [[Missing]]", redirect="Missing"),
        xml_page(15, "Salvia rosmarinus 'Section cultivar'", "#REDIRECT [[Rosemary#Cultivars]]", redirect="Rosemary"),
        xml_page(16, "Conflicting mint", "{{Speciesbox|genus=Mentha|species=spicata|synonyms={{Species list|Prunus dulcis|L.}}}}Conflict fixture."),
        xml_page(17, "List of plants", ROSEMARY),
        xml_page(18, "Proboscidea (plant)", "{{Automatic taxobox|taxon=Proboscidea (plant)}}A plant genus."),
    ]
    latin_ranks = {"family": "familia", "order": "ordo", "kingdom": "regnum"}
    for index, (name, node) in enumerate(TAXONOMY.items(), 100):
        fields = "|".join(key + "=" + (latin_ranks.get(value, value) if key == "rank" else value)
                          for key, value in node.items())
        pages.append(xml_page(index, "Template:Taxonomy/" + name,
                              "<noinclude>{{High-use}}</noinclude>{{Don't edit this line {{{machine code|}}}|" + fields + "}}", namespace=10))
    source = directory / "fixture.xml.bz2"
    with bz2.open(source, "wt", encoding="utf-8") as file:
        file.write('<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">' + "\n".join(pages) + "</mediawiki>")
    wiki_output = directory / "wiki.tsv"
    report = extract(spark, [str(source)], wiki_output)
    assert report["plant_pages"] == 7, report
    assert report["extraction_counts"]["non_plant"] == 1
    assert report["unresolved_extraction_count"] == 1
    wiki = read_tsv(spark, wiki_output, WIKI_SCHEMA).cache()
    rosemary = wiki.filter(F.col("page_id") == 1).first()
    assert set(rosemary.redirect_aliases) == {"Rosmarinus antiquus", "Rosmarinus vetustus"}
    assert "full_text" not in wiki.columns
    assert "redirect_aliases" not in ARTICLE_SCHEMA.fieldNames()
    assert not {"subspecies", "variety", "cultivar"}.intersection(wiki.columns)
    assert "Named selections" in rosemary.cultivation
    assert len(rosemary.description) > 200000
    assert rosemary.description.endswith("END_OF_LONG_DESCRIPTION")
    assert rosemary.description == " ".join(rosemary.description.split())
    assert rosemary.introduction == " ".join(rosemary.introduction.split())
    assert "More information" not in rosemary.description
    assert rosemary.family == "Lamiaceae"
    specifications = [
        ("1", "Salvia rosmarinus (Ro)", "rosemary", "matched", 1),
        ("2", "Prunus dulcis", "almond|Prunus amygdalus", "ambiguous", None),
        ("3", "Mentha x piperita", "peppermint", "matched", 3),
        ("4", "Olea europaea (F)", "olive", "unmatched", None),
        ("5", "Olea europaea subsp. europaea", "", "matched", 4),
        ("6", "Salvia rosmarinus 'Wye'", "Salvia rosmarinus|rosemary", "matched", 5),
        ("7", "Salvia rosmarinus 'Other'", "Salvia rosmarinus", "unmatched", None),
        ("8", "Rosmarinus vetustus", "", "matched", 1),
        ("9", "Rosmarinus cyclicus", "", "unmatched", None),
        ("10", "Agapanthus africanus hybrid", "Salvia rosmarinus", "unmatched", None),
        ("11", "Proboscidea", "", "matched", 18),
    ]
    rhs_path = directory / "rhs.tsv"
    original = {}
    with rhs_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS, delimiter="\t", quoting=csv.QUOTE_NONE, lineterminator="\n")
        writer.writeheader()
        for page_id, name, aliases, _, _ in specifications:
            row = dict.fromkeys(COLUMNS, "")
            row.update(canonical_id=page_id, primary_name=name, alternative_names=aliases,
                       source_url="https://rhs.example/" + page_id, description=r"Literal \n and \N; quotes ' and čľá")
            if page_id == "1":
                row.update(genus="Salvia", family="RHS family", plant_range="RHS range")
            writer.writerow(row)
            original[page_id] = row
    enriched = directory / "enriched.tsv"
    join_report = join(spark, str(rhs_path), str(wiki_output), enriched)
    expected = merge_fields(combine(read_rhs(spark, str(rhs_path)), wiki))
    # The original HTML export uses empty strings for missing RHS values.
    rhs_scalar_fields = [field for field in COLUMNS if field not in SHARED_FIELDS and field != "alternative_names"]
    restored = read_tsv(spark, enriched, expected.schema).fillna("", subset=rhs_scalar_fields).cache()
    assert not expected.exceptAll(restored).take(1)
    assert not restored.exceptAll(expected).take(1)
    actual = {row.canonical_id: row.asDict(recursive=True) for row in restored.collect()}
    assert all("wiki_full_text" not in row for row in actual.values())
    assert len(actual) == len(original)
    for page_id, _, _, status, wiki_id in specifications:
        assert {key: actual[page_id][key] for key in rhs_scalar_fields} == {
            key: original[page_id][key] for key in rhs_scalar_fields
        }
        for field in SHARED_FIELDS:
            value_key = "value" if field in {"genus", "family"} else "text"
            rhs_entries = [entry[value_key] for entry in actual[page_id][field] if entry["source"] == "rhs"]
            assert rhs_entries == ([original[page_id][field]] if original[page_id][field] else [])
            if status != "matched":
                assert all(entry["source"] == "rhs" for entry in actual[page_id][field])
        candidates = actual[page_id]["candidate_page_ids"]
        actual_status = "matched" if len(candidates) == 1 else "ambiguous" if candidates else "unmatched"
        assert actual_status == status, (page_id, actual[page_id])
        assert actual[page_id]["wiki_page_id"] == wiki_id
    assert actual["2"]["candidate_page_ids"] == [2, 16]
    assert len(restored.columns) == 26
    assert not {"alternative_names", "scientific_synonyms", "common_names"}.intersection(restored.columns)
    assert not {"match_status", "match_evidence", "redirect_aliases"}.intersection(restored.columns)
    assert actual["1"]["family"] == [
        {"source": "rhs", "value": "RHS family"}, {"source": "wiki", "value": "Lamiaceae"},
    ]
    assert [entry["source"] for entry in actual["1"]["description"]] == ["rhs", "wiki"]
    assert actual["1"]["introduction"] == [{"source": "wiki", "text": rosemary.introduction}]
    assert actual["4"]["introduction"] == []
    assert actual["4"]["names"] == ["olive"]
    assert actual["1"]["names"] == list(dict.fromkeys([
        "rosemary", *rosemary.scientific_synonyms, *rosemary.common_names,
    ]))
    assert join_report["distinct_wikipedia_pages_joined"] == 5
    # The same Almond synonym is accepted when no conflicting entity exists.
    almond = combine(read_rhs(spark, str(rhs_path)).filter(F.col("canonical_id") == "2"),
                     wiki.filter(F.col("page_id") != 16)).first()
    assert "wiki_full_text" not in almond.asDict()
    assert almond.match_status == "matched" and almond.wiki_page_id == 2

    # Actual line breaks, literal escape sequences, nulls, lists and Unicode.
    sample = spark.createDataFrame([
        (1, "line 1\nline 2\tčľá", ["name\nline", r"literal \n"], None),
        (2, r"\N", [], None),
    ], "id long, body string, names array<string>, optional string")
    output = directory / "roundtrip.tsv"
    write_tsv(sample, output)
    rows = output.read_text(encoding="utf-8").splitlines()[1:]
    assert all(line.endswith("\t") for line in rows)
    reread = read_tsv(spark, output, sample.schema)
    assert not sample.exceptAll(reread).take(1)
    assert not reread.exceptAll(sample).take(1)
    before = output.read_bytes()
    try:
        write_tsv(sample, output)
    except FileExistsError:
        pass
    else:
        raise AssertionError("Existing output was overwritten")
    assert output.read_bytes() == before
    empty_output = directory / "empty.tsv"
    write_tsv(sample.limit(0), empty_output)
    assert read_tsv(spark, empty_output, sample.schema).count() == 0
    assert tsv_unescape(r"\N") == r"\N"
    assert tsv_unescape("") is None
    for extra_column in ["subspecies", "variety", "cultivar", "full_text"]:
        old_output = directory / f"old-{extra_column}.tsv"
        old_output.write_text("\t".join([*wiki.columns, extra_column]) + "\n", encoding="utf-8")
        try:
            read_tsv(spark, old_output, WIKI_SCHEMA)
        except ValueError as error:
            assert "columns" in str(error)
        else:
            raise AssertionError("An old export header was accepted")
    try:
        read_xml_rows(spark, [str(source), str(source)])
    except ValueError as error:
        assert "Overlapping" in str(error)
    else:
        raise AssertionError("Duplicate input paths were accepted")
    try:
        combine(read_rhs(spark, str(rhs_path)), wiki.unionByName(wiki))
    except ValueError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("Duplicate Wikipedia IDs were accepted")
    for output in [wiki_output, enriched]:
        assert output.is_file()
        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines.count(lines[0]) == 1, "TSV header must occur exactly once"
    print("SPARK_PIPELINE_FIXTURES_OK", flush=True)
    return {
        "wiki_schema": wiki.schema.jsonValue(),
        "wiki_rows": [row.asDict(recursive=True) for row in wiki.orderBy("page_id").collect()],
        "joined_schema": restored.schema.jsonValue(),
        "joined_rows": [row.asDict(recursive=True) for row in restored.orderBy("canonical_id").collect()],
        "extraction_report": {key: value for key, value in report.items() if key not in {"inputs", "output"}},
        "join_report": {key: value for key, value in join_report.items()
                        if key not in {"rhs_input", "wiki_input", "output"}},
    }


def check_redirect_depth(spark, directory):
    plants = spark.createDataFrame([(1, "Plant")], "page_id long, article_title string")
    links = [("redirect", f"Alias {depth}", "Plant" if depth == 1 else f"Alias {depth - 1}")
             for depth in range(1, 34)]
    staged = spark.createDataFrame(links, "kind string, title string, redirect_title string")
    aliases, count, reached_limit = resolve_plant_redirects(spark, staged, plants, directory)
    assert reached_limit and count == 32
    assert set(aliases.first().redirect_aliases) == {f"Alias {depth}" for depth in range(1, 33)}
    print("REDIRECT_DEPTH_LIMIT_OK", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    spark = SparkSession.builder.appName("VINF two-job TSV integration").getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    for path in [BASE_DIR / "wiki_utils.py", BASE_DIR / "spark_data.py",
                 BASE_DIR / "extract_wiki.py", BASE_DIR / "join_plants.py",
                 BASE_DIR.parent / "extract_tsv.py"]:
        spark.sparkContext.addPyFile(str(path))
    try:
        with TemporaryDirectory(prefix="vinf-wiki-integration-", dir=BASE_DIR.parent / "tests") as directory:
            snapshot = check(spark, Path(directory))
            check_redirect_depth(spark, Path(directory) / "depth-limit")
            if args.snapshot:
                args.snapshot.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        spark.stop()
