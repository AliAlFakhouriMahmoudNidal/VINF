import json
import re
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


JSON_STRING = r'"(?:\\.|[^"\\])*"'
QUOTED_STRING = re.compile(JSON_STRING)
METADATA = re.compile(
    r'''<script\b[^>]*\btype\s*=\s*["']application/ld\+json["'][^>]*>'''
    r'(.*?)</script>', FLAGS,
)
ALIASES = re.compile(rf'''
    "alternateName" \s* : \s* \[ \s*
    ( {JSON_STRING} (?: \s*,\s* {JSON_STRING} )* )?
    \s* \]
''', re.VERBOSE)

# One match returns both the rank and name, whichever property comes first.
TAXONOMY = re.compile(rf'''
    \{{
    (?=[^{{}}]*"taxonRank"\s*:\s*"(genus|family)")
    [^{{}}]*?"name"\s*:\s*({JSON_STRING})
''', re.VERBOSE)

# Match a canonical link and capture its URL. Attribute order does not matter.
CANONICAL = re.compile(r'''
    <link\b
    (?=[^>]*\brel\s*=\s*["']canonical["'])
    [^>]*\bhref\s*=\s*["']([^"']+)["'][^>]*>
''', FLAGS | re.VERBOSE)
PLANT_URL = re.compile(r"https://www\.rhs\.org\.uk/plants/(\d+)/[^/?#]+/details/?")
JUNK = re.compile(r"<!--.*?-->|<(script|style|dialog|svg|noscript)\b[^>]*>.*?</\1\s*>", FLAGS)
MAIN = re.compile(
    r'''<main\b[^>]*\bclass=["'][^"']*\bplant-profile__main\b[^"']*["'][^>]*>'''
    r'(.*?)</main>', FLAGS,
)
HEADING = re.compile(r"<h1\b[^>]*>(.*?)</h1>", FLAGS)
TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", FLAGS)
NAME_PARAGRAPHS = re.compile(
    r'''<p\b[^>]*\bclass=["']plant-profile__(?:names|synonyms)-text["'][^>]*>'''
    r'(.*?)</p>', FLAGS,
)

# Description paragraphs have no class attribute.
DESCRIPTION = re.compile(r"<p\b(?![^>]*\bclass\s*=)[^>]*>(.*?)</p>", FLAGS)
CARDS_START = re.compile(r'''<div\b[^>]*\bclass=["']cards["'][^>]*>''', FLAGS)
CARD_FIELDS = re.compile(
    r'<h2\b[^>]*>\s*(Position|Soil Types)\s*</h2>.*?'
    r'''<div\b[^>]*\bclass=["']card__bottom["'][^>]*>'''
    r'(.*?)</div>', FLAGS,
)
TYPES = re.compile(
    r'''<span\b[^>]*\bclass=["'][^"']*\bplant-profile__type\b[^"']*["'][^>]*>'''
    r'(.*?)</span>', FLAGS,
)
DEFINITIONS = re.compile(r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>", FLAGS)
BLOCK_TAGS = re.compile(r"<br\b[^>]*>|</(?:p|div|li|dt|dd)\s*>", FLAGS)
TAGS = re.compile(r"<[^>]+>")
LIST_SEPARATOR = re.compile(r"\s+or\s+|,\s*", re.IGNORECASE)


def clean_text(text):
    # Keep inline words together: semi-<a>hardwood</a> -> semi-hardwood.
    text = BLOCK_TAGS.sub(" ", text)
    text = TAGS.sub("", text)
    text = unescape(text)
    return " ".join(text.split())


def unique(values, exclude=""):
    by_name = {}
    excluded = exclude.casefold()
    for value in values:
        key = value.casefold()
        if value and key != excluded:
            by_name.setdefault(key, value)
    return list(by_name.values())


def split_values(text):
    # RHS uses several dash characters in the same controlled categories.
    text = clean_text(text).replace("\u2013", "-").replace("\u2011", "-")
    return unique(part.strip() for part in LIST_SEPARATOR.split(text))


def extract_plant(html):
    canonical = CANONICAL.search(html)
    url = unescape(canonical.group(1)).strip() if canonical else ""
    plant_id = PLANT_URL.fullmatch(url)
    body = JUNK.sub("", html)
    main = MAIN.search(body)
    heading = HEADING.search(body)
    if not plant_id or not main or not heading:
        raise ValueError("Missing RHS canonical plant URL, plant section, or heading")

    profile = main.group(1)
    record = {field: [] if field in LIST_FIELDS else "" for field in COLUMNS}
    record["canonical_id"] = plant_id.group(1)
    record["source_url"] = url.rstrip("/")
    record["primary_name"] = clean_text(heading.group(1))
    if not record["primary_name"]:
        raise ValueError("Empty plant name")

    names = record["alternative_names"]
    for script in METADATA.findall(html):
        aliases = ALIASES.search(script)
        if aliases:
            for value in QUOTED_STRING.findall(aliases.group(1) or ""):
                names.append(clean_text(json.loads(value)))
        for field, name in TAXONOMY.findall(script):
            if not record[field]:
                record[field] = clean_text(json.loads(name))

    # These visible names also cover pages without an alternateName array.
    if not names:
        title = TITLE.search(body)
        if title:
            title_text = clean_text(title.group(1))
            if "|" in title_text:
                names.append(title_text.rsplit("|", 1)[1].strip())
        for paragraph in NAME_PARAGRAPHS.findall(profile):
            names.extend(part.strip() for part in clean_text(paragraph).split(","))
    record["alternative_names"] = unique(names, exclude=record["primary_name"])

    # RHS puts its description after the names/types and before the summary cards.
    # Name paragraphs have classes; the description paragraph does not.
    before_cards = CARDS_START.split(profile, maxsplit=1)[0]
    for paragraph in DESCRIPTION.findall(before_cards):
        record["description"] = clean_text(paragraph)

    record["plant_types"] = unique(clean_text(value) for value in TYPES.findall(profile))
    for label, value in DEFINITIONS.findall(body):
        field = LABELS.get(clean_text(label))
        if field:
            record[field] = split_values(value) if field in LIST_FIELDS else clean_text(value)
    for label, value in CARD_FIELDS.findall(profile):
        field = "position" if label.lower() == "position" else "soil_types"
        record[field] = split_values(value)
    return record


def merge_plant(plants, record, source_file):
    plant_id = record["canonical_id"]
    is_canonical = source_file.stem == plant_id
    if plant_id not in plants:
        plants[plant_id] = record
        return

    existing = plants[plant_id]
    names = existing["alternative_names"] + [existing["primary_name"]]
    names += record["alternative_names"] + [record["primary_name"]]
    for field in COLUMNS:
        if record[field] and (is_canonical or not existing[field]):
            existing[field] = record[field]
    existing["alternative_names"] = unique(names, exclude=existing["primary_name"])


def tsv_cell(value):
    if isinstance(value, list):
        items = (" ".join(item.split()) for item in value)
        return "|".join(item.replace("\\", "\\\\").replace("|", "\\|") for item in items)
    return " ".join(str(value).split())


def write_tsv(plants, output):
    temporary = output.with_suffix(output.suffix + ".tmp")
    print(f"Writing {len(plants):,} plants to {output}", flush=True)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as target:
            target.write("\t".join(COLUMNS) + "\n")
            for plant_id in sorted(plants, key=int):
                record = plants[plant_id]
                cells = (tsv_cell(record[field]) for field in COLUMNS)
                target.write("\t".join(cells) + "\n")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def read_plant(source):
    html = source.read_bytes().decode("utf-8")
    return source, extract_plant(html)


def read_plants(files, readers):
    if readers == 1:
        yield from map(read_plant, files)
    else:
        # Small batches bound memory use and let Ctrl+C stop without a huge queue.
        with ThreadPoolExecutor(max_workers=readers) as pool:
            for start in range(0, len(files), readers * 2):
                yield from pool.map(read_plant, files[start:start + readers * 2])


def main():
    input_dir = Path("data/input/html")
    output = Path("data/output/plants-extracted.tsv")
    readers = 1
    limit = None

    files = list(input_dir.glob("*.html"))
    if limit is not None:
        files = files[:limit]

    output.parent.mkdir(parents=True, exist_ok=True)
    plants = {}
    print(f"Parsed 0/{len(files):,} files", flush=True)

    for count, (source, record) in enumerate(read_plants(files, readers), 1):
        merge_plant(plants, record, source)
        if count % 1000 == 0 or count == len(files):
            print(f"Parsed {count:,}/{len(files):,} files", flush=True)

    if not plants:
        raise ValueError("No valid plants extracted")
    write_tsv(plants, output)
    print(f"Saved {len(plants):,} plants to {output}", flush=True)

if __name__ == "__main__":
    main()
