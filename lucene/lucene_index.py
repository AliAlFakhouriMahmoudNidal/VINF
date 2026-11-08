import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import lucene

if lucene.getVMEnv() is None:
    lucene.initVM(vmargs=["-Djava.awt.headless=true", "-Xmx2g"])

from java.lang import Float, Long
from java.nio.file import Paths
from java.text import NumberFormat
from java.util import HashMap, Locale
from org.apache.lucene.analysis.standard import StandardAnalyzer
from org.apache.lucene.document import Document, Field, LongPoint, StoredField, StringField, TextField
from org.apache.lucene.index import DirectoryReader, IndexWriter, IndexWriterConfig
from org.apache.lucene.queryparser.flexible.standard import StandardQueryParser
from org.apache.lucene.queryparser.flexible.standard.config import PointsConfig, StandardQueryConfigHandler
from org.apache.lucene.search import IndexSearcher
from org.apache.lucene.search.similarities import BM25Similarity
from org.apache.lucene.store import FSDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from indexer import BM25_B, BM25_K1, FIELD_WEIGHTS, parse_query


BASE_DIR = Path(__file__).resolve().parent
SOURCE = BASE_DIR.parent / "data/output/plants-enriched.tsv"
INDEX = BASE_DIR.parent / "data/output/lucene-index"
TEXT_WEIGHTS = {
    **{name: weight for name, weight in FIELD_WEIGHTS.items() if name != "alternative_names"},
    "names": FIELD_WEIGHTS["alternative_names"],
    "article_title": 2,
    "scientific_name": 3,
    **dict.fromkeys(("order", "introduction", "habitat", "uses", "cultivation"), 1),
}
KEYWORD_FIELDS = ("taxonomic_rank",)
NUMBER_FIELDS = ("canonical_id", "wiki_page_id")
SOURCED_FIELDS = {
    "genus": "value", "family": "value",
    **dict.fromkeys(("description", "plant_range", "introduction", "habitat", "uses", "cultivation"), "text"),
}
JSON_FIELDS = {"names", "candidate_page_ids", *SOURCED_FIELDS}
ENRICHED_COLUMNS = (
    "canonical_id", "source_url", "primary_name", "names", "genus", "family",
    "description", "plant_range", "plant_types", "foliage", "habit", "soil_types",
    "moisture", "exposure", "position", "candidate_page_ids", "wiki_page_id",
    "article_title", "scientific_name", "taxonomic_rank", "order", "introduction",
    "habitat", "uses", "cultivation", "wiki_url",
)


def read_records(source):
    source = Path(source)
    csv.field_size_limit(2**31 - 1)
    if source.is_dir():
        root = source.parent if source.name == "tsv" else source
        if not (root / "tsv/_SUCCESS").is_file():
            raise ValueError(f"Spark TSV export is not complete: {root}")
        schema = json.loads((root / "schema.json").read_text(encoding="utf-8"))
        types = {field["name"]: field["type"] for field in schema["fields"]}
        paths = sorted((root / "tsv").glob("part-*.csv"))
        if not paths:
            raise ValueError(f"No TSV partitions in {root / 'tsv'}")
    else:
        paths, types = [source], None
    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}
    for path in paths:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
            current_format = "names" in (reader.fieldnames or ())
            if current_format:
                if tuple(reader.fieldnames) != ENRICHED_COLUMNS:
                    raise ValueError("Enriched TSV must have the current 26 columns in order")
                types = {key: {} if key in JSON_FIELDS else "string" for key in reader.fieldnames}
            elif types is None and {"candidate_page_ids", "match_evidence"} <= set(reader.fieldnames or ()):
                json_fields = {"candidate_page_ids", "match_evidence", "wiki_scientific_synonyms",
                               "wiki_common_names", "wiki_redirect_aliases"}
                types = {key: {} if key in json_fields else "string" for key in reader.fieldnames}
            for row in reader:
                if current_format and (None in row or None in row.values()):
                    raise ValueError(f"Invalid TSV row at line {reader.line_num}")
                row.pop("wiki_full_text", None)
                row.pop("full_text", None)
                if types is not None:
                    for key, value in row.items():
                        if current_format:
                            value = None if value == "" else re.sub(r"\\([nrt\\])", lambda match: escapes[match[1]], value)
                        elif value in (None, r"\N") or (value == "" and key.startswith("wiki_")):
                            value = None
                        else:
                            value = re.sub(r"\\([nrt\\])", lambda match: escapes[match[1]], value)
                        if value is not None and isinstance(types[key], dict):
                            value = json.loads(value)
                        row[key] = value
                yield row


def field_text(row, name):
    value = row.get(name)
    if not value:
        return ""
    if isinstance(value, list):
        if name in SOURCED_FIELDS:
            value = [entry[SOURCED_FIELDS[name]] for entry in value]
        return "; ".join(value)
    return str(value)


def build_index(source=SOURCE, output=INDEX):
    started = perf_counter()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    directory = FSDirectory.open(Paths.get(str(output.resolve())))
    analyzer = StandardAnalyzer()
    config = IndexWriterConfig(analyzer)
    config.setOpenMode(IndexWriterConfig.OpenMode.CREATE)
    config.setSimilarity(BM25Similarity(BM25_K1, BM25_B))
    writer = IndexWriter(directory, config)
    seen = set()
    matched = 0
    try:
        for row in read_records(source):
            identifier = int(row["canonical_id"])
            if identifier in seen:
                raise ValueError(f"Duplicate canonical_id: {identifier}")
            seen.add(identifier)
            document = Document()
            for name in TEXT_WEIGHTS:
                text = field_text(row, name)
                if text:
                    document.add(TextField(name, text, Field.Store.NO))
            for name in KEYWORD_FIELDS:
                if row.get(name):
                    document.add(StringField(name, str(row[name]).lower(), Field.Store.NO))
            for name in NUMBER_FIELDS:
                if row.get(name) not in (None, ""):
                    document.add(LongPoint(name, [int(row[name])]))
            document.add(StoredField("record_json", json.dumps(row, ensure_ascii=False)))
            writer.addDocument(document)
            matched += row.get("wiki_page_id") not in (None, "")
            if len(seen) % 25000 == 0:
                print(f"Indexed {len(seen):,} plants", flush=True)
        if not seen:
            raise ValueError("Source contains no plants")
        writer.close()
    except Exception:
        writer.rollback()
        raise
    finally:
        analyzer.close()
        directory.close()
    report = {
        "stage": "complete", "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source), "documents": len(seen), "matched_documents": matched,
        "pylucene_version": lucene.VERSION, "analyzer": "StandardAnalyzer",
        "similarity": {"name": "BM25", "k1": BM25_K1, "b": BM25_B},
        "text_fields": TEXT_WEIGHTS, "keyword_fields": KEYWORD_FIELDS,
        "numeric_fields": NUMBER_FIELDS, "stored_field": "record_json",
        "build_seconds": round(perf_counter() - started, 3),
        "index_bytes": sum(path.stat().st_size for path in output.iterdir() if path.is_file()),
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def translate_legacy(query):
    if not query.lstrip().startswith("--"):
        return query
    return " AND ".join(
        ("" if field == "phrase" else field + ":") + "(" + f" {operator.upper()} ".join(terms) + ")"
        for operator, field, terms in parse_query(query)
    )


class PlantSearch:
    def __init__(self, index=INDEX):
        self.directory = FSDirectory.open(Paths.get(str(Path(index).resolve())))
        self.reader = DirectoryReader.open(self.directory)
        self.searcher = IndexSearcher(self.reader)
        self.searcher.setSimilarity(BM25Similarity(BM25_K1, BM25_B))
        self.analyzer = StandardAnalyzer()
        self.parser = StandardQueryParser(self.analyzer)
        self.parser.getQueryConfigHandler().set(
            StandardQueryConfigHandler.ConfigurationKeys.MULTI_FIELDS,
            lucene.JArray("string")(list(TEXT_WEIGHTS)))
        boosts = HashMap()
        for name, weight in TEXT_WEIGHTS.items():
            boosts.put(name, Float(float(weight)))
        self.parser.setFieldsBoost(boosts)
        points = HashMap()
        for name in NUMBER_FIELDS:
            points.put(name, PointsConfig(NumberFormat.getIntegerInstance(Locale.ROOT), Long.class_))
        self.parser.setPointsConfigMap(points)

    def search(self, query, limit=10, mode="or"):
        if limit < 1:
            raise ValueError("Limit must be positive")
        started = perf_counter()
        operator = (StandardQueryConfigHandler.Operator.AND if mode == "and"
                    else StandardQueryConfigHandler.Operator.OR)
        self.parser.setDefaultOperator(operator)
        try:
            parsed = self.parser.parse(translate_legacy(query), None)
        except lucene.JavaError as error:
            raise ValueError(f"Invalid Lucene query: {error.getJavaException()}") from error
        hits = self.searcher.search(parsed, limit)
        stored = self.searcher.storedFields()
        results = [dict(score=hit.score, record=json.loads(stored.document(hit.doc).get("record_json")))
                   for hit in hits.scoreDocs]
        return {"query": query, "parsed_query": str(parsed), "total_hits": self.searcher.count(parsed),
                "search_ms": round((perf_counter() - started) * 1000, 3), "results": results}

    def close(self):
        self.reader.close()
        self.directory.close()
        self.analyzer.close()


def print_results(result, details=False):
    print(f"\nMatches: {result['total_hits']:,}; search: {result['search_ms']:.3f} ms")
    for rank, hit in enumerate(result["results"], 1):
        row = hit["record"]
        print(f"\n{rank}. {row['primary_name']} | ID {row['canonical_id']} | BM25 {hit['score']:.6f}")
        print(row["source_url"])
        if details:
            for key, value in row.items():
                if value and key not in ("primary_name", "canonical_id", "source_url"):
                    print(f"{key}: {value}")
        else:
            print((field_text(row, "description") or field_text(row, "introduction") or "No description")[:300])
            if row.get("wiki_url"):
                print(row["wiki_url"])


def interactive(searcher):
    print('PyLucene: help, mode and/or, details on/off, limit N; blank input exits.')
    mode, details, limit = "or", False, 10
    while True:
        try:
            query = input(f"\nQuery [Lucene, {mode.upper()}, limit {limit}]: ").strip()
        except EOFError:
            break
        if not query:
            break
        if query == "help":
            print('Examples:\n  primary_name:(abies AND alba)\n  description:"red flowers"'
                  '\n  primary_name:lavandula~1\n  canonical_id:[10 TO 20]'
                  '\n  names:"silver fir"\n  taxonomic_rank:species AND habitat:forest'
                  '\n  soil_types:clay AND NOT position:shade'
                  '\nBoolean operators use uppercase. Legacy --and--FIELD="words" also works.'
                  '\nText fields: ' + ", ".join(TEXT_WEIGHTS)
                  + '\nExact fields: ' + ", ".join(KEYWORD_FIELDS)
                  + '\nNumeric fields: ' + ", ".join(NUMBER_FIELDS))
        elif query in ("mode and", "mode or"):
            mode = query.split()[1]
        elif query in ("details on", "details off"):
            details = query.endswith("on")
        else:
            try:
                if query.startswith("limit "):
                    new_limit = int(query.split()[1])
                    if new_limit < 1:
                        raise ValueError("Limit must be positive")
                    limit = new_limit
                else:
                    print_results(searcher.search(query, limit, mode), details)
            except ValueError as error:
                print(error)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("build", "search", "interactive"), default="interactive")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--index", type=Path, default=INDEX)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--mode", choices=("and", "or"), default="or")
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "build":
        print(json.dumps(build_index(args.source, args.index), ensure_ascii=False, indent=2))
        return
    searcher = PlantSearch(args.index)
    try:
        if args.command == "interactive":
            interactive(searcher)
        else:
            if not args.query:
                parser.error("search requires a query")
            result = searcher.search(args.query, args.limit, args.mode)
            print(json.dumps(result, ensure_ascii=False, indent=2)) if args.json else print_results(result, args.details)
    finally:
        searcher.close()


if __name__ == "__main__":
    main()
