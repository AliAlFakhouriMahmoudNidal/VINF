"""Extract saved RHS plant pages: python extract_tsv.py --input data/html --output data/plants.tsv."""

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path


COLUMNS = (
    "canonical_id", "source_url", "primary_name", "alternative_names", "genus",
    "family", "description", "plant_range", "plant_types", "foliage", "habit",
    "soil_types", "moisture", "exposure", "position",
)
LIST_FIELDS = {
    "alternative_names", "plant_types", "foliage", "habit", "soil_types",
    "moisture", "exposure", "position",
}
LABELS = {
    "Family": "family", "Plant Range": "plant_range", "Foliage": "foliage",
    "Habit": "habit", "Moisture": "moisture", "Exposure": "exposure",
}
FLAGS = re.IGNORECASE | re.DOTALL

# Regex finds the fields. json.loads below only decodes individual quoted strings
# (for example, \u00d7 or an escaped quotation mark), not the metadata document.
JSON_STRING = r'"(?:\\.|[^"\\])*"'
QUOTED_STRING = re.compile(JSON_STRING)
METADATA = re.compile(r'''<script\b[^>]*\btype\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>''', FLAGS)
ALIASES = re.compile(r'"alternateName"\s*:\s*\[\s*(' + JSON_STRING + r'(?:\s*,\s*' + JSON_STRING + r')*)?\s*\]')
TAXON_NAMES = re.compile(r'\{(?=[^{}]*"taxonRank"\s*:\s*"(genus|family)")[^{}]*?"name"\s*:\s*(' + JSON_STRING + r')')
CANONICAL = re.compile(r'''<link\b(?=[^>]*\brel\s*=\s*["']canonical["'])(?=[^>]*\bhref\s*=\s*["']([^"']+)["'])[^>]*>''', FLAGS)
PLANT_URL = re.compile(r"https://www\.rhs\.org\.uk/plants/(\d+)/[^/?#]+/details/?")
JUNK = re.compile(r"<!--.*?-->|<(script|style|dialog|svg|noscript)\b[^>]*>.*?</\1\s*>", FLAGS)
MAIN = re.compile(r'''<main\b[^>]*\bclass=["'][^"']*\bplant-profile__main\b[^"']*["'][^>]*>(.*?)</main>''', FLAGS)
HEADING = re.compile(r"<h1\b[^>]*>(.*?)</h1>", FLAGS)
TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", FLAGS)
NAME_PARAGRAPHS = re.compile(r'''<p\b[^>]*\bclass=["']plant-profile__(?:names|synonyms)-text["'][^>]*>(.*?)</p>''', FLAGS)
PARAGRAPHS = re.compile(r"<p\b([^>]*)>(.*?)</p>", FLAGS)
HAS_CLASS = re.compile(r"\bclass\s*=", re.IGNORECASE)
CARDS_START = re.compile(r'''<div\b[^>]*\bclass=["']cards["'][^>]*>''', FLAGS)
CARD_FIELDS = re.compile(r'''<h2\b[^>]*>\s*(Position|Soil Types)\s*</h2>.*?<div\b[^>]*\bclass=["']card__bottom["'][^>]*>(.*?)</div>''', FLAGS)
TYPES = re.compile(r'''<span\b[^>]*\bclass=["'][^"']*\bplant-profile__type\b[^"']*["'][^>]*>(.*?)</span>''', FLAGS)
DEFINITIONS = re.compile(r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>", FLAGS)
BLOCK_TAGS = re.compile(r"<br\b[^>]*>|</(?:p|div|li|dt|dd)\s*>", FLAGS)
TAGS = re.compile(r"<[^>]+>")
LIST_SEPARATOR = re.compile(r"\s+or\s+|,\s*", re.IGNORECASE)


def clean_text(text):
    # Keep inline words together: semi-<a>hardwood</a> -> semi-hardwood.
    text = BLOCK_TAGS.sub(" ", text)
    return " ".join(unescape(TAGS.sub("", text)).split())


def unique(values):
    result = []
    seen = set()
    for value in values:
        if value and value.casefold() not in seen:
            result.append(value)
            seen.add(value.casefold())
    return result


def split_values(text):
    # RHS uses several dash characters in the same controlled categories.
    text = clean_text(text).replace("\u2013", "-").replace("\u2011", "-")
    return unique(part.strip() for part in LIST_SEPARATOR.split(text))


def extract_plant(html):
    record = {field: [] if field in LIST_FIELDS else "" for field in COLUMNS}
    canonical = CANONICAL.search(html)
    url = unescape(canonical[1]).strip() if canonical else ""
    plant_id = PLANT_URL.fullmatch(url)
    body = JUNK.sub("", html)
    main = MAIN.search(body)
    heading = HEADING.search(body)
    if not plant_id or not main or not heading:
        raise ValueError("Missing RHS canonical plant URL, plant section, or heading")

    record["canonical_id"] = plant_id[1]
    record["source_url"] = url.rstrip("/")
    record["primary_name"] = clean_text(heading[1])
    if not record["primary_name"]:
        raise ValueError("Empty plant name")

    for script in METADATA.findall(html):
        aliases = ALIASES.search(script)
        if aliases:
            record["alternative_names"].extend(
                clean_text(json.loads(value))
                for value in QUOTED_STRING.findall(aliases[1] or "")
            )
        for rank, value in TAXON_NAMES.findall(script):
            if not record[rank]:
                record[rank] = clean_text(json.loads(value))

    # These visible names also cover pages without an alternateName array.
    if not record["alternative_names"]:
        title = TITLE.search(body)
        if title and "|" in clean_text(title[1]):
            record["alternative_names"].append(clean_text(title[1]).rsplit("|", 1)[1].strip())
        for paragraph in NAME_PARAGRAPHS.findall(main[1]):
            record["alternative_names"].extend(part.strip() for part in clean_text(paragraph).split(","))
    record["alternative_names"] = unique(
        name for name in record["alternative_names"]
        if name.casefold() != record["primary_name"].casefold()
    )

    # RHS puts its description after the names/types and before the summary cards.
    # Name paragraphs have classes; the description paragraph does not.
    before_cards = CARDS_START.split(main[1], maxsplit=1)[0]
    for attributes, paragraph in PARAGRAPHS.findall(before_cards):
        if not HAS_CLASS.search(attributes):
            record["description"] = clean_text(paragraph)

    record["plant_types"] = unique(clean_text(value) for value in TYPES.findall(main[1]))
    for label, value in DEFINITIONS.findall(body):
        field = LABELS.get(clean_text(label))
        if field:
            record[field] = split_values(value) if field in LIST_FIELDS else clean_text(value)
    for label, value in CARD_FIELDS.findall(main[1]):
        field = "position" if label.lower() == "position" else "soil_types"
        record[field] = split_values(value)
    return record


def merge_plant(plants, record, source_file):
    plant_id = record["canonical_id"]
    canonical = source_file.stem == plant_id
    if plant_id not in plants:
        record["_canonical"] = canonical
        record["_source_file"] = source_file.name
        plants[plant_id] = record
        return False

    existing = plants[plant_id]
    names = unique(existing["alternative_names"] + [existing["primary_name"]]
                   + record["alternative_names"] + [record["primary_name"]])
    for field in COLUMNS:
        if record[field] and (canonical or not existing[field]):
            existing[field] = record[field]
    if canonical:
        existing["_canonical"] = True
        existing["_source_file"] = source_file.name
    existing["alternative_names"] = [
        name for name in names if name.casefold() != existing["primary_name"].casefold()
    ]
    return True


def serialize_value(value):
    if isinstance(value, list):
        return "|".join(" ".join(item.split()).replace("\\", "\\\\").replace("|", "\\|") for item in value)
    return " ".join(str(value).split())


def duration(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def show_progress(stats, total, plant_count, started, force=False):
    now = time.perf_counter()
    if not force and now < stats["next_update"]:
        return
    stats["next_update"] = now + 1
    elapsed = max(now - started, 0.000001)
    processed = stats["processed"]
    left = total - processed
    eta = duration(left * elapsed / processed) if processed else "--:--:--"
    print(f"Parsing {processed:,}/{total:,} ({processed / total:.2%}) | Left {left:,} | "
          f"Plants {plant_count:,} | Merged {stats['merged']:,} | Failed {stats['failed']:,}", flush=True)
    print(f"  {processed / elapsed:,.1f} docs/s | {stats['bytes'] / 1_000_000 / elapsed:,.2f} MB/s | "
          f"{stats['tokens'] / elapsed:,.0f} tokens/s (words) | Elapsed {duration(elapsed)} | ETA {eta}", flush=True)


def write_tsv(plants, output):
    temporary = output.with_suffix(output.suffix + ".tmp")
    started = time.perf_counter()
    next_update = started + 1
    print(f"Writing {len(plants):,} plants to {output}", flush=True)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as target:
            target.write("\t".join(COLUMNS) + "\n")
            for count, plant_id in enumerate(sorted(plants, key=int), 1):
                record = plants[plant_id]
                target.write("\t".join(serialize_value(record[field]) for field in COLUMNS) + "\n")
                now = time.perf_counter()
                if now >= next_update:
                    print(f"Writing {count:,}/{len(plants):,} rows ({count / len(plants):.2%})", flush=True)
                    next_update = now + 1
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Writing finished in {time.perf_counter() - started:.2f}s", flush=True)


def read_file(source):
    try:
        return source, source.read_bytes(), ""
    except OSError as error:
        return source, b"", str(error)


def read_files(files, readers):
    if readers == 1:
        yield from map(read_file, files)
    else:
        # Small batches bound memory use and let Ctrl+C stop without a huge queue.
        with ThreadPoolExecutor(max_workers=readers) as pool:
            for start in range(0, len(files), readers * 2):
                yield from pool.map(read_file, files[start:start + readers * 2])


def run(input_dir, output, readers=1, limit=None):
    files = sorted(input_dir.glob("*.html"), key=lambda p: (
        int(p.stem) if p.stem.isdigit() else float("inf"), p.name,
    ))
    if limit is not None:
        files = files[:limit]
    if not files:
        raise ValueError(f"No HTML files found in {input_dir}")
    output.parent.mkdir(parents=True, exist_ok=True)
    error_path = output.with_suffix(".errors.tsv")
    plants = {}
    stats = {"processed": 0, "failed": 0, "merged": 0, "bytes": 0, "tokens": 0, "next_update": 0}
    started = time.perf_counter()
    show_progress(stats, len(files), 0, started, force=True)

    with error_path.open("w", encoding="utf-8", newline="") as errors:
        errors.write("source_file\tlevel\tmessage\n")
        for source, raw, read_error in read_files(files, readers):
            try:
                if read_error:
                    raise ValueError(read_error)
                stats["bytes"] += len(raw)
                record = extract_plant(raw.decode("utf-8"))
                stats["tokens"] += sum(
                    len(item.split())
                    for field in COLUMNS if field not in ("canonical_id", "source_url")
                    for item in (record[field] if isinstance(record[field], list) else [record[field]])
                )
                stats["merged"] += merge_plant(plants, record, source)
            except (OSError, UnicodeError, ValueError) as error:
                stats["failed"] += 1
                errors.write(f"{source.name}\terror\t{clean_text(str(error))}\n")
            stats["processed"] += 1
            show_progress(stats, len(files), len(plants), started)

        show_progress(stats, len(files), len(plants), started, force=True)
        fallbacks = 0
        for plant_id, record in plants.items():
            if not record["_canonical"]:
                fallbacks += 1
                errors.write(f"{record['_source_file']}\twarning\tCanonical {plant_id}.html was not parsed; using this record\n")

    if not plants:
        raise ValueError(f"No valid plants extracted. See {error_path}")
    write_tsv(plants, output)
    print(f"Saved {len(plants):,} plants from {len(files):,} files | Merged {stats['merged']:,} | "
          f"Failed {stats['failed']:,} | Canonical fallbacks {fallbacks:,}", flush=True)
    print(f"Total elapsed: {duration(time.perf_counter() - started)} | Issues: {error_path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/html"), help="Folder containing saved HTML")
    parser.add_argument("--output", type=Path, default=Path("data/plants.tsv"), help="Destination TSV file")
    parser.add_argument("--readers", type=int, default=1, help="File readers (default: 1; try 4 for slow disk reads)")
    parser.add_argument("--limit", type=int, help="Maximum input files to parse (default: all files)")
    args = parser.parse_args()
    if args.output.suffix.lower() != ".tsv":
        parser.error("--output must end with .tsv")
    if args.readers < 1:
        parser.error("--readers must be at least 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    try:
        run(args.input, args.output, args.readers, args.limit)
    except KeyboardInterrupt:
        print("\nStopped. Incomplete TSV output was not published. Run again to restart.", flush=True)
        return 130
    except (OSError, ValueError) as error:
        print(f"Error: {error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
