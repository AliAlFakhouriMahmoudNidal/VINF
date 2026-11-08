import argparse
import json
import sys
from pathlib import Path

from pyspark import StorageLevel
from pyspark.sql import SparkSession, functions as F

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))

from extract_tsv import COLUMNS
from spark_data import KEY_SCHEMA, WIKI_SCHEMA, prepare_output, read_tsv, write_tsv
from wiki_utils import parse_name, rhs_aliases, rhs_name_keys, wiki_name_keys


SHARED_FIELDS = {"genus", "family", "description", "plant_range"}
WIKI_TEXT_FIELDS = {"introduction", "habitat", "uses", "cultivation"}


def sourced_values(field, sources):
    value_key = "value" if field in {"genus", "family"} else "text"
    entries = []
    for source, column_name in sources:
        value = F.col(column_name)
        entry = F.struct(F.lit(source).alias("source"), value.alias(value_key))
        entries.append(F.when(value.isNotNull() & (value != ""), entry))
    return F.filter(F.array(*entries), lambda entry: entry.isNotNull())


def merge_fields(joined):
    decode_names = F.udf(rhs_aliases, "array<string>")
    empty_names = F.array().cast("array<string>")
    names = F.array_distinct(F.concat(
        decode_names("alternative_names"),
        F.coalesce("wiki_scientific_synonyms", empty_names),
        F.coalesce("wiki_common_names", empty_names),
    ))
    columns = []
    for field in COLUMNS:
        if field == "alternative_names":
            columns.append(names.alias("names"))
            continue
        if field in SHARED_FIELDS:
            value = sourced_values(field, [("rhs", field), ("wiki", "wiki_" + field)])
        else:
            value = F.col(field)
        columns.append(value.alias(field))

    columns.append(F.col("candidate_page_ids"))
    for field in WIKI_SCHEMA:
        name = field.name
        if name in SHARED_FIELDS or name in {"scientific_synonyms", "common_names", "redirect_aliases"}:
            continue
        value = F.col("wiki_" + name)
        if name in WIKI_TEXT_FIELDS:
            value = sourced_values(name, [("wiki", "wiki_" + name)])
        output_name = "wiki_" + name if name in {"page_id", "url"} else name
        columns.append(value.alias(output_name))
    return joined.select(*columns)


def read_rhs(spark, path):
    frame = (spark.read.option("header", True).option("sep", "\t").option("quote", "")
             .option("mode", "FAILFAST").option("nullValue", "\u0000")
             .csv(path))
    if frame.columns != list(COLUMNS):
        raise ValueError("RHS columns do not match the existing 15-column extraction contract")
    frame = frame.fillna("")
    if frame.filter(F.col("canonical_id") == "").take(1):
        raise ValueError("RHS canonical_id must not be empty")
    if frame.groupBy("canonical_id").count().filter(F.col("count") > 1).take(1):
        raise ValueError("RHS canonical_id must be unique")
    return frame


def combine(rhs, wiki):
    wiki_fields = WIKI_SCHEMA.fieldNames()
    wiki = wiki.select(*wiki_fields)
    if wiki.groupBy("page_id").count().filter(F.col("count") > 1).take(1):
        raise ValueError("Wikipedia page_id must be unique")

    genera = set()
    for row in wiki.select("genus").distinct().toLocalIterator():
        identity = parse_name(row.genus)
        if identity and identity["rank"] == "genus":
            genera.add(identity["name"].casefold())
    known_genera = wiki.sparkSession.sparkContext.broadcast(genera)

    rhs_keys_udf = F.udf(rhs_name_keys, KEY_SCHEMA)
    wiki_keys_udf = F.udf(
        lambda name, rank, synonyms, redirects, common:
        wiki_name_keys(name, rank, synonyms, redirects, common, known_genera.value),
        KEY_SCHEMA,
    )

    rhs_names = rhs.select(
        "canonical_id",
        F.explode(rhs_keys_udf("primary_name", "alternative_names")).alias("name"),
    )
    rhs_keys = rhs_names.select(
        "canonical_id", F.col("name.key").alias("key"),
    )

    wiki_names = wiki.select(
        "page_id",
        F.explode(wiki_keys_udf(
            "scientific_name", "taxonomic_rank", "scientific_synonyms",
            "redirect_aliases", "common_names",
        )).alias("name"),
    )
    wiki_keys = wiki_names.select(
        "page_id", F.col("name.key").alias("key"),
    )

    candidates = rhs_keys.join(wiki_keys, "key").distinct()
    matches = candidates.groupBy("canonical_id").agg(
        F.sort_array(F.collect_set("page_id")).alias("candidate_page_ids"),
    )

    single_match = F.size("candidate_page_ids") == 1
    matches = matches.withColumn(
        "matched_page_id", F.when(single_match, F.element_at("candidate_page_ids", 1)),
    )
    matches = matches.withColumn(
        "match_status", F.when(single_match, "matched").otherwise("ambiguous"),
    )

    result = rhs.join(matches, "canonical_id", "left").fillna({"match_status": "unmatched"})
    result = result.withColumn(
        "candidate_page_ids", F.coalesce("candidate_page_ids", F.array().cast("array<long>")),
    )

    wiki_columns = wiki.select(*[F.col(name).alias("wiki_" + name) for name in wiki_fields])
    result = result.join(wiki_columns, result.matched_page_id == wiki_columns.wiki_page_id, "left")
    return result.select(*COLUMNS, "match_status", "candidate_page_ids",
                         *["wiki_" + name for name in wiki_fields])


def join(spark, rhs_path, wiki_path, output):
    output = prepare_output(output)
    report = {"rhs_input": rhs_path, "wiki_input": wiki_path,
              "identity_policy": "exact_only_no_parent_fallback"}
    rhs = read_rhs(spark, rhs_path).persist(StorageLevel.DISK_ONLY)
    wiki = read_tsv(spark, wiki_path, WIKI_SCHEMA)
    result = combine(rhs, wiki).persist(StorageLevel.DISK_ONLY)

    rhs_count = rhs.count()
    if result.count() != rhs_count or result.select("canonical_id").distinct().count() != rhs_count:
        raise AssertionError("Join changed RHS row count or uniqueness")
    if rhs.exceptAll(result.select(*COLUMNS)).take(1) or result.select(*COLUMNS).exceptAll(rhs).take(1):
        raise AssertionError("Join changed original RHS values")

    statuses = dict.fromkeys(["matched", "unmatched", "ambiguous"], 0)
    status_counts = result.groupBy("match_status").count().collect()
    statuses.update({row.match_status: row["count"] for row in status_counts})
    matched_pages = result.filter(F.col("match_status") == "matched").select("wiki_page_id").distinct()
    report.update(rhs_records=rhs_count, wikipedia_records=wiki.count(), match_counts=statuses,
                  distinct_wikipedia_pages_joined=matched_pages.count(),
                  rhs_values_preserved=True)

    write_tsv(merge_fields(result), output)
    report["stage"] = "complete"
    report["output"] = str(output)
    result.unpersist()
    rhs.unpersist()
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rhs", default=str(BASE_DIR.parent / "data/output/plants-extracted.tsv"), help="Existing HTML export TSV")
    parser.add_argument("--wiki", default=str(BASE_DIR.parent / "data/output/plants-extracted-wiki.tsv"), help="TSV from extract_wiki.py")
    parser.add_argument("--output", default=str(BASE_DIR.parent / "data/output/plants-enriched.tsv"), help="Output TSV file")
    args = parser.parse_args()
    output = prepare_output(args.output)
    spark = SparkSession.builder.appName("VINF exact RHS Wikipedia join").getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    spark.sparkContext.addPyFile(str(BASE_DIR / "wiki_utils.py"))
    spark.sparkContext.addPyFile(str(BASE_DIR / "spark_data.py"))
    try:
        join(spark, args.rhs, args.wiki, output)
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
