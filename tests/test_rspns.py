import json
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup
from lxml import etree

from rspns import (ANY, Document, FilterConfig, HookError, Node, ParseError,
                   ReductionConfig, StrategyRegistry, Summarizer, SummaryConfig)


@pytest.mark.parametrize("body,expected", [
    ('{"x":1}', "json"), ('[1,true,null]', "json"), ('"hello"', "json"),
    ('123456789012345678901234567890', "json"), ('false', "json"),
    ('<!doctype html><html><body>hi</body></html>', "html"),
    ('<widget>hi</widget>', "html"), ('<?xml version="1.0"?><widget/>', "xml"),
    ('<x:root xmlns:x="urn:test"/>', "xml"), ('const x = () => fetch("/api");', "javascript"),
    ('hello', "text"), ('hello world', "text"), ('', "empty"), (' \n ', "empty"),
    (b'\x00\xffPNG', "binary"),
])
def test_auto_formats(body, expected):
    assert Summarizer().summarize_result(body).format == expected


def test_native_api_and_separate_diagnostics():
    body = '<form action="/login"><input name="user"><button>Login</button></form>'
    summarizer = Summarizer()
    assert summarizer.summarize(body) == body
    result = summarizer.summarize_result(body)
    assert result.content == result.to_text() == str(result) == body
    assert result.statistics['output_characters'] == 70
    assert result.to_dict()['content'] == body
    assert json.loads(result.to_report_json())['format'] == 'html'
    with pytest.raises(ValueError, match='JSON response'):
        result.to_json()
    assert summarizer.summarize_result('{ "x": 1 }').to_json() == '{"x":1}'


def test_misleading_headers_explicit_failure_and_duplicates():
    s = Summarizer()
    result = s.summarize_result('{"ok":true}', headers=[('Content-Type', 'text/html')])
    assert result.format == 'json' and 'header indicates' in ' '.join(result.warnings)
    result = s.summarize_result('<html/>', content_type='application/problem+json; charset=utf-8')
    assert result.format == 'json' and result.content == '<html/>'
    assert result.detection['fallback'] == 'original'
    assert 'parse failed' in ' '.join(result.warnings)
    result = s.summarize_result('{}', headers=[('Content-Type', 'text/html'), ('Content-Type', 'application/json')])
    assert len(result.metadata['headers']) == 2
    assert 'Conflicting' in ' '.join(result.warnings)


def test_encodings_and_binary():
    s = Summarizer()
    assert s.summarize('{"a":"è"}'.encode('utf-16')) == '{"a":"è"}'
    assert s.summarize('<button>è</button>'.encode('latin1'), content_type='text/html; charset=iso-8859-1') == '<button>è</button>'
    assert s.summarize(b'') == ''
    with pytest.raises(ValueError, match='Binary bodies'):
        s.summarize(b'\x00\xff')


def test_json_duplicate_keys_numeric_spelling_and_types():
    body = '{"a":1.0000000000000000001,"a":null,"big":123456789012345678901234567890,"s":"1","z":-0,"e":1e+30}'
    assert Summarizer().summarize(body) == body


def test_json_1000_users_preserve_every_record_and_key():
    users = [{'id': i, 'bio': 'x' * 1000, 'enabled': bool(i % 2)} for i in range(1000)]
    source = json.dumps(users)
    output = Summarizer().summarize(source)
    reduced = json.loads(output)
    assert len(output) < len(source)
    assert len(reduced) == 1000
    assert [user['id'] for user in reduced] == list(range(1000))
    assert all(set(user) == {'id', 'bio', 'enabled'} for user in reduced)
    assert all(user['bio'] == 'x' * 320 + '...' + 'x' * 128 for user in reduced)


def test_json_duplicates_missing_null_and_order_preserved():
    source = '[{"a":null},{},{"a":null},[1,1,2],true,"hello"]'
    assert Summarizer().summarize(source) == source


def test_json_wildcards_filters_sampling_and_protection():
    config = SummaryConfig(
        filters={'json': FilterConfig(exclude_paths=[('users', ANY, 'secret')])},
        reduction=ReductionConfig(protected_paths=[('users', ANY, 'id')], sample_paths={('samples',): 2}),
    )
    data = {'users': [{'id': 'x' * 1000, 'secret': 'hide'}], 'samples': [1, 2, 3], '*': 'literal'}
    result = Summarizer(config).summarize_result(json.dumps(data))
    assert json.loads(result.content) == {'users': [{'id': 'x' * 1000}], 'samples': [1, 2], '*': 'literal'}
    assert any(r['operation'] == 'sample' and r['omitted'] == 1 for r in result.reductions)


def test_json_inclusions_exclusions_and_empty():
    config = SummaryConfig(filters={'json': FilterConfig(include_paths=[('a',)], exclude_paths=[('a', 'secret')])})
    assert Summarizer(config).summarize('{"a":{"keep":true,"secret":1},"b":2}') == '{"a":{"keep":true}}'
    assert Summarizer(SummaryConfig(filters={'json': FilterConfig(exclude_paths=[()])})).summarize('{}') == ''


def test_shortening_unicode_escaping_zero_suffix_and_disabled():
    value = '🙂"\\\n' * 200
    config = SummaryConfig(reduction=ReductionConfig(string_threshold=20, string_prefix=4, string_suffix=0))
    output = Summarizer(config).summarize(json.dumps(value))
    assert json.loads(output) == value[:4] + '...'
    assert Summarizer(SummaryConfig(reduction=ReductionConfig(string_threshold=None))).summarize(json.dumps(value)) == json.dumps(value, ensure_ascii=False)
    assert json.loads(Summarizer().summarize('"\\ud800"')) == '\ud800'


def test_html_strips_prose_css_and_presentation_wrappers():
    source = '''<!doctype html><html><head><title>Some news</title><style>.box{color:red}</style>
    <link rel="stylesheet" href="/style.css"></head><body><h1>Welcome!</h1><p>Long article.</p>
    <div class="layout" style="color:red"><form action="/login"><label for="u">Username</label>
    <input id="u" name="user" required><button><span>Log</span> in</button></form></div></body></html>'''
    output = Summarizer().summarize(source)
    assert output == '<form action="/login"><label for="u">Username</label><input id="u" name="user" required><button>Log in</button></form>'


def test_html_labels_controls_events_and_associations():
    source = '''<h2 id="label">Delete account</h2><p>Remove this prose</p>
    <div role="button" aria-labelledby="label" onclick="confirmDelete()">Delete</div>
    <form id="f"><input type="hidden" name="csrf" value="TOKEN"><select name="choice"><option value="a">Choice A</option></select></form>
    <input form="f" name="outside"><a href="/help">Help</a>'''
    output = Summarizer().summarize(source)
    assert 'Remove this prose' not in output
    parsed = BeautifulSoup(output, 'html5lib')
    assert parsed.find(id='label').text == 'Delete account'
    assert parsed.find(role='button')['onclick'] == 'confirmDelete()'
    assert parsed.find('input', attrs={'name': 'csrf'})['value'] == 'TOKEN'
    assert parsed.find('input', attrs={'name': 'outside'})['form'] == 'f'
    assert parsed.option.text == 'Choice A'
    assert parsed.a.text == 'Help'


@pytest.mark.parametrize('category,absent', [('buttons', 'button'), ('links', 'a'), ('forms', 'form')])
def test_html_category_exclusions(category, absent):
    source = '<form><input name="u"><button>Go</button><a href="/help">Help</a></form>'
    output = Summarizer(SummaryConfig(filters={'html': FilterConfig(exclude_categories={category})})).summarize(source)
    assert BeautifulSoup(output, 'html5lib').find(absent) is None


def test_html_selectors_tags_and_functional_values():
    config = SummaryConfig(filters={'html': FilterConfig(include_selectors=['#f'], exclude_tags={'button'})})
    source = '<form id="f"><textarea name="x">' + 'x' * 1000 + '</textarea><button>Go</button></form><script>fetch("/outside")</script>'
    output = Summarizer(config).summarize(source)
    assert 'button' not in output and '/outside' not in output
    assert 'x' * 1000 in output


def test_html_native_scripts_and_embedded_json():
    source = '<script src="/app.js" defer></script><script>function check(x){return x.length>5;}fetch("/api");</script><script type="application/json">{ "users": [1,1,2] }</script>'
    output = Summarizer().summarize(source)
    scripts = BeautifulSoup(output, 'html5lib').find_all('script')
    assert len(scripts) == 3
    assert scripts[0]['src'] == '/app.js' and 'defer' in scripts[0].attrs
    assert 'function check(x){return x.length>5;}' in scripts[1].string
    assert scripts[2].string == '{"users":[1,1,2]}'
    assert not any('rspns' in key or key.startswith('embedded') for script in scripts for key in script.attrs)


def test_xml_preserves_mixed_content_namespaces_and_entities(tmp_path):
    secret = tmp_path / 'secret'
    secret.write_text('NEVER_READ')
    source = f'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "{secret.as_uri()}">]><r xmlns:t="urn:t">before<t:a k="v">inside</t:a>after&x;</r>'
    output = Summarizer().summarize(source)
    assert 'NEVER_READ' not in output
    parsed = etree.fromstring(output.encode(), etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))
    assert parsed.text == 'before' and parsed[0].tail == 'after'
    assert parsed[0].tag == '{urn:t}a' and parsed[0].get('k') == 'v'


def test_xml_1000_elements_and_long_fields():
    source = '<users>' + ''.join(f'<user id="{i}"><bio>{"x" * 1000}</bio></user>' for i in range(1000)) + '</users>'
    output = Summarizer().summarize(source, content_type='xml')
    root = etree.fromstring(output.encode())
    assert len(root) == 1000
    assert [int(el.get('id')) for el in root] == list(range(1000))
    assert all(el[0].text == 'x' * 320 + '...' + 'x' * 128 for el in root)
    assert len(output) < len(source)


def test_xml_filters_namespaces_and_whitespace():
    source = '<r xmlns:x="urn:x">\n<x:a>keep</x:a>\n<x:b>remove</x:b>\n</r>'
    config = SummaryConfig(filters={'xml': FilterConfig(exclude_xpath=['//x:b'], namespaces={'x': 'urn:x'})})
    output = Summarizer(config).summarize(source)
    assert 'remove' not in output and '\n' not in output
    preserved = '<r xml:space="preserve">\n<a/>\n</r>'
    assert Summarizer().summarize(preserved, content_type='xml') == preserved


def test_hooks_order_native_postprocessing_and_failure():
    called = []
    def pre(value, context):
        called.append('pre')
        return replace(value, body=value.body.removeprefix('PREFIX'))
    def doc(value, context):
        called.append('doc')
        value.roots[0].children[0].value = 'changed'
        return value
    def post(value, context):
        called.append('post')
        value.content = value.content.replace('changed', 'done')
        return value
    config = SummaryConfig(preprocessors=[pre], document_processors=[doc], postprocessors=[post])
    result = Summarizer(config).summarize_result('PREFIX{"name":"original"}')
    assert called == ['pre', 'doc', 'post']
    assert result.content == '{"name":"done"}'
    assert result.metadata['source_version'] == 'transformed'
    assert result.statistics['output_characters'] == len(result.content)
    with pytest.raises(HookError, match='preprocessors'):
        Summarizer(SummaryConfig(preprocessors=[lambda v, c: None])).summarize('{}')


def test_custom_strategy_and_determinism():
    class Custom:
        def extract(self, input, context):
            return Document('custom', [Node('message', value=input.body[4:])])
        def render(self, document, context):
            return 'MSG:' + document.roots[0].value.strip()
    registry = StrategyRegistry()
    registry.register('custom', Custom(), recognizer=lambda i, c: i.body.startswith('MSG:'), media_types=['text/x-custom'])
    summarizer = Summarizer(registry=registry)
    assert summarizer.summarize('MSG: hello ') == 'MSG:hello'
    assert summarizer.summarize('MSG: hello ', content_type='text/x-custom') == 'MSG:hello'
    assert summarizer.summarize_result('{}').to_report_json() == summarizer.summarize_result('{}').to_report_json()


def test_parse_failure_does_not_silently_disable_filters():
    config = SummaryConfig(filters={'json': FilterConfig(exclude_paths=[('secret',)])})
    with pytest.raises(ParseError):
        Summarizer(config).summarize('{broken', content_type='json')
