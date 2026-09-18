import json

import pytest
from lxml import etree

from rspns import FilterConfig, ReductionConfig, Summarizer, SummaryConfig
from rspns.javascript import parse


def test_no_metadata_in_native_output_and_duplicate_buttons_not_lost():
    body = '<button>Go</button><button>Go</button>'
    assert Summarizer().summarize(body) == body


@pytest.mark.parametrize('body', ["<input name=x>", "<button name='save'>Save</button>"])
def test_already_minimal_markup_not_expanded_by_canonical_quotes(body):
    assert Summarizer().summarize(body) == body


def test_all_json_duplicates_survive_even_after_identical_truncation():
    a = 'a' * 400 + 'X' + 'b' * 400
    b = 'a' * 400 + 'Y' + 'b' * 400
    result = json.loads(Summarizer().summarize(json.dumps([a, b, a])))
    assert len(result) == 3
    assert result[0] == result[1] == result[2]


def test_json_nested_duplicate_keys_not_collapsed():
    source = '{"x":{"y":1},"x":{"y":2}}'
    assert Summarizer().summarize(source) == source


def test_html_script_filter_does_not_leak_source():
    config = SummaryConfig(filters={'html': FilterConfig(include_tags={'form'})})
    output = Summarizer(config).summarize('<form><input name="x"></form><script>fetch("/outside")</script>')
    assert output == '<form><input name="x"></form>'


def test_xml_comments_and_instructions_can_be_filtered():
    config = SummaryConfig(filters={'xml': FilterConfig(exclude_xpath=['//comment()', '//processing-instruction()'])})
    output = Summarizer(config).summarize('<?xml version="1.0"?><?custom hidden?><r><!-- SECRET --><a/></r>')
    assert output == '<r><a/></r>'


def test_xml_xinclude_not_expanded(tmp_path):
    path = tmp_path / 'include.xml'
    path.write_text('<secret>NEVER_EXPAND</secret>')
    source = f'<r xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="{path.as_uri()}"/></r>'
    output = Summarizer().summarize(source)
    assert 'NEVER_EXPAND' not in output
    assert len(etree.fromstring(output.encode())) == 1


def test_xml_encoding_declaration_and_unicode():
    source = '<?xml version="1.0" encoding="ISO-8859-1"?><r>caffè</r>'
    assert Summarizer().summarize(source.encode('latin1')) == '<r>caffè</r>'


def test_invalid_postprocessor_return_type_rejected():
    def invalid(result, context):
        result.content = {'nodes': []}
        return result
    with pytest.raises(ValueError, match='Invalid result'):
        Summarizer(SummaryConfig(postprocessors=[invalid])).summarize('{}')


def test_unparseable_json_kept_instead_of_truncated():
    source = '{broken' + 'x' * 1000
    assert Summarizer().summarize(source, content_type='json') == source


def test_js_inclusion_with_global_declarations():
    config = SummaryConfig(filters={'javascript': FilterConfig(include_symbols={'keep'})})
    output = Summarizer(config).summarize('const extra=1; function keep(){return check(value)} function skip(){}', content_type='js')
    assert 'function keep(){return check(value)}' in output and 'extra' not in output and 'skip' not in output
    assert not parse(output).root_node.has_error


def test_xml_and_json_protected_paths_disable_shortening():
    config = SummaryConfig(reduction=ReductionConfig(protected_paths=[('x',), (0, 'text')]))
    summarizer = Summarizer(config)
    assert json.loads(summarizer.summarize(json.dumps({'x': 'x' * 1000})))['x'] == 'x' * 1000
    xml = '<r><a>' + 'x' * 1000 + '</a></r>'
    assert summarizer.summarize(xml, content_type='xml') == xml


def test_markup_header_and_ambiguous_fragment():
    summarizer = Summarizer()
    assert summarizer.summarize_result('<widget/>', headers=[('Content-Type', 'application/xml')]).format == 'xml'
    result = summarizer.summarize_result('<widget/>')
    assert result.format == 'html' and any('Ambiguous' in w for w in result.warnings)


def test_xml_doctype_comment_delimiters_do_not_break_native_output():
    source = '<?xml version="1.0"?><!-- <!DOCTYPE fake> --><!DOCTYPE r [<!-- ] > --> <!ENTITY x "a">]><r>&x;</r>'
    output = Summarizer().summarize(source)
    assert '<!ENTITY x "a">' in output
    etree.fromstring(output.encode(), etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))


def test_postprocessor_must_preserve_native_syntax():
    def invalid(result, context):
        result.content = '{broken'
        return result
    with pytest.raises(ValueError, match='invalid json output'):
        Summarizer(SummaryConfig(postprocessors=[invalid])).summarize('{}')
