import bz2
import glob
import os
import re
import shutil
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

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


XML_MARKUP = re.compile(rb"<!\[CDATA\[.*?\]\]>|<!--.*?-->|<(?!\!\[CDATA\[|!--)[^>]*>", re.S)
XML_PAGE_START = re.compile(rb"<page\s*>")
XML_PAGE_END = re.compile(rb"</page\s*>")
XML_ROOT_START = re.compile(rb"<mediawiki\b[^>]*>")
XML_ROOT_END = re.compile(rb"</mediawiki\s*>")
XML_REVISION = re.compile(r"<revision\b[^>]*>(.*?)</revision\s*>", re.S)
XML_FIELDS = {
    name: re.compile(r"<" + name + r"\b[^>]*?(?:/\s*>|>(.*?)</" + name + r"\s*>)", re.S)
    for name in ("title", "ns", "id", "model", "text")
}
XML_REDIRECT = re.compile(r"<redirect\b[^>]*>")
XML_TITLE_ATTRIBUTE = re.compile(r"\btitle\s*=\s*([\"'])(.*?)\1", re.S)
XML_ESCAPE = re.compile(r"&(#x[0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos);")
XML_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def xml_unescape(value):
    def replace(match):
        entity = match[1]
        if entity.startswith("#x"):
            return chr(int(entity[2:], 16))
        if entity.startswith("#"):
            return chr(int(entity[1:]))
        return XML_ENTITIES[entity]

    return XML_ESCAPE.sub(replace, value)


def xml_pages(stream, chunk_size=1024 * 1024):
    buffer = b""
    page = None
    root_seen = False
    root_closed = False
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        buffer += chunk
        position = 0
        for opening in re.finditer(b"<", buffer):
            if opening.start() < position:
                continue
            if page is not None:
                page.extend(buffer[position:opening.start()])
            position = opening.start()
            tag = XML_MARKUP.match(buffer, position)
            if tag is None:
                break
            markup = tag[0]
            if XML_PAGE_START.fullmatch(markup):
                if page is not None or not root_seen or root_closed:
                    raise ValueError("Unexpected Wikipedia page opening")
                page = bytearray()
            if page is not None:
                page.extend(markup)
            if XML_PAGE_END.fullmatch(markup):
                if page is None:
                    raise ValueError("Unexpected Wikipedia page ending")
                yield bytes(page)
                page = None
            if XML_ROOT_START.fullmatch(markup):
                root_seen = True
            if XML_ROOT_END.fullmatch(markup):
                root_closed = True
            position = tag.end()
        else:
            if page is not None:
                page.extend(buffer[position:])
            position = len(buffer)
        buffer = buffer[position:]
    if page is not None or buffer.strip() or not root_closed:
        raise ValueError("Incomplete Wikipedia XML dump")


def xml_record(page):
    def escape_literal(match):
        if match[1] is None:
            return ""
        return match[1].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    text = page.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(
        r"<!\[CDATA\[(.*?)\]\]>|<!--.*?-->",
        escape_literal, text, flags=re.S,
    )
    revision = XML_REVISION.search(text)
    header = text[:revision.start()] if revision else text
    revision_text = revision[1] if revision else ""
    fields = {}
    for name, pattern in XML_FIELDS.items():
        match = pattern.search(revision_text if name in ("model", "text") else header)
        fields[name] = xml_unescape(match[1] or "") if match else None
    redirect = XML_REDIRECT.search(header)
    title = XML_TITLE_ATTRIBUTE.search(redirect[0]) if redirect else None
    return Row(
        id=int(fields["id"]), title=fields["title"], ns=int(fields["ns"]),
        redirect_title=xml_unescape(re.sub(r"[\t\n\r]", " ", title[2])) if title else None,
        model=fields["model"], text=fields["text"] or "",
    )


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
        for page in xml_pages(stream):
            count += 1
            now = time.monotonic()
            if now - last_update >= 5 and size:
                percent = min(99, 100 * raw.tell() // size)
                if percent > last_percent:
                    print(f"{worker}: {percent}% read ({count:,} pages)", file=sys.stderr, flush=True)
                    last_percent = percent
                last_update = now

            yield xml_record(page)
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
