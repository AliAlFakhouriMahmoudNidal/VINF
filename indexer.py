import csv
import math
import re
import sys
from collections import Counter
from itertools import zip_longest
from pathlib import Path
from time import perf_counter

from rich.console import Console
from rich.table import Table
from rich.text import Text


BASE_DIR = Path(__file__).resolve().parent
SOURCE = BASE_DIR / "data" / "plants-extracted.tsv"
# Relative field weights. Restart the indexer after changing these values.
FIELD_WEIGHTS = {
    "primary_name": 4,
    "alternative_names": 3,
    "genus": 2,
    "family": 1,
    "description": 1,
    "plant_range": 1,
    "plant_types": 2,
    "foliage": 1.5,
    "habit": 1.5,
    "soil_types": 1.5,
    "moisture": 1.5,
    "exposure": 1.5,
    "position": 1.5,
}
TEXT_FIELDS = {"primary_name", "alternative_names", "description"}
DESCRIPTION_BONUS = 2
BM25_K1 = 1.2
BM25_B = 0.75


def tokenize(text):
    return re.findall(r"[^\W_]+", text.casefold())


def parse_query(query, mode="or"):
    query = query.strip()
    if not query.startswith("--"):
        if re.search(r"--(?:and|or)--", query, re.IGNORECASE):
            raise ValueError("Use either plain text or explicit clauses, without mixing them.")
        return [(mode, "phrase", list(dict.fromkeys(tokenize(query))))]

    clauses = []
    position = 0
    pattern = r'--(and|or)--([a-z_]+)="([^"\\]*)"(?=\s|$)'
    while position < len(query):
        match = re.match(pattern, query[position:], re.IGNORECASE)
        if not match:
            raise ValueError('Use --and--FIELD="words" or --or--FIELD="words". Type help for examples.')
        operator, field, value = match.groups()
        operator, field = operator.casefold(), field.casefold()
        if field != "phrase" and field not in FIELD_WEIGHTS:
            raise ValueError(f"Unknown field: {field}. Type help for searchable fields.")
        terms = list(dict.fromkeys(tokenize(value)))
        if not terms:
            raise ValueError(f"The value for {field} must contain at least one word.")
        clauses.append((operator, field, terms))
        position += match.end()
        while position < len(query) and query[position].isspace():
            position += 1
    return clauses


def matching_documents(clauses, index, documents):
    candidates = None
    for operator, field, terms in clauses:
        matching = None
        for term in terms:
            found = {entry[0] for entry in index.get(term, [])}
            if matching is None:
                matching = found
            elif operator == "and":
                matching &= found
            else:
                matching |= found
        matching = matching or set()
        if candidates is not None:
            matching &= candidates
        if field != "phrase":
            required = set(terms)
            matching = {
                doc_id for doc_id in matching
                if (required <= set(tokenize(documents[doc_id][field]))
                    if operator == "and"
                    else required.intersection(tokenize(documents[doc_id][field])))
            }
        candidates = matching
        if not candidates:
            break
    return candidates or set()


def log_tf(tf):
    return 1 + math.log10(tf) if tf > 0 else 0


def idf(df, total, method):
    if method == "bm25":
        # Strictly positive IDF, including words in over half the plants.
        return math.log1p((total - df + 0.5) / (df + 0.5))
    if method != "classic":
        raise ValueError(f"Unknown scoring method: {method}")
    return math.log(total / df)


def build_index(source=SOURCE):
    started = perf_counter()
    index = {}
    documents = {}
    # First pass: average length of each field for BM25F normalization.
    field_lengths = dict.fromkeys(FIELD_WEIGHTS, 0)
    total = 0
    with source.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            total += 1
            for field in FIELD_WEIGHTS:
                tokens = tokenize(row[field])
                length = len(tokens) if field in TEXT_FIELDS else len(set(tokens))
                field_lengths[field] += length
    averages = {field: length / total if total else 0
                for field, length in field_lengths.items()}

    with source.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            doc_id = int(row["canonical_id"])
            term_weights = {}
            for field, weight in FIELD_WEIGHTS.items():
                counts = Counter(tokenize(row[field]))
                length = sum(counts.values()) if field in TEXT_FIELDS else len(counts)
                norm = 1 - BM25_B + BM25_B * length / averages[field] if averages[field] else 1
                for term, count in counts.items():
                    # Repeated words matter in text, but only presence matters
                    # in categorical fields such as moisture and soil types.
                    tf = log_tf(count) if field in TEXT_FIELDS else 1
                    raw_tf = count if field in TEXT_FIELDS else 1
                    weights = term_weights.setdefault(term, [0, 0])
                    weights[0] += weight * tf
                    weights[1] += weight * raw_tf / norm
            for term, (classic_weight, bm25_tf) in term_weights.items():
                index.setdefault(term, []).append((doc_id, classic_weight, bm25_tf))
            documents[doc_id] = row
            if len(documents) % 10000 == 0:
                print(f"Plants processed: {len(documents):,}", flush=True)

    print(f"Documents: {len(documents):,}")
    print(f"With descriptions: {sum(bool(row['description'].strip()) for row in documents.values()):,}")
    print(f"Unique terms: {len(index):,}")
    print(f"Index entries: {sum(len(postings) for postings in index.values()):,}")
    print(f"Build time: {perf_counter() - started:.2f} s")
    return index, documents


def search(query, index, documents, method="classic", limit=10, mode="or"):
    clauses = parse_query(query, mode)
    candidates = matching_documents(clauses, index, documents)
    if not candidates:
        return []
    scores = {}
    # Count repeated query words once, in order of first occurrence.
    terms = dict.fromkeys(term for _, _, words in clauses for term in words)
    for term in terms:
        postings = index.get(term, [])
        if not postings:
            continue
        word_idf = idf(len(postings), len(documents), method)
        for doc_id, classic_weight, bm25_tf in postings:
            if doc_id not in candidates:
                continue
            # BM25F combines normalized field frequencies before saturation.
            weight = (bm25_tf * (BM25_K1 + 1) / (bm25_tf + BM25_K1)
                      if method == "bm25" else classic_weight)
            scores[doc_id] = scores.get(doc_id, 0) + weight * word_idf
    for doc_id in scores:
        if documents[doc_id]["description"].strip():
            scores[doc_id] *= DESCRIPTION_BONUS
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]


def highlighted_text(value, terms, style=None):
    text = Text(value, style=style or "")
    for match in re.finditer(r"[^\W_]+", value):
        if match.group().casefold() in terms:
            text.stylize("bold red", match.start(), match.end())
    return text


def result_text(rank, doc_id, score, documents, show_details, query=""):
    row = documents[doc_id]
    terms = {term for _, _, words in parse_query(query) for term in words}
    body = Text(overflow="fold")
    body.append(f"{rank}. ", style="bold cyan")
    body.append(highlighted_text(row["primary_name"], terms, "bold cyan"))
    body.append("\n")
    body.append(f"Score: {score:.6f}", style="bold green")
    body.append(f"    ID: {doc_id}\n", style="dim")
    body.append(row["source_url"], style="blue underline")
    if show_details:
        for field, value in row.items():
            if field in ("canonical_id", "primary_name", "source_url"):
                continue
            body.append(f"\n\n{field.replace('_', ' ').title()}: ", style="bold")
            body.append(highlighted_text(value, terms) if value
                        else Text("Not available", style="dim italic"))
    else:
        description = row["description"].strip()
        preview = description[:200] + ("…" if len(description) > 200 else "")
        body.append("\n\n")
        body.append(highlighted_text(preview, terms) if description
                    else Text("No description", style="dim italic"))
    return body


def print_results(classic_results, bm25_results, documents, show_details=False, query=""):
    table = Table(expand=True, show_lines=True, border_style="cyan", header_style="bold cyan")
    table.add_column("Classic IDF", ratio=1)
    table.add_column("BM25 (BM25F)", ratio=1)
    columns = []
    for results in (classic_results, bm25_results):
        cells = [result_text(rank, doc_id, score, documents, show_details, query)
                 for rank, (doc_id, score) in enumerate(results, 1)]
        columns.append(cells or [Text("No results.", style="yellow")])
    for left, right in zip_longest(*columns, fillvalue=Text("")):
        table.add_row(left, right)
    Console().print(table)


def print_help():
    console = Console()
    console.rule("[bold cyan]Search help")
    examples = Table(title="Search examples", border_style="cyan", show_lines=True)
    examples.add_column("Query", style="green", ratio=1)
    examples.add_column("Meaning", ratio=1)
    for query, meaning in (
        ("red flowers", "Search all fields. Uses the current mode: AND = all words, OR = any word."),
        ('--and--soil_types="clay loam"', "Require both clay and loam in soil_types."),
        ('--or--soil_types="clay loam"', "Require clay or loam in soil_types."),
        ('--and--phrase="red flowers"', "Require both words across all searchable fields, in any order."),
        ('--and--soil_types="clay loam" --or--phrase="red flowers"',
         "Require both soil words AND either red or flowers across all searchable fields."),
    ):
        examples.add_row(Text(query), meaning)
    console.print(examples)
    console.print("[bold]Query rules[/bold]\n"
                  "Every explicit clause must match; session mode applies only to plain searches.\n"
                  "Words are case-insensitive; extra words are allowed. phrase searches words in any order.\n"
                  "Use double-quoted values and keep plain searches separate from explicit clauses.")
    fields = Table(title="Searchable fields:", border_style="cyan")
    fields.add_column("Field", style="cyan")
    fields.add_column("Searches")
    fields.add_row("phrase", "All searchable fields")
    for field in FIELD_WEIGHTS:
        fields.add_row(field, field.replace("_", " ").capitalize())
    console.print(fields)
    commands = Table(title="Session commands", border_style="cyan")
    commands.add_column("Command", style="green")
    commands.add_column("Action")
    for command, action in (
        ("mode and / mode or", "Require all / any words in plain searches (default: OR)."),
        ("details on / details off", "Show all fields / a short preview (default: off)."),
        ("limit 20", "Set results per scoring method (default: 10)."),
        ("help", "Show this guide."),
        ("Blank input", "Exit the indexer."),
    ):
        commands.add_row(command, action)
    console.print(commands)


def main():
    if sys.argv[1:]:
        sys.exit("Usage: python indexer.py")

    print("Building the index in memory...")
    index, documents = build_index()
    console = Console()
    print("Type 'details on' or 'details off' to change the view for following queries.")
    print("Type 'mode and' to require all words, or 'mode or' to match any word.")
    print("Type 'limit 20' to show up to 20 results per scoring method.")
    print("Type 'help' for field-aware query syntax and examples.")
    show_details = False
    mode = "or"
    limit = 10
    while True:
        query = input(f"\nQuery [mode {mode.upper()}, details {'on' if show_details else 'off'}, limit {limit}]: ").strip()
        if not query:
            break
        if query.casefold() == "help":
            print_help()
            continue
        if query.casefold() in ("details on", "details off"):
            show_details = query.casefold() == "details on"
            console.print(f"Details: {'ON' if show_details else 'OFF'}")
            continue
        if query.casefold() in ("mode and", "mode or"):
            mode = query.casefold().split()[1]
            console.print(f"Mode: {mode.upper()}")
            continue
        parts = query.casefold().split()
        if parts[0] == "limit":
            try:
                if len(parts) != 2 or int(parts[1]) <= 0:
                    raise ValueError
                limit = int(parts[1])
            except ValueError:
                console.print("Use 'limit N' with a positive whole number, for example 'limit 20'.")
                continue
            console.print(f"Limit: {limit}")
            continue
        try:
            started = perf_counter()
            classic_results = search(query, index, documents, "classic", limit=limit, mode=mode)
            classic_finished = perf_counter()
            bm25_results = search(query, index, documents, "bm25", limit=limit, mode=mode)
            finished = perf_counter()
        except ValueError as error:
            console.print(Text(str(error), style="yellow"))
            continue
        print_results(classic_results, bm25_results, documents, show_details, query)
        console.print(
            f"[bold cyan]Search time:[/bold cyan] {(finished - started) * 1000:.2f} ms total"
            f"  [dim](Classic: {(classic_finished - started) * 1000:.2f} ms"
            f" | BM25F: {(finished - classic_finished) * 1000:.2f} ms)[/dim]"
        )


if __name__ == "__main__":
    main()
