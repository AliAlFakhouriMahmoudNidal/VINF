import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spark"))

from wiki_utils import (balanced_end, clean_text, extract_article, find_box, parse_name,
                        parse_taxonomy, rhs_aliases, rhs_name_keys, scientific_synonyms,
                        taxonomy_info, wiki_name_keys)


TAXONOMY = {
    "Salvia": {"rank": "genus", "parent": "Lamiaceae", "link": "Salvia"},
    "Mentha": {"rank": "genus", "parent": "Lamiaceae"},
    "Lamiaceae": {"rank": "family", "parent": "Lamiales"},
    "Lamiales": {"rank": "order", "parent": "Core eudicots"},
    "Core eudicots": {"rank": "clade", "parent": "Plantae"},
    "core eudicots": {"same_as": "Core eudicots"},
    "Plantae": {"rank": "kingdom", "parent": "Eukaryota"},
    "Prunus": {"rank": "genus", "parent": "Rosaceae"},
    "Rosaceae": {"rank": "family", "parent": "Plantae"},
    "Olea": {"rank": "genus", "parent": "Plantae"},
    "Proboscidea": {"rank": "order", "parent": "Animalia"},
    "Proboscidea (plant)": {"rank": "genus", "parent": "Martyniaceae", "link": "Proboscidea (plant)|Proboscidea"},
    "Martyniaceae": {"rank": "family", "parent": "Plantae"},
    "Animalia": {"rank": "kingdom"},
}
ROSEMARY = """{{Short description|Species of plant}}
{{Speciesbox
| genus = Salvia
| species = rosmarinus<ref>{{cite journal|title=A reference|year=2017}}</ref>
| synonyms = {{Species list|Rosmarinus officinalis|L.|Rosmarinus communis|Noronha}}
}}
'''''Salvia rosmarinus''''', commonly known as '''rosemary''', is an [[evergreen]] shrub.
== Description ==
Grows to {{convert|1.2|to|1.8|m|ft}}. Leaves are green.<ref>A citation</ref>
== Distribution and habitat ==
Native to the [[Mediterranean Basin|Mediterranean]].
== Cultivation ==
Cultivated in gardens.
=== Cultivars ===
Named selections exist.
== Uses ==
Used in cooking.
== External links ==
[https://example.org More information]
"""


class IdentityTests(unittest.TestCase):
    def test_long_invalid_author_suffix(self):
        self.assertIsNone(parse_name("Selaginella " + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 100 + "!", allow_bare_author=True))

    def test_authors_codes_hybrids_and_unicode(self):
        for name, expected in [
            ("Salvia rosmarinus (Ro)", "species:salvia rosmarinus"),
            ("Abelia chinensis R. Br.", "species:abelia chinensis"),
            ("Podocarpus latifolius (Thunb.) R.Br. ex Mirb.", "species:podocarpus latifolius"),
            ("Mentha x piperita", "species:mentha × piperita"),
            ("Mentha ×piperita", "species:mentha × piperita"),
            ("Salvia rosmarinus ‘Wye’", "cultivar:salvia rosmarinus 'wye'"),
            ("Rosa 'L'Haÿ'", "cultivar:rosa 'l'haÿ'"),
            ("Olea europaea ssp. europaea", "subspecies:olea europaea subsp. europaea"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(parse_name(name)["key"], expected)

    def test_rank_and_cultivar_are_not_discarded(self):
        self.assertNotEqual(parse_name("Olea europaea")["key"], parse_name("Olea europaea subsp. europaea")["key"])
        self.assertNotEqual(parse_name("Salvia rosmarinus")["key"], parse_name("Salvia rosmarinus 'Wye'")["key"])
        self.assertIsNone(parse_name("Agapanthus africanus hybrid"))
        self.assertIsNone(parse_name("Rosa canina deep pink (S)"))
        self.assertIsNone(parse_name("rose of the sea"))
        self.assertIsNone(parse_name("Rosa Peace"))
        self.assertIsNone(parse_name("Agapanthus hybrid"))
        self.assertIsNone(parse_name("Salvia rosmarinus Blue"))
        self.assertEqual(scientific_synonyms("Salvia fasciculata Fernald"), ["Salvia fasciculata"])

    def test_rhs_alias_encoding(self):
        self.assertEqual(rhs_aliases(r"name\|part|back\\slash|Salvia rosmarinus"),
                         ["name|part", "back\\slash", "Salvia rosmarinus"])

    def test_broad_alias_cannot_weaken_a_cultivar(self):
        keys = rhs_name_keys("Salvia rosmarinus 'Wye'", "Salvia rosmarinus|rosemary")
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0][0].startswith("cultivar:"))
        self.assertEqual(rhs_name_keys("Unknown plant hybrid", "Salvia rosmarinus"), [])

    def test_renamed_species_and_redirect_keys(self):
        rhs = {row[0] for row in rhs_name_keys("Prunus dulcis", "almond|Prunus amygdalus")}
        wiki = {row[0] for row in wiki_name_keys("Prunus amygdalus", "species", ["Prunus dulcis"], ["Sweet almond"], ["Sweet almond"])}
        self.assertIn("species:prunus amygdalus", rhs & wiki)
        self.assertNotIn("genus:almond", rhs)
        self.assertNotIn("species:sweet almond", wiki)
        unlabelled = {row[0] for row in wiki_name_keys("Prunus amygdalus", "species", [], ["Sweet almond"])}
        self.assertNotIn("species:sweet almond", unlabelled)
        renamed = wiki_name_keys("Prunus amygdalus", "species", [], ["Amygdalus dulcis"], known_genera={"amygdalus"})
        self.assertIn("species:amygdalus dulcis", {row[0] for row in renamed})


class WikiParsingTests(unittest.TestCase):
    def test_taxonomy_subpage_case_and_parent_overrides(self):
        key, node = parse_taxonomy("Template:Taxonomy/core eudicots", "", "Template:Taxonomy/Core eudicots")
        self.assertEqual(key, "core eudicots")
        taxonomy = dict(TAXONOMY)
        taxonomy.update({"Core eudicots": {"parent": "Plantae", "rank": "clade"}, key: node,
                         "Ailanthus": {"rank": "genus", "parent": "Core eudicots"},
                         "AlternateSalvia": {"same_as": "Salvia", "parent": "Animalia"}})
        self.assertTrue(taxonomy_info("Ailanthus", taxonomy)["plant"])
        self.assertFalse(taxonomy_info("AlternateSalvia", taxonomy)["plant"])
        taxonomy["Eukaryota"] = {"parent": "Archaea"}
        self.assertTrue(taxonomy_info("Salvia", taxonomy)["plant"])

    def test_plant_and_animal_taxonomy_collision(self):
        plant = extract_article("Proboscidea (plant)", "{{Automatic taxobox|taxon=Proboscidea (plant)}}", TAXONOMY)
        self.assertEqual(plant["extraction_status"], "plant")
        self.assertEqual(plant["taxonomic_rank"], "genus")
        self.assertEqual(plant["family"], "Martyniaceae")
        manual = extract_article("Proboscidea (plant)", "{{Taxobox|regnum=Plantae|genus=Proboscidea}}", TAXONOMY)
        self.assertEqual(manual["taxonomic_rank"], "genus")
        self.assertIsNone(manual["order"])
        partial = dict(TAXONOMY, Martyniaceae={"rank": "family", "parent": "Missing clade"})
        plant = extract_article("Proboscidea (plant)", "{{Short description|Genus of plants}}{{Automatic taxobox|taxon=Proboscidea (plant)}}", partial)
        self.assertEqual(plant["extraction_status"], "plant")
        self.assertEqual(plant["family"], "Martyniaceae")

    def test_common_title_is_not_invented_as_a_binomial(self):
        unknown = extract_article("Tree fern", "{{Taxobox|regnum=Plantae|ordo=Cyatheales}}", {})
        self.assertEqual(unknown["extraction_status"], "unresolved_identity")
        implicit = extract_article("Salvia rosmarinus", "{{Speciesbox}}", TAXONOMY)
        self.assertEqual(implicit["scientific_name"], "Salvia rosmarinus")

    def test_nested_box_keeps_fields_after_references(self):
        name, fields = find_box(ROSEMARY)
        self.assertEqual(name, "speciesbox")
        self.assertIn("Rosmarinus officinalis", fields["synonyms"])
        self.assertEqual(scientific_synonyms(fields["synonyms"]), ["Rosmarinus officinalis", "Rosmarinus communis"])

    def test_taxonomy_machine_code_and_aliases(self):
        raw = "{{Don't edit this line {{{machine code|}}}|rank=genus|link=Salvia|parent=Lamiaceae}}"
        self.assertEqual(balanced_end(raw, 0), len(raw))
        key, node = parse_taxonomy("Template:Taxonomy/Salvia", raw)
        self.assertEqual(key, "Salvia")
        self.assertEqual(node["parent"], "Lamiaceae")
        real_order = "<noinclude>{{High-use}}</noinclude>{{Don't edit this line {{{machine code|}}}|parent=Plantae|rank=ordo|link=Lamiales}}"
        _, order = parse_taxonomy("Template:Taxonomy/Lamiales", real_order)
        _, family = parse_taxonomy("Template:Taxonomy/Lamiaceae", "{{Don't edit this line {{{machine code|}}}|rank=familia|parent=Lamiales|link=Lamiaceae}}")
        self.assertEqual(order["rank"], "order")
        self.assertEqual(family["rank"], "family")
        _, root = parse_taxonomy("Template:Taxonomy/Plantae", "<noinclude>{{High-use}}</noinclude>{{Don't edit this line {{{machine code|}}}|rank=regnum|link=Plant|Plantae|parent=Archaeplastida}}")
        self.assertEqual(root["link"], "Plant|Plantae")
        self.assertEqual(root["rank"], "kingdom")
        actual_ranks = dict(TAXONOMY, Lamiales=order, Lamiaceae=family)
        self.assertEqual(taxonomy_info("Salvia", actual_ranks)["order"], "Lamiales")
        taxonomy = dict(TAXONOMY, OldSalvia={"same_as": "Salvia"})
        self.assertEqual(taxonomy_info("OldSalvia", taxonomy)["family"], "Lamiaceae")
        self.assertIsNone(taxonomy_info("A", {"A": {"parent": "B"}, "B": {"parent": "A"}})["plant"])

    def test_realistic_rosemary_selected_fields(self):
        plant = extract_article("Rosemary", ROSEMARY, TAXONOMY)
        self.assertEqual(plant["extraction_status"], "plant")
        self.assertEqual(plant["scientific_name"], "Salvia rosmarinus")
        self.assertEqual(plant["family"], "Lamiaceae")
        self.assertEqual(plant["order"], "Lamiales")
        self.assertIn("rosemary", [name.lower() for name in plant["common_names"]])
        self.assertIn("Mediterranean", plant["habitat"])
        self.assertIn("Named selections", plant["cultivation"])
        self.assertIn("Used in cooking", plant["uses"])
        self.assertNotIn("full_text", plant)
        self.assertIn("evergreen shrub", plant["introduction"])
        self.assertNotIn("A citation", plant["description"])
        self.assertNotIn("{{", plant["description"])
        self.assertNotIn("More information", str(plant))
        self.assertIn("1.2 to 1.8 m ft", plant["description"])

    def test_missing_description_does_not_use_full_article(self):
        plant = extract_article("Rosemary", ROSEMARY.replace("== Description ==", "== History =="), TAXONOMY)
        self.assertIsNone(plant["description"])
        self.assertNotIn("full_text", plant)
        self.assertIn("evergreen shrub", plant["introduction"])

    def test_nonplants_disambiguation_and_lists(self):
        elephant = "{{Automatic taxobox|taxon=Proboscidea}}Elephants."
        self.assertEqual(extract_article("Proboscidea", elephant, TAXONOMY)["extraction_status"], "non_plant")
        self.assertEqual(extract_article("Unknown", "{{Speciesbox|taxon=Unknown unknown}}", {})["extraction_status"], "unresolved_taxonomy")
        self.assertEqual(extract_article("List of plants", ROSEMARY, TAXONOMY)["extraction_status"], "list")
        self.assertEqual(extract_article("Rosemary", "{{disambiguation}}" + ROSEMARY, TAXONOMY)["extraction_status"], "disambiguation")
        pathogen = "{{Short description|Species of plant pathogen}}{{Speciesbox|taxon=Unknown unknown}}"
        self.assertEqual(extract_article("Unknown", pathogen, {})["extraction_status"], "unresolved_taxonomy")
        for description in ["Species of plant bug", "Moth found on plants"]:
            animal = "{{Short description|" + description + "}}{{Speciesbox|taxon=Unknown unknown}}"
            self.assertEqual(extract_article("Unknown", animal, {})["extraction_status"], "unresolved_taxonomy")

    def test_higher_rank_is_not_mislabeled_as_genus(self):
        plant = extract_article("Lamiaceae", "{{Automatic taxobox|taxon=Lamiaceae}}Mint family.", TAXONOMY)
        self.assertEqual(plant["taxonomic_rank"], "family")
        self.assertIsNone(plant["genus"])
        keys = wiki_name_keys(plant["scientific_name"], plant["taxonomic_rank"], [], [])
        self.assertEqual(keys[0][0], "family:lamiaceae")
        sample = extract_article("Asteraceae", "{{Short description|Large family of flowering plants}}{{Automatic taxobox|taxon=Asteraceae}}", {})
        self.assertEqual(sample["taxonomic_rank"], "family")
        self.assertIsNone(sample["genus"])
        bare = extract_article("Salvia", "{{Automatic taxobox}}", TAXONOMY)
        self.assertEqual(bare["scientific_name"], "Salvia")

    def test_cultivar_group_is_not_invented_as_a_species_or_genus(self):
        text = "{{Short description|Species of edible plant}}{{Infobox cultivar|genus=Apium|species=Apium graveolens|group=Dulce Group}}"
        self.assertEqual(extract_article("Celery", text, {})["extraction_status"], "unresolved_identity")

    def test_manual_taxobox_family_and_rank(self):
        raw = "{{Taxobox|regnum=Plantae|genus=Protea|binomial=Protea cynaroides|familia=Proteaceae}}A plant."
        plant = extract_article("Protea cynaroides", raw, {})
        self.assertEqual(plant["family"], "Proteaceae")
        genus = extract_article("Taxus", "{{Taxobox|regnum=Plantae|genus=Taxus}}", {})
        family = extract_article("Lamiaceae", "{{Taxobox|regnum=Plantae|familia=Lamiaceae}}", {})
        self.assertEqual(genus["taxonomic_rank"], "genus")
        self.assertEqual(family["taxonomic_rank"], "family")

    def test_infraspecies_and_cultivar_boxes(self):
        plant = extract_article("Olive", "{{Infraspeciesbox|genus=Olea|species=europaea|subspecies=europaea}}", TAXONOMY)
        self.assertEqual(plant["scientific_name"], "Olea europaea subsp. europaea")
        self.assertEqual(plant["taxonomic_rank"], "subspecies")
        traditional = extract_article("Olive", "{{Taxobox|regnum=Plantae|binomial=Olea europaea|trinomial=Olea europaea subsp. europaea}}", TAXONOMY)
        self.assertEqual(traditional["scientific_name"], plant["scientific_name"])
        self.assertEqual(traditional["taxonomic_rank"], "subspecies")
        declared = extract_article("Olive", "{{Taxobox|regnum=Plantae|binomial=Olea europaea|subspecies=europaea}}", TAXONOMY)
        self.assertEqual(declared["scientific_name"], plant["scientific_name"])
        cultivar = extract_article("Wye rosemary", "{{Infobox cultivar|genus=Salvia|species=Salvia rosmarinus|cultivar='Wye'}}", TAXONOMY)
        self.assertEqual(cultivar["scientific_name"], "Salvia rosmarinus 'Wye'")
        self.assertEqual(cultivar["taxonomic_rank"], "cultivar")

    def test_synonym_bullets_links_and_multiline_text(self):
        raw = "{{hidden begin|title=List}}\n* ''Mentha × odora'' <small>Salisb.</small>\n* ''Mentha × balsamea'' <small>Willd.</small>\n{{hidden end}}"
        self.assertEqual(scientific_synonyms(raw), ["Mentha × odora", "Mentha × balsamea"])
        self.assertEqual(clean_text("[[Plant|Displayed]] &amp; ''text''\n\nNext paragraph."), "Displayed & text\n\nNext paragraph.")

    def test_article_fields_keep_identity_without_removed_columns(self):
        for suffix, expected_rank in [
            ("|subspecies=europaea", "subspecies"),
            ("|variety=europaea", "variety"),
            ("|form=europaea", "form"),
        ]:
            with self.subTest(rank=expected_rank):
                article = "{{Taxobox|regnum=Plantae|binomial=Olea europaea" + suffix + "}}"
                result = extract_article("Olive", article, TAXONOMY)
                self.assertEqual(result["taxonomic_rank"], expected_rank)
                self.assertFalse({"subspecies", "variety", "cultivar", "redirect_aliases"} & result.keys())

    def test_nested_and_repeated_sections_keep_boundaries(self):
        article = """{{Speciesbox|genus=Salvia|species=rosmarinus}}
First paragraph.

Second paragraph.
== Description ==
Outer description.
=== Appearance ===
Nested description.
== History ==
Excluded history.
== Botany ==
Another description.
== Distribution and habitat ==
Shared paragraph.

Second habitat paragraph.
"""
        result = extract_article("Rosemary", article, TAXONOMY)
        self.assertEqual(result["introduction"], "First paragraph. Second paragraph.")
        self.assertEqual(result["description"],
                         "Outer description. === Appearance === Nested description. Another description.")
        self.assertEqual(result["habitat"], "Shared paragraph.\n\nSecond habitat paragraph.")
        self.assertEqual(result["plant_range"], result["habitat"])


if __name__ == "__main__":
    unittest.main()
