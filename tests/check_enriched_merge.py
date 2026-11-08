"""Compare a merged enriched TSV with the preceding side-by-side export."""

import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spark"))
from wiki_utils import rhs_aliases


def read_rows(path):
    csv.field_size_limit(2**31 - 1)
    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE):
            yield {key: re.sub(r"\\([nrt\\])", lambda match: escapes[match[1]], value)
                   for key, value in row.items()}


def check(before_path, after_path):
    before = {row["canonical_id"]: row for row in read_rows(before_path)}
    count = len(before)
    shared = {"genus", "family", "description", "plant_range"}
    wiki_text = {"introduction", "habitat", "uses", "cultivation"}
    for actual in read_rows(after_path):
        original = before.pop(actual["canonical_id"])
        expected = {}
        for name, value in original.items():
            if name in {
                "match_status", "match_evidence", "wiki_redirect_aliases",
                "wiki_scientific_synonyms", "wiki_common_names",
            }:
                continue
            if name == "alternative_names":
                names = rhs_aliases(value)
                names.extend(json.loads(original["wiki_scientific_synonyms"] or "[]"))
                names.extend(json.loads(original["wiki_common_names"] or "[]"))
                expected["names"] = list(dict.fromkeys(names))
            elif name in shared:
                value_key = "value" if name in {"genus", "family"} else "text"
                expected[name] = [
                    {"source": source, value_key: text}
                    for source, text in [("rhs", value), ("wiki", original["wiki_" + name])]
                    if text
                ]
            elif name.startswith("wiki_"):
                field = name.removeprefix("wiki_")
                if field in shared:
                    continue
                output_name = name if field in {"page_id", "url"} else field
                if field in wiki_text:
                    expected[output_name] = [{"source": "wiki", "text": value}] if value else []
                else:
                    expected[output_name] = value
            elif name == "candidate_page_ids":
                expected[name] = json.loads(value)
            else:
                expected[name] = value
        decoded = {
            name: json.loads(value) if isinstance(expected[name], list) else value
            for name, value in actual.items()
        }
        assert decoded == expected, original["canonical_id"]
    assert not before, "Rows were lost"
    print(f"Verified all {count:,} rows against the preceding export")


if __name__ == "__main__":
    check(*sys.argv[1:])
