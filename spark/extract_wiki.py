import argparse
import json
import re
from collections import Counter
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from pyspark import StorageLevel
from pyspark.sql import SparkSession, functions as F, types as T

BASE_DIR = Path(__file__).resolve().parent

from spark_data import (ARTICLE_SCHEMA, RAW_SCHEMA, STAGE_SCHEMA, WIKI_SCHEMA,
                        field_availability, prepare_output, read_xml_rows, write_tsv)
from wiki_utils import BOX_RE, extract_article, normalize_title, parse_taxonomy


def stage_pages(rows):
    counts = Counter()
    for row in rows:
        counts["pages_read"] += 1
        title = row.title or ""
        text = row.text or ""
        kind = None
        redirect = row.redirect_title
        if row.ns == 10 and title.startswith("Template:Taxonomy/"):
            kind = "taxonomy"
        elif row.ns == 0:
            counts["article_namespace_pages"] += 1
            redirect_match = re.match(r"\s*#redirect\s*\[\[([^\]|]+)", text, re.I)
            if redirect or redirect_match:
                redirect = redirect_match[1] if redirect_match else redirect
                kind = "redirect"
                text = ""
            elif row.model in (None, "wikitext") and BOX_RE.search(text):
                kind = "candidate"
        if kind:
            counts[kind] += 1
            yield (kind, row.id, title, redirect, text, None)
    yield ("stats", None, None, None, None, json.dumps(counts))


def resolve_plant_redirects(spark, staged, plants, work):
    normalize_title_udf = F.udf(normalize_title, T.StringType())
    redirects = staged.filter(F.col("kind") == "redirect").filter(~F.col("redirect_title").contains("#"))
    links = redirects.select(
        normalize_title_udf("title").alias("alias_title"),
        normalize_title_udf("redirect_title").alias("target"),
    ).dropDuplicates().persist(StorageLevel.DISK_ONLY)

    plant_titles = plants.select(normalize_title_udf("article_title").alias("alias_title"), "page_id")
    frontier = plant_titles.distinct()
    visited = frontier
    alias_paths = []
    reached_limit = True
    for hop in range(1, 33):
        redirect_targets = links.alias("redirect").join(
            frontier.alias("resolved"), F.col("redirect.target") == F.col("resolved.alias_title"),
        )
        candidate_aliases = redirect_targets.select(F.col("redirect.alias_title"), F.col("resolved.page_id"))
        found = candidate_aliases.join(visited.select("alias_title"), "alias_title", "left_anti").distinct()
        destination = str(work / f"redirects-{hop:02d}")
        found.write.parquet(destination)
        frontier = spark.read.parquet(destination)
        if not frontier.take(1):
            reached_limit = False
            break
        alias_paths.append(destination)
        visited = plant_titles.unionByName(spark.read.parquet(*alias_paths))

    links.unpersist()
    if alias_paths:
        aliases = spark.read.parquet(*alias_paths)
    else:
        aliases = spark.createDataFrame([], "alias_title string, page_id long")
    grouped = aliases.groupBy("page_id").agg(
        F.sort_array(F.collect_set("alias_title")).alias("redirect_aliases"),
    )
    return grouped, aliases.count(), reached_limit


def extract(spark, inputs, output, limit_pages=None):
    output = prepare_output(output)
    with TemporaryDirectory(prefix="vinf-wiki-") as directory:
        return extract_pages(spark, inputs, Path(directory), output, limit_pages)


def extract_pages(spark, inputs, scratch, output, limit_pages):
    report = {"inputs": inputs, "sample_limit_pages": limit_pages,
              "run_scope": "bounded_sample" if limit_pages else "all_supplied_inputs",
              "input_reader": "spark_workers_native_bzip2_streaming_xml"}
    work = scratch / "_work"
    work.mkdir()
    raw_rows = read_xml_rows(spark, inputs)
    if limit_pages:
        bounded = raw_rows.mapPartitions(lambda rows: islice(rows, limit_pages))
        raw_rows = spark.createDataFrame(bounded, RAW_SCHEMA).limit(limit_pages).rdd
    staged_path = str(work / "pages")
    print("Reading XML and staging taxonomy, redirects and taxon candidates...", flush=True)
    staged = spark.createDataFrame(raw_rows.mapPartitions(stage_pages), STAGE_SCHEMA)
    staged.write.partitionBy("kind").parquet(staged_path)
    staged = spark.read.parquet(staged_path)

    counts = Counter()
    statistics = staged.filter(F.col("kind") == "stats").select("stats_json")
    for row in statistics.toLocalIterator():
        counts.update(json.loads(row.stats_json))
    report["input_counts"] = dict(counts)

    taxonomy = {}
    taxonomy_pages = staged.filter(F.col("kind") == "taxonomy").select("title", "text", "redirect_title")
    for row in taxonomy_pages.toLocalIterator():
        key, node = parse_taxonomy(row.title, row.text or "", row.redirect_title)
        taxonomy[key] = node
    print(f"Staged {counts['candidate']:,} taxon candidates; loaded {len(taxonomy):,} taxonomy templates.", flush=True)

    taxonomy_broadcast = spark.sparkContext.broadcast(taxonomy)
    parse_article_udf = F.udf(
        lambda title, text: extract_article(title, text or "", taxonomy_broadcast.value),
        ARTICLE_SCHEMA,
    )
    candidates = staged.filter(F.col("kind") == "candidate")
    parsed = candidates.withColumn("plant", parse_article_udf("title", "text")).select(
        "page_id", F.col("title").alias("article_title"), "plant.*",
    )
    parsed_path = str(work / "parsed")
    parsed.write.parquet(parsed_path)
    parsed = spark.read.parquet(parsed_path)

    status_counts = parsed.groupBy("extraction_status").count().collect()
    statuses = {row.extraction_status: row["count"] for row in status_counts}
    report["extraction_counts"] = statuses
    report["unresolved_extraction_count"] = sum(value for key, value in statuses.items() if key.startswith("unresolved"))

    plants = parsed.filter(F.col("extraction_status") == "plant").drop("extraction_status")
    if plants.groupBy("page_id").count().filter(F.col("count") > 1).take(1):
        raise ValueError("Duplicate plant page IDs in the input; use one dump snapshot without overlapping files")

    print("Resolving redirects to plant pages...", flush=True)
    aliases, aliases_count, reached_limit = resolve_plant_redirects(spark, staged, plants, work)

    article_url_udf = F.udf(
        lambda title: "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe=""),
        T.StringType(),
    )
    plants = plants.join(aliases, "page_id", "left").withColumn(
        "redirect_aliases", F.coalesce("redirect_aliases", F.array().cast("array<string>")),
    )
    plants = plants.withColumn("url", article_url_udf("article_title"))
    plants = plants.select(*WIKI_SCHEMA.fieldNames()).persist(StorageLevel.DISK_ONLY)

    report["plant_pages"] = plants.count()
    report["plant_redirect_aliases"] = aliases_count
    report["redirect_depth_limit_reached"] = reached_limit
    report["field_availability"] = field_availability(plants)

    print(f"Writing {output}...", flush=True)
    write_tsv(plants, output)
    plants.unpersist()
    taxonomy_broadcast.unpersist()
    report["stage"] = "complete"
    report["output"] = str(output)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", nargs="+", help="Local XML archives; defaults to all supplied Wikipedia archives")
    parser.add_argument("--output", default=str(BASE_DIR.parent / "data/output/plants-extracted-wiki.tsv"), help="Output TSV file")
    parser.add_argument("--limit-pages", type=int, help="Bounded validation sample; omitted for the full supplied input")
    args = parser.parse_args()
    if args.limit_pages is not None and args.limit_pages <= 0:
        parser.error("--limit-pages must be positive")
    inputs = args.input or [str(BASE_DIR.parent / "data/input/wikipedia/*.xml.bz2")]
    output = prepare_output(args.output)
    spark = SparkSession.builder.appName("VINF Wikipedia plant extraction").getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    spark.sparkContext.addPyFile(str(BASE_DIR / "wiki_utils.py"))
    spark.sparkContext.addPyFile(str(BASE_DIR / "spark_data.py"))
    try:
        extract(spark, inputs, output, args.limit_pages)
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
