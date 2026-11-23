import re
import unicodedata
from html import unescape


BOX_NAMES = {"taxobox", "speciesbox", "automatic taxobox", "infraspeciesbox",
             "infraspeciesbox special", "subspeciesbox", "infobox cultivar"}
BOX_PATTERN = r"(?i)\{\{\s*(?:taxobox|speciesbox|automatic[ _]taxobox|infraspeciesbox(?:[ _]special)?|subspeciesbox|infobox[ _]cultivar)\s*(?:[|\n]|(?=\}\}))"
BOX_RE = re.compile(BOX_PATTERN)
COMMENTS = re.compile(r"<!--.*?-->", re.S)
REFS = re.compile(r"<ref\b[^>]*?/>|<ref\b[^>]*>.*?</ref\s*>", re.I | re.S)
HEADINGS = re.compile(r"(?m)^(={2,6})\s*(.*?)\s*\1\s*$")
RHS_CODES = {"A", "C", "D", "F", "G", "H", "HT", "M", "S", "T", "Ro", "Fl",
             "Cl", "Min", "Sp", "d", "f", "h", "s", "t", "v"}
HIGH_RANKS = {"genus", "subgenus", "section", "subsection", "series", "family", "subfamily",
              "superfamily", "tribe", "subtribe", "order", "suborder", "superorder",
              "infraorder", "parvorder",
              "class", "subclass", "superclass", "infraclass", "phylum", "subphylum",
              "division", "subdivision", "kingdom", "subkingdom", "clade", "unranked"}
RANK_NAMES = {"familia": "family", "subfamilia": "subfamily", "superfamilia": "superfamily",
              "ordo": "order", "subordo": "suborder", "superordo": "superorder",
              "infraordo": "infraorder", "parvordo": "parvorder", "classis": "class",
              "subclassis": "subclass", "superclassis": "superclass", "infraclassis": "infraclass",
              "regnum": "kingdom", "subregnum": "subkingdom", "divisio": "division",
              "subdivisio": "subdivision", "cladus": "clade", "tribus": "tribe",
              "subtribus": "subtribe", "sectio": "section", "subsectio": "subsection",
              "varietas": "variety", "forma": "form"}
NAME_RE = re.compile(
    r"^(?P<genus>×?\s*[A-Z][a-zA-Z-]+)"
    r"(?:\s+(?P<species>×?\s*[a-z][a-z-]+))?"
    r"(?P<ranks>(?:\s+(?:subsp\.|ssp\.|var\.|f\.|forma)\s+[a-z][a-z-]+)*)"
    r"(?:\s+'(?P<cultivar>.+)')?"
    r"(?P<tail>.*)$"
)
AUTHOR_WORD_RE = re.compile(
    r"(?:[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ.-]*|ex|in|et|al\.|de|van|von|\d{4})"
)

ARTICLE_TEXT_FIELDS = [
    "scientific_name", "taxonomic_rank", "genus", "family", "order",
    "introduction", "description", "plant_range", "habitat", "uses", "cultivation",
]
ARTICLE_LIST_FIELDS = ["scientific_synonyms", "common_names"]
SECTION_PATTERNS = {
    "description": r"^(?:description|appearance|botany)$",
    "plant_range": r"distribution|geographic(?:al)? range|native range",
    "habitat": r"habitat",
    "uses": r"\buses?\b|human usage|culinary",
    "cultivation": r"cultivation|^growing$",
}
TAXOBOX_RANKS = {
    "genus": "genus", "familia": "family", "ordo": "order",
    "classis": "class", "divisio": "division", "regnum": "kingdom",
}


def unique(values):
    result = {}
    for value in values:
        if value:
            result.setdefault(value.casefold(), value)
    return list(result.values())


TEMPLATE_OPEN = re.compile(r"\{\{\{|\{\{")
INNER_TEMPLATE = re.compile(
    r"\{\{\{(?:(?!\{\{|\}\}\}).)*\}\}\}"
    r"|\{\{(?!\{)(?:(?!\{\{|\}\}).)*\}\}", re.S,
)
PARAMETER_TOKEN = re.compile(r"\{\{|\[\[|\|")
NAMED_PARAMETER = re.compile(r"\s*([\w ]+)\s*=")
WIKI_LINK = re.compile(r"\[\[.*?\]\]", re.S)


def balanced_end(text, start):
    remaining = text[start:]
    while True:
        pieces = []
        covered_until = 0
        for opening in TEMPLATE_OPEN.finditer(remaining):
            if opening.start() < covered_until:
                continue
            match = INNER_TEMPLATE.match(remaining, opening.start())
            if match is None:
                continue
            if match.start() == 0:
                return start + match.end()
            pieces.extend((
                remaining[covered_until:match.start()],
                " " * (match.end() - match.start()),
            ))
            covered_until = match.end()
        if not pieces:
            return None
        pieces.append(remaining[covered_until:])
        remaining = "".join(pieces)


def split_parameters(text):
    parts = []
    start = 0
    covered_until = 0
    for token in PARAMETER_TOKEN.finditer(text):
        if token.start() < covered_until:
            continue
        if token[0] == "{{":
            end = balanced_end(text, token.start())
            if end is None:
                break
            covered_until = end
        elif token[0] == "[[":
            link = WIKI_LINK.match(text, token.start())
            covered_until = link.end() if link else len(text)
        else:
            parts.append(text[start:token.start()])
            start = token.end()
    parts.append(text[start:])
    return parts


def template_parts(raw):
    parts = split_parameters(raw[2:-2])
    name = " ".join(parts[0].replace("_", " ").casefold().split())
    named = {}
    positional = []
    for part in parts[1:]:
        match = NAMED_PARAMETER.match(part)
        if match:
            key = match.group(1).strip().casefold().replace(" ", "_")
            named[key] = part[match.end():].strip()
        else:
            positional.append(part.strip())
    return name, named, positional


def find_box(text):
    text = COMMENTS.sub("", text)
    match = BOX_RE.search(text)
    if not match:
        return None
    end = balanced_end(text, match.start())
    if end is None:
        return None
    name, fields, _ = template_parts(text[match.start():end])
    return name, fields


def render_templates(text, depth=0):
    if depth > 24:
        return text
    result = []
    position = 0
    for match in re.finditer(r"\{\{", text):
        if match.start() < position:
            continue
        end = balanced_end(text, match.start())
        if end is None:
            continue
        result.append(text[position:match.start()])
        name, named, values = template_parts(text[match.start():end])
        skip = (name in BOX_NAMES or name.startswith(("infobox", "use ", "short description"))
                or name in {"reflist", "notelist", "taxonbar", "authority control", "clear",
                            "other uses", "for", "for-multi", "about", "redirect", "redirect-several",
                            "good article", "featured article", "pp", "more citations needed"}
                or name.startswith(("cite ", "citation", "sfn", "harv", "refn")))
        if skip:
            value = ""
        elif name in {"lang", "langx", "lang-en", "lang-la", "lang-af", "lang-zh"}:
            value = values[-1] if values else ""
        elif name in {"hybrid", "times"}:
            value = "×"
        elif name in {"hidden begin", "hidden end", "small", "nowrap", "nobr", "plainlist",
                      "unbulleted list", "ubl", "flatlist", "hlist", "collapsible list"}:
            value = "\n".join(values)
        else:
            value = " ".join(values + [v for k, v in named.items()
                                       if k not in {"style", "class", "date", "width", "align"}])
        result.append(render_templates(value, depth + 1))
        position = end
    result.append(text[position:])
    return "".join(result)


def clean_text(text):
    text = COMMENTS.sub("", text or "")
    text = render_templates(REFS.sub("", text))
    text = re.sub(r"\[\[(?:File|Image|Category):[^\n]*?\]\]", "", text, flags=re.I)
    text = re.sub(r"\[\[([^\[\]]+)\]\]", lambda m: m[1].split("|")[-1], text)
    text = re.sub(r"\[(https?://[^\s\]]+)(?:\s+([^\]]+))?\]", lambda m: m[2] or m[1], text)
    text = re.sub(r"<br\s*/?>|</(?:p|div|li)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"'{2,}", "", text)
    text = unescape(text)
    text = "\n".join(re.sub(r"[\t \u00a0]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def inline(text):
    return " ".join(clean_text(text).split())


def normalize_title(title):
    title = " ".join(unescape(title or "").replace("_", " ").split())
    return title[:1].upper() + title[1:]


def parse_name(text, rank_hint=None, allow_bare_author=False):
    text = unicodedata.normalize("NFKC", inline(text))
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "ʼ": "'"}))
    text = re.sub(r"\s*×\s*", " × ", text).strip()
    text = re.sub(r"(?<!\S)x(?=\s)", "×", text)
    text = re.sub(r"\s+\((?:plant|plants)\)(?=\s|$)", "", text)

    code = re.search(r"\s+\(([^()]*)\)$", text)
    if code and all(part in RHS_CODES for part in code[1].split("/")):
        text = text[:code.start()]

    match = NAME_RE.fullmatch(text)
    if not match:
        return None

    tail = match["tail"].strip()
    author_words = re.split(r"[\s(),&]+", tail)
    if tail and any(word and not AUTHOR_WORD_RE.fullmatch(word) for word in author_words):
        return None
    if tail and not allow_bare_author and not re.search(r"[.(),&0-9]", tail):
        return None
    if match["species"] in {"hybrid", "hybrids", "species", "cultivar", "cultivars", "unidentified"}:
        return None

    genus = " ".join(match["genus"].split())
    parts = [genus]
    rank = rank_hint if rank_hint in HIGH_RANKS else "genus"
    if match["species"]:
        parts.append(" ".join(match["species"].split()))
        rank = "species"

    ranks = re.findall(r"(subsp\.|ssp\.|var\.|f\.|forma)\s+([a-z][a-z-]+)", match["ranks"])
    for label, epithet in ranks:
        label = {"ssp.": "subsp.", "forma": "f."}.get(label, label)
        parts.extend([label, epithet])
        rank = {"subsp.": "subspecies", "var.": "variety", "f.": "form"}[label]
    if match["cultivar"]:
        parts.append("'" + " ".join(match["cultivar"].split()) + "'")
        rank = "cultivar"
    name = " ".join(parts)
    return {"name": name, "rank": rank, "key": rank + ":" + name.casefold()}


def rhs_aliases(cell):
    # A backslash quotes the next character. Keep a final backslash literally.
    values = re.findall(r"(?:\\.|[^\\|]|\\\Z)+", cell or "", re.S)
    return [re.sub(r"\\(.)", r"\1", value, flags=re.S) for value in values]


def taxonomy_key(value):
    return " ".join(unescape(value or "").replace("_", " ").split()).removeprefix("Template:Taxonomy/")


def parse_taxonomy(title, text, redirect=None):
    key = taxonomy_key(title)
    if redirect:
        return key, {"same_as": taxonomy_key(redirect)}
    text = COMMENTS.sub("", text)
    text = re.sub(r"<noinclude\b[^>]*>.*?</noinclude\s*>", "", text, flags=re.I | re.S)
    machine = re.search(r"\{\{\s*Don't edit this line\b", text, re.I)
    start = machine.start() if machine else text.find("{{")
    end = balanced_end(text, start) if start >= 0 else None
    if end is None:
        return key, {}
    _, fields, _ = template_parts(text[start:end])
    active = None
    for part in split_parameters(text[start + 2:end - 2])[1:]:
        parameter = NAMED_PARAMETER.match(part)
        if parameter:
            active = parameter[1].strip().casefold()
        elif active == "link":
            fields["link"] += "|" + part.strip()
    node = {k: inline(fields.get(k, "")) for k in ["parent", "rank", "link", "same_as"]}
    rank = node["rank"].casefold()
    node["rank"] = RANK_NAMES.get(rank, rank)
    return key, node


def taxonomy_info(start, taxonomy):
    current = taxonomy_key(start)
    seen = set()
    classification = {}
    plant = None
    for _ in range(100):
        if not current or current in seen:
            break
        seen.add(current)
        node = {}
        alias = current
        aliases_seen = set()
        while alias and alias not in aliases_seen:
            aliases_seen.add(alias)
            if plant is None:
                if alias in {"Plantae", "Viridiplantae", "Embryophyta"}:
                    plant = True
                elif alias in {"Animalia", "Fungi", "Bacteria", "Archaea"}:
                    plant = False
            inherited = taxonomy.get(alias)
            if inherited is None:
                break
            for field in ["parent", "rank", "link"]:
                if not node.get(field) and inherited.get(field):
                    node[field] = inherited[field]
            alias = taxonomy_key(inherited.get("same_as"))
        rank = node.get("rank", "").casefold()
        if rank in {"genus", "family", "order"} and rank not in classification:
            classification[rank] = node.get("link", "").split("|")[-1] or current
        if "rank" not in classification and rank:
            classification["rank"] = rank
        current = taxonomy_key(node.get("parent", ""))
    classification["plant"] = plant
    return classification


def scientific_synonyms(raw):
    raw = REFS.sub("", COMMENTS.sub("", raw or ""))
    candidates = []
    for match in re.finditer(r"\{\{\s*species[ _]list\s*\|", raw, re.I):
        end = balanced_end(raw, match.start())
        if end:
            _, _, values = template_parts(raw[match.start():end])
            candidates.extend(values[::2])
    cleaned = clean_text(raw)
    candidates.extend(re.split(r"\n|\s*;\s*|\*", cleaned))
    names = []
    for candidate in candidates:
        identity = parse_name(candidate.strip(), allow_bare_author=True)
        if identity:
            names.append(identity["name"])
    return unique(names)


def section_text(text, headings, pattern):
    selected = []
    covered_until = -1
    for index, match in enumerate(headings):
        if match.start() < covered_until or not re.search(pattern, inline(match[2]), re.I):
            continue
        end = len(text)
        for following_heading in headings[index + 1:]:
            if len(following_heading[1]) <= len(match[1]):
                end = following_heading.start()
                break
        selected.append(clean_text(text[match.end():end]))
        covered_until = end
    return "\n\n".join(value for value in selected if value)


def extract_article(title, text, taxonomy):
    result = dict.fromkeys(ARTICLE_TEXT_FIELDS, None)
    result.update({key: [] for key in ARTICLE_LIST_FIELDS})
    result["extraction_status"] = "unresolved_identity"

    if title.casefold().startswith(("list of ", "lists of ")):
        result["extraction_status"] = "list"
        return result
    if re.search(r"\{\{\s*(?:disambiguation|disambig|dab|hndis|plant common name)\s*[|}]", text, re.I):
        result["extraction_status"] = "disambiguation"
        return result
    box = find_box(text)
    if not box:
        return result
    box_kind, fields = box
    short_description = re.search(r"\{\{\s*short description\s*\|([^{}]+)\}\}", text, re.I)

    genus = inline(fields.get("genus", ""))
    parent = inline(fields.get("parent", "")) or genus
    candidate_name = (
        inline(fields.get("taxon", ""))
        or inline(fields.get("trinomial", ""))
        or inline(fields.get("binomial", ""))
    )
    if box_kind == "infobox cultivar":
        species = inline(fields.get("species", ""))
        cultivar = inline(fields.get("cultivar", ""))
        if cultivar:
            cultivar = cultivar.strip("'\" ")
            if species.startswith(genus + " "):
                prefix = species
            else:
                prefix = " ".join(filter(None, [genus, species]))
            candidate_name = prefix + " '" + cultivar + "'"
        else:
            candidate_name = title
            title_identity = parse_name(title)
            if not title_identity or title_identity["rank"] != "cultivar":
                return result
    elif not candidate_name and genus:
        candidate_name = " ".join(filter(None, [genus, inline(fields.get("species", ""))]))
    title_fallback = not candidate_name
    candidate_name = candidate_name or title
    if box_kind != "infobox cultivar":
        for key, marker in [("subspecies", "subsp."), ("variety", "var."), ("form", "f.")]:
            value = inline(fields.get(key, ""))
            if value and marker not in candidate_name:
                if not re.fullmatch(r"[a-z][a-z-]+", value):
                    return result
                candidate_name += " " + marker + " " + value

    classification = taxonomy_info(parent or candidate_name, taxonomy)
    rank_hint = classification.get("rank")
    if box_kind == "taxobox":
        for field, rank in TAXOBOX_RANKS.items():
            if inline(fields.get(field, "")).casefold() == candidate_name.casefold():
                rank_hint = rank
                break
    if not rank_hint and short_description:
        rank_word = re.search(r"\b(genus|family|order|clade|division|class|kingdom)\b", short_description[1], re.I)
        rank_hint = rank_word[1].lower() if rank_word else None
    identity = parse_name(candidate_name, rank_hint, allow_bare_author=True)
    if not identity:
        return result

    if title_fallback:
        explicit_names = {inline(fields.get(key, "")).casefold() for key in TAXOBOX_RANKS}
        genus_title = taxonomy_key(NAME_RE.fullmatch(identity["name"])["genus"])
        if (identity["name"].casefold() not in explicit_names
                and taxonomy_key(candidate_name) not in taxonomy and genus_title not in taxonomy):
            return result
    if identity["rank"] in HIGH_RANKS and not rank_hint:
        result["extraction_status"] = "unresolved_taxonomy"
        return result
    if box_kind == "automatic taxobox" and not fields.get("taxon") and taxonomy_key(title) not in taxonomy:
        return result
    if not parent and taxonomy_key(candidate_name) not in taxonomy:
        parent = identity["name"].split()[0]
        classification = taxonomy_info(parent, taxonomy)

    kingdom = inline(fields.get("regnum", fields.get("kingdom", "")))
    plant = classification.get("plant")
    if kingdom in {"Plantae", "Viridiplantae"}:
        if plant is False:
            classification = {}
        plant = True
    elif kingdom in {"Animalia", "Fungi", "Bacteria", "Archaea"}:
        plant = False
    if plant is None and short_description:
        botanical_description = (
            re.search(
                r"\b(?:plants?|conifers?|ferns?|cycads?)(?:\s*$|[,.;:]|"
                r"\s+(?:in|of|from|native|endemic|genus|species|family)\b)", short_description[1], re.I,
            )
            or re.fullmatch(r"Species of (?:nut|tree|shrub|grass|herb)", short_description[1].strip(), re.I)
        )
        nonplant_description = re.search(
            r"\b(?:fungus|fungi|bacteria|bacterium|virus|pathogen|parasite|insect|"
            r"mammal|bug|moth|butterfly|beetle|bird|fish|invertebrate)\b", short_description[1], re.I,
        )
        if botanical_description and not nonplant_description:
            plant = True
    if plant is not True:
        result["extraction_status"] = "non_plant" if plant is False else "unresolved_taxonomy"
        return result

    result.update(scientific_name=identity["name"], taxonomic_rank=identity["rank"],
                  extraction_status="plant")
    for key in ["genus", "family", "order"]:
        manual = {"family": "familia", "order": "ordo"}.get(key, key)
        result[key] = inline(fields.get(manual, fields.get(key, ""))) or classification.get(key) or None
    if identity["rank"] in {"genus", "family", "order"}:
        result[identity["rank"]] = result[identity["rank"]] or identity["name"]
    if not result["genus"] and identity["rank"] in {"genus", "species", "subspecies", "variety", "form", "cultivar"}:
        result["genus"] = re.sub(r"^×\s*", "", identity["name"]).split()[0]

    synonyms = scientific_synonyms(fields.get("synonyms", ""))
    scientific_name = identity["name"].casefold()
    synonym_names = {name.casefold() for name in synonyms}
    result["scientific_synonyms"] = [name for name in synonyms if name.casefold() != scientific_name]

    headings = list(HEADINGS.finditer(text))
    lead = text[:headings[0].start()] if headings else text
    lead_without_boxes = render_templates(REFS.sub("", lead))
    result["introduction"] = inline(lead) or None
    names = [inline(name) for name in re.findall(r"'''([^'\n]+)'''", lead_without_boxes)]
    if normalize_title(title).casefold() not in {scientific_name, *synonym_names}:
        names.insert(0, title)
    scientific_names = {scientific_name, genus.casefold(), *synonym_names}
    result["common_names"] = unique(
        name for name in names if name and len(name) < 120 and name.casefold() not in scientific_names
    )

    for key, pattern in SECTION_PATTERNS.items():
        section = section_text(text, headings, pattern)
        if key == "description":
            section = " ".join(section.split())
        result[key] = section or None
    return result


def rhs_name_keys(primary, alternatives):
    identity = parse_name(primary)
    if not identity:
        return []
    result = [(identity["key"], "scientific_name", primary)]
    for alias in rhs_aliases(alternatives):
        other = parse_name(alias)
        if other and other["rank"] == identity["rank"]:
            result.append((other["key"], "rhs_synonym", alias))
    return list(dict.fromkeys(result))


def scientific_genus(name):
    match = NAME_RE.fullmatch(name or "")
    return " ".join(match["genus"].split()).casefold() if match else None


def wiki_name_keys(scientific_name, rank, synonyms, redirects, common_names=None, known_genera=None):
    identity = parse_name(scientific_name, rank)
    if not identity:
        return []
    result = [(identity["key"], "scientific_name", scientific_name)]
    common = {inline(name).casefold() for name in common_names or []}
    own_genera = {scientific_genus(value) for value in [identity["name"], *(synonyms or [])]}
    for method, values in [("wiki_synonym", synonyms), ("redirect", redirects)]:
        for value in values or []:
            if method == "redirect" and inline(value).casefold() in common:
                continue
            other = parse_name(value, rank)
            if other and other["rank"] == identity["rank"]:
                if method == "redirect":
                    genus = scientific_genus(other["name"])
                    if genus not in own_genera and genus not in (known_genera or set()):
                        continue
                result.append((other["key"], method, value))
    return list(dict.fromkeys(result))
