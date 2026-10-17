"""python extract.py 141042.html — save extracted fields to 141042.json."""

import json
import re
import sys
from html import unescape
from pathlib import Path


def clean(text):
    text = re.sub(r"<(script|style|dialog|svg)\b[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", text)).split())


def extract(html):
    taxon = None
    for script in re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        document = json.loads(script)
        items = document if isinstance(document, list) else document.get("@graph", [document])
        for item in items:
            if item.get("@type") == "Taxon":
                taxon = item
                break
        if taxon:
            break
    if not taxon:
        raise ValueError("No RHS Taxon data found in this HTML.")

    genus = taxon.get("parentTaxon", {})
    family = genus.get("parentTaxon", {})
    data = {
        "rhs_id": taxon.get("identifier"),
        "source_url": taxon.get("url"),
        "name": taxon.get("name"),
        "description": taxon.get("description"),
        # RHS combines common names and synonyms in alternateName.
        "alternate_names": taxon.get("alternateName", []),
        "genus": genus.get("name"),
        "family": family.get("name"),
    }
    fields = {
        "Time to Maturity": "time_to_maturity", "Max Height": "max_height",
        "Max Spread": "max_spread", "Moisture": "moisture", "pH": "ph",
        "Aspect": "aspect", "Exposure": "exposure", "Hardiness": "hardiness",
        "Family": "family", "Native to GB/Ireland": "native_to_gb_ireland",
        "Foliage": "foliage", "Habit": "habit", "Genus": "genus_description",
        "Name Status": "name_status", "Plant Range": "plant_range",
    }
    data.update({key: None for key in fields.values() if key not in data})
    for label, value in re.findall(r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>", html, re.S | re.I):
        key = fields.get(clean(label))
        if key:
            data[key] = clean(value) or None

    data.update({"position": None, "soil_types": None})
    for label, value in re.findall(r'<h2\b[^>]*>(Position|Soil Types)</h2>.*?<div\b[^>]*class="card__bottom"[^>]*>(.*?)</div>', html, re.S):
        data[{"Position": "position", "Soil Types": "soil_types"}[label]] = clean(value) or None

    for label in ("Cultivation", "Propagation", "Pruning", "Pests", "Diseases"):
        match = re.search(r"<h3\b[^>]*>" + label + r"</h3>\s*<p\b[^>]*>(.*?)</p>", html, re.S)
        data[label.lower()] = clean(match[1]) if match else None
    return data


if __name__ == "__main__":
    source = Path(sys.argv[1] if len(sys.argv) > 1 else "141042.html")
    data = extract(source.read_text(encoding="utf-8"))
    data["source_file"] = source.name
    output = source.with_suffix(".json")
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {output}")
