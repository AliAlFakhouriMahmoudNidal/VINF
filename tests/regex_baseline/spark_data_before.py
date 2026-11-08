import bz2
import glob
import os
import shutil
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

from pyspark.sql import Row, functions as F, types as T

from wiki_utils import ARTICLE_LIST_FIELDS, ARTICLE_TEXT_FIELDS


RAW_SCHEMA = T.StructType([
    T.StructField("id", T.LongType()), T.StructField("title", T.StringType()),
    T.StructField("ns", T.LongType()),
    T.StructField("redirect_title", T.StringType()),
    T.StructField("model", T.StringType()), T.StructField("text", T.StringType()),
])
STAGE_SCHEMA = T.StructType([
    T.StructField("kind", T.StringType()), T.StructField("page_id", T.LongType()),
    T.StructField("title", T.StringType()), T.StructField("redirect_title", T.StringType()),
    T.StructField("text", T.StringType()),
    T.StructField("stats_json", T.StringType()),
])
ARTICLE_SCHEMA = T.StructType(
    [T.StructField(key, T.StringType()) for key in ARTICLE_TEXT_FIELDS]
    + [T.StructField(key, T.ArrayType(T.StringType())) for key in ARTICLE_LIST_FIELDS]
    + [T.StructField("extraction_status", T.StringType())]
)
KEY_SCHEMA = T.ArrayType(T.StructType([
    T.StructField("key", T.StringType()), T.StructField("method", T.StringType()),
    T.StructField("evidence", T.StringType()),
]))
WIKI_SCHEMA = T.StructType([
    T.StructField("page_id", T.LongType()),
    T.StructField("article_title", T.StringType()),
    *[field for field in ARTICLE_SCHEMA if field.name != "extraction_status"],
    T.StructField("redirect_aliases", T.ArrayType(T.StringType())),
    T.StructField("url", T.StringType()),
])


def stream_xml(path):
    source = Path(path)
    started = time.monotonic()
    count = 0
    size = source.stat().st_size
    worker = f"[Worker {os.getpid()}] {source.name}"
    last_update = started
    last_percent = 0

    with source.open("rb") as raw, (bz2.BZ2File(raw) if source.suffix == ".bz2" else nullcontext(raw)) as stream:
        print(f"{worker}: 0% read", file=sys.stderr, flush=True)
        events = ElementTree.iterparse(stream, events=("start", "end"))
        _, root = next(events)
        namespace = root.tag.split("}", 1)[0] + "}" if "}" in root.tag else ""
        for event, page in events:
            if event != "end" or page.tag != namespace + "page":
                continue
            revision = page.find(namespace + "revision")
            redirect = page.find(namespace + "redirect")
            count += 1
            now = time.monotonic()
            if now - last_update >= 5 and size:
                percent = min(99, 100 * raw.tell() // size)
                if percent > last_percent:
                    print(f"{worker}: {percent}% read ({count:,} pages)", file=sys.stderr, flush=True)
                    last_percent = percent
                last_update = now

            yield Row(
                id=int(page.findtext(namespace + "id")),
                title=page.findtext(namespace + "title"),
                ns=int(page.findtext(namespace + "ns")),
                redirect_title=redirect.get("title") if redirect is not None else None,
                model=revision.findtext(namespace + "model") if revision is not None else None,
                text=revision.findtext(namespace + "text", "") if revision is not None else "",
            )
            page.clear()
            root.clear()
    print(f"{worker}: 100% read ({count:,} pages in {time.monotonic() - started:.1f}s)",
          file=sys.stderr, flush=True)


def read_xml_rows(spark, patterns):
    paths = []
    for pattern in patterns:
        pattern = str(Path(pattern).expanduser())
        matches = [pattern] if Path(pattern).is_file() else glob.glob(pattern)
        matches = [str(Path(path).resolve()) for path in matches if Path(path).is_file()]
        if not matches:
            raise FileNotFoundError(f"No local XML archives match {pattern}")
        paths.extend(matches)

    if len(paths) != len(set(paths)):
        raise ValueError("Overlapping input paths would process an archive twice")
    return spark.sparkContext.parallelize(sorted(paths), len(paths)).flatMap(stream_xml)


def prepare_output(path):
    output = Path(path).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def tsv_escape(column):
    for old, new in [("\\", "\\\\"), ("\t", "\\t"), ("\r", "\\r"), ("\n", "\\n")]:
        column = F.replace(column, F.lit(old), F.lit(new))
    return column


def tsv_unescape(value):
    if value in (None, ""):
        return None
    result = []
    index = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            index += 1
            result.append(escapes.get(value[index], "\\" + value[index]))
        else:
            result.append(value[index])
        index += 1
    return "".join(result)


def write_tsv(frame, output):
    columns = []
    for field in frame.schema:
        value = F.col(field.name)
        if isinstance(field.dataType, (T.ArrayType, T.StructType, T.MapType)):
            value = F.to_json(value)
        else:
            value = value.cast("string")
        columns.append(tsv_escape(value).alias(field.name))

    with TemporaryDirectory(prefix="vinf-tsv-") as directory:
        parts = Path(directory) / "parts"
        (frame.select(*columns).write.option("header", False)
         .option("sep", "\t").option("quote", "").option("escape", "")
         .option("nullValue", "").option("emptyValue", "")
         .csv(str(parts)))

        with Path(output).open("xb") as destination:
            try:
                destination.write(("\t".join(frame.columns) + "\n").encode("utf-8"))
                for part in sorted(parts.glob("part-*.csv")):
                    with part.open("rb") as source:
                        shutil.copyfileobj(source, destination)
            except BaseException:
                destination.close()
                Path(output).unlink()
                raise


def read_tsv(spark, path, schema):
    raw = (spark.read.option("header", True).option("sep", "\t").option("quote", "")
           .option("mode", "FAILFAST").option("nullValue", "")
           .option("maxCharsPerColumn", -1).csv(str(path)))

    if raw.columns != schema.fieldNames():
        raise ValueError("TSV columns do not match the expected schema")

    decode = F.udf(tsv_unescape, T.StringType())
    columns = []
    for field in schema:
        value = decode(F.col(field.name))
        if isinstance(field.dataType, (T.ArrayType, T.StructType, T.MapType)):
            value = F.from_json(value, field.dataType, {"mode": "FAILFAST"})
        else:
            value = value.cast(field.dataType)
        columns.append(value.alias(field.name))
    return raw.select(*columns)


def field_availability(frame):
    expressions = []
    for field in frame.schema:
        col = F.col(field.name)
        nonempty = F.size(col) > 0 if isinstance(field.dataType, T.ArrayType) else col.cast("string") != ""
        expressions.append(F.sum(F.when(col.isNotNull() & nonempty, 1).otherwise(0)).alias(field.name))
    return {key: value or 0 for key, value in frame.agg(*expressions).first().asDict().items()}
