import bz2
import importlib.util
import itertools
import json
import sys
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'spark'), str(ROOT / 'tests')]
import wiki_utils
from test_regex_parsing import XML
from test_wiki_utils import TAXONOMY

spec = importlib.util.spec_from_file_location('before', ROOT / 'tests/regex_baseline/wiki_utils_before.py')
before = importlib.util.module_from_spec(spec)
spec.loader.exec_module(before)


def old_rows(path):
    with bz2.open(path, 'rb') as stream:
        events = ElementTree.iterparse(stream, events=('start', 'end'))
        _, root = next(events)
        prefix = root.tag.split('}')[0] + '}' if '}' in root.tag else ''
        for event, page in events:
            if event != 'end' or page.tag != prefix + 'page':
                continue
            revision = page.find(prefix + 'revision')
            redirect = page.find(prefix + 'redirect')
            yield dict(id=int(page.findtext(prefix + 'id')), title=page.findtext(prefix + 'title'),
                       ns=int(page.findtext(prefix + 'ns')),
                       redirect_title=redirect.get('title') if redirect is not None else None,
                       model=revision.findtext(prefix + 'model') if revision is not None else None,
                       text=revision.findtext(prefix + 'text', '') if revision is not None else '')
            page.clear()
            root.clear()


def check():
    report = dict(pages=0, candidates=0, taxonomy=0, archives=[])
    paths = sorted((ROOT / 'data/input/wikipedia').glob('*.xml.bz2'))[:3]
    if not paths:
        raise FileNotFoundError('No local Wikipedia dumps')
    for path, limit in zip(paths, (6000, 500, 500)):
        old = old_rows(path)
        with bz2.open(path, 'rb') as stream:
            new = (XML.xml_record(page) for page in XML.xml_pages(stream))
            for expected, actual in itertools.islice(zip(old, new, strict=True), limit):
                assert actual == expected, (path.name, expected['id'])
                text, title = actual['text'], actual['title']
                if wiki_utils.BOX_RE.search(text):
                    report['candidates'] += 1
                    assert wiki_utils.find_box(text) == before.find_box(text), title
                    for taxonomy in ({}, TAXONOMY):
                        assert wiki_utils.extract_article(title, text, taxonomy) == before.extract_article(title, text, taxonomy), title
                if title.startswith('Template:Taxonomy/'):
                    report['taxonomy'] += 1
                    assert wiki_utils.parse_taxonomy(title, text, actual['redirect_title']) == before.parse_taxonomy(title, text, actual['redirect_title']), title
                report['pages'] += 1
        old.close()
        report['archives'].append(path.name)
        print(json.dumps(report), flush=True)
    (ROOT / 'tests/regex_baseline/comparison.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    check()
