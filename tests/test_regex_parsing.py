import ast
import bz2
import io
import re
import sys
import types
import unittest
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'spark'))
import wiki_utils


def load_xml_helpers():
    path = ROOT / 'spark/spark_data.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in {'xml_pages', 'xml_record', 'xml_unescape'}
             or isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith('XML_') for t in node.targets)]
    module = types.ModuleType('xml_helpers')
    module.__dict__.update(re=re, Row=lambda **fields: fields)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), module.__dict__)
    return module


XML = load_xml_helpers()


def original_records(data):
    root = ElementTree.fromstring(data)
    prefix = root.tag.split('}')[0] + '}' if '}' in root.tag else ''
    result = []
    for page in root.findall(prefix + 'page'):
        revision = page.find(prefix + 'revision')
        redirect = page.find(prefix + 'redirect')
        result.append(dict(id=int(page.findtext(prefix + 'id')), title=page.findtext(prefix + 'title'),
                           ns=int(page.findtext(prefix + 'ns')),
                           redirect_title=redirect.get('title') if redirect is not None else None,
                           model=revision.findtext(prefix + 'model') if revision is not None else None,
                           text=revision.findtext(prefix + 'text', '') if revision is not None else ''))
    return result


class RegexXmlTests(unittest.TestCase):
    def test_chunk_boundaries_entities_and_nested_ids(self):
        data = '''<mediawiki xmlns="urn:wiki"><siteinfo><sitename>Wiki</sitename></siteinfo>
<page><title>Žltý &amp; &lt;rose&gt;</title><ns>0</ns><id>12</id><redirect title='A &quot;B&quot; &#x17D; &#10; C'/>
<revision><id>900</id><contributor><id>999</id></contributor><model>wikitext</model>
<text xml:space="preserve">{{Speciesbox|taxon=Rosa alba}} &#65; &amp;lt; &lt;/page&gt;\r\nLine</text></revision></page>
<page><title>Empty</title><ns>1</ns><id>13</id><revision><text deleted="deleted" /></revision></page>
<page><title>No revision</title><ns>0</ns><id>14</id></page></mediawiki>'''.encode()
        expected = original_records(data)
        for size in (1, 2, 7, 31, 1024):
            with self.subTest(chunk_size=size):
                actual = [XML.xml_record(page) for page in XML.xml_pages(io.BytesIO(data), size)]
                self.assertEqual(actual, expected)
        compressed = bz2.compress(data)
        with bz2.BZ2File(io.BytesIO(compressed)) as stream:
            self.assertEqual([XML.xml_record(page) for page in XML.xml_pages(stream, 13)], expected)

    def test_comments_cdata_and_long_text(self):
        body = 'x' * 200000 + '<![CDATA[literal </page> </revision> </text> &amp;]]>'
        data = ('<mediawiki><!-- <page><id>wrong</id></page> -->'
                '<page><title>A</title><ns>0</ns><id>1</id><!-- <revision>wrong</revision> -->'
                '<revision><text>' + body + '</text></revision></page></mediawiki>').encode()
        self.assertEqual([XML.xml_record(page) for page in XML.xml_pages(io.BytesIO(data), 97)], original_records(data))

    def test_truncated_input_rejected(self):
        for data in (b'', b'<mediawiki><page>', b'<mediawiki>', b'<mediawiki><!-- unfinished'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                list(XML.xml_pages(io.BytesIO(data), 2))

    def test_read_size_is_bounded(self):
        class LimitedStream(io.BytesIO):
            def read(self, size=-1):
                self.assert_size(size)
                return super().read(size)
        stream = LimitedStream(b'<mediawiki></mediawiki>')
        stream.assert_size = lambda size: self.assertEqual(size, 17)
        self.assertEqual(list(XML.xml_pages(stream, 17)), [])


class RegexTemplateTests(unittest.TestCase):
    def test_deep_templates_and_parameters(self):
        text = '{{outer|a=' + '{{inner|' * 40 + 'value' + '}}' * 40 + '|b=[[A|B]]|c=end}}'
        self.assertEqual(wiki_utils.balanced_end(text, 0), len(text))
        parts = wiki_utils.template_parts(text)
        self.assertEqual(parts[1]['b'], '[[A|B]]')
        self.assertEqual(parts[1]['c'], 'end')

    def test_machine_parameters_and_unclosed_templates(self):
        text = '{{outer|a={{{parameter|{{small|default}}}}}|b=end}}'
        self.assertEqual(wiki_utils.balanced_end(text, 0), len(text))
        self.assertEqual(wiki_utils.template_parts(text)[1]['b'], 'end')
        self.assertIsNone(wiki_utils.balanced_end('{{outer|{{inner}}', 0))
        self.assertEqual(wiki_utils.split_parameters('a|{{unclosed|b|c'), ['a', '{{unclosed|b|c'])


if __name__ == '__main__':
    unittest.main()
