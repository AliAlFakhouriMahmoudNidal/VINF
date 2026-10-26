"""Build an in-memory index and search it with: python indexer.py."""

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
SOURCE = BASE_DIR / "plants-extracted.tsv"
NAME_FIELDS = ("primary_name", "alternative_names")
NAME_WEIGHT = 3
DESCRIPTION_BONUS = 2


def tokenize(text):
    return re.findall(r"[^\W_]+", text.casefold())


def log_tf(tf):
    return 1 + math.log10(tf) if tf > 0 else 0


def idf(df, total, method):
    if method == "smooth":
        return math.log((total + 1) / (df + 1)) + 1
    return math.log(total / df)


def build_index(source=SOURCE):
    started = perf_counter()
    index = {}
    documents = {}
    with source.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            doc_id = int(row["canonical_id"])
            names = " ".join(row[field] for field in NAME_FIELDS)
            other = " ".join(value for field, value in row.items()
                             if field not in (*NAME_FIELDS, "canonical_id", "source_url"))
            name_tf = Counter(tokenize(names))
            other_tf = Counter(tokenize(other))
            for term in name_tf.keys() | other_tf.keys():
                index.setdefault(term, []).append((doc_id, name_tf[term], other_tf[term]))
            documents[doc_id] = row
            if len(documents) % 50000 == 0:
                print(f"Plants processed: {len(documents):,}", flush=True)

    print(f"Documents: {len(documents):,}")
    print(f"With descriptions: {sum(bool(row['description'].strip()) for row in documents.values()):,}")
    print(f"Unique terms: {len(index):,}")
    print(f"Index entries: {sum(len(postings) for postings in index.values()):,}")
    print(f"Build time: {perf_counter() - started:.2f} s")
    return index, documents


def search(query, index, documents, method="classic", limit=10, mode="or"):
    scores = {}
    candidates = None
    # Count repeated query words once, in order of first occurrence.
    for term in dict.fromkeys(tokenize(query)):
        postings = index.get(term, [])
        if not postings:
            if mode == "and":
                return []
            continue
        if mode == "and":
            matching = {doc_id for doc_id, _, _ in postings}
            candidates = matching if candidates is None else candidates & matching
            if not candidates:
                return []
        word_idf = idf(len(postings), len(documents), method)
        for doc_id, tf_name, tf_other in postings:
            weight = NAME_WEIGHT * log_tf(tf_name) + log_tf(tf_other)
            scores[doc_id] = scores.get(doc_id, 0) + weight * word_idf
    for doc_id in scores:
        if documents[doc_id]["description"].strip():
            scores[doc_id] *= DESCRIPTION_BONUS
    if mode == "and":
        scores = {doc_id: score for doc_id, score in scores.items() if doc_id in candidates}
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]


def result_text(rank, doc_id, score, documents, show_details):
    row = documents[doc_id]
    body = Text(overflow="fold")
    body.append(f"{rank}. {row['primary_name']}\n", style="bold cyan")
    body.append(f"Score: {score:.6f}", style="bold green")
    body.append(f"    ID: {doc_id}\n", style="dim")
    body.append(row["source_url"], style="blue underline")
    if show_details:
        for field, value in row.items():
            if field in ("canonical_id", "primary_name", "source_url"):
                continue
            body.append(f"\n\n{field.replace('_', ' ').title()}: ", style="bold")
            body.append(value or "Not available", style=None if value else "dim italic")
    else:
        description = row["description"].strip()
        preview = description[:200] + ("…" if len(description) > 200 else "")
        body.append("\n\n" + (preview or "No description"),
                    style=None if description else "dim italic")
    return body


def print_results(classic_results, smooth_results, documents, show_details=False):
    table = Table(expand=True, show_lines=True, border_style="cyan", header_style="bold cyan")
    table.add_column("Classic IDF", ratio=1)
    table.add_column("Smooth IDF", ratio=1)
    columns = []
    for results in (classic_results, smooth_results):
        cells = [result_text(rank, doc_id, score, documents, show_details)
                 for rank, (doc_id, score) in enumerate(results, 1)]
        columns.append(cells or [Text("No results.", style="yellow")])
    for left, right in zip_longest(*columns, fillvalue=Text("")):
        table.add_row(left, right)
    Console().print(table)


def main():
    if sys.argv[1:]:
        sys.exit("Usage: python indexer.py")

    print("Building the index in memory...")
    index, documents = build_index()
    console = Console()
    print("Type 'details on' or 'details off' to change the view for following queries.")
    print("Type 'mode and' to require all words, or 'mode or' to match any word.")
    show_details = False
    mode = "or"
    while True:
        query = input(f"\nQuery [mode {mode.upper()}, details {'on' if show_details else 'off'}]: ").strip()
        if not query:
            break
        if query.casefold() in ("details on", "details off"):
            show_details = query.casefold() == "details on"
            console.print(f"Details: {'ON' if show_details else 'OFF'}")
            continue
        if query.casefold() in ("mode and", "mode or"):
            mode = query.casefold().split()[1]
            console.print(f"Mode: {mode.upper()}")
            continue
        classic_results = search(query, index, documents, "classic", mode=mode)
        smooth_results = search(query, index, documents, "smooth", mode=mode)
        print_results(classic_results, smooth_results, documents, show_details)


if __name__ == "__main__":
    main()
