from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from rspns import FilterConfig, Summarizer, SummaryConfig


@pytest.mark.parametrize('body', [
    '<!DOCTYPE html><html><style>i{background:url("data:image/svg+xml,<svg xmlns=\'urn:svg\'>")}</style><button>Go</button></html>',
    '<html><svg xmlns="http://www.w3.org/2000/svg"></svg><button>Go</button></html>',
    '<!-- <fake xmlns="urn:fake"> --><html><button>Go</button></html>',
    '<div><svg xmlns="http://www.w3.org/2000/svg"></svg></div>',
    '<div title="xmlns=wrong"><button>Go</button></div>',
])
def test_nested_or_quoted_namespaces_do_not_select_xml(body):
    result = Summarizer().summarize_result(body)
    assert result.format == 'html'
    assert result.detection['fallback'] is None


@pytest.mark.parametrize('body', [
    '<root xmlns="urn:actual"><item/></root>',
    '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"/>',
])
def test_actual_xml_evidence_still_selects_xml(body):
    assert Summarizer().summarize_result(body).format == 'xml'


def test_html_doctype_overrides_misleading_xml_header():
    result = Summarizer().summarize_result(
        '<!DOCTYPE html><html><input name="q"></html>',
        headers=[('Content-Type', 'application/xml')],
    )
    assert result.format == 'html'
    assert result.warnings


def test_user_portal_fixture_keeps_every_functional_entry():
    body = Path(__file__).with_name('test.html').read_text(encoding='utf-8')
    result = Summarizer().summarize_result(body)
    assert result.format == 'html'
    assert result.content == Summarizer().summarize(body, content_type='html')
    original = BeautifulSoup(body, 'html5lib')
    reduced = BeautifulSoup(result.content, 'html5lib')
    for tag in ('a', 'input', 'button', 'select', 'option', 'form', 'script'):
        before, after = original.find_all(tag), reduced.find_all(tag)
        assert len(before) == len(after), tag
        for left, right in zip(before, after):
            for attr in ('id', 'name', 'value', 'href', 'action', 'src', 'type', 'required', 'selected'):
                assert left.get(attr) == right.get(attr), (tag, attr)
    assert [n.get_text() for n in original.find_all('option')] == [n.get_text() for n in reduced.find_all('option')]
    assert not reduced.find('style')
    assert not reduced.select('[style]')
    assert not reduced.select('.central-textlogo, .styled-select-active-helper')
    assert reduced.select_one('#js-link-box-en').get_text(strip=True) == 'English'
    assert len(reduced.find_all('div')) < 40
    assert len(result.content) < len(body) * 0.4  # Specific CSS-heavy fixture, not a universal budget.


@pytest.mark.parametrize(('body', 'expected'), [
    ('<a href="/x"><small>Only label</small></a>', '<a href="/x">Only label</a>'),
    ('<a href="/x">English<small>1,000 articles</small></a>', '<a href="/x">English</a>'),
    ('<button><span>Log</span>\n  <span>in</span></button>', '<button>Log in</button>'),
])
def test_labels_keep_primary_text(body, expected):
    assert Summarizer().summarize(body) == expected


def test_secondary_markup_never_drops_controls_or_referenced_labels():
    body = '<label>Name<input class="secondary" name="q"><small id="hint">Hint</small></label>'
    output = Summarizer().summarize(body)
    assert '<input class="secondary" name="q">' in output
    assert 'Hint' in output


def test_external_script_does_not_preserve_all_layout_wrappers():
    body = '<div class="layout"><span class="empty"></span><button>Go</button></div><script src="/app.js"></script>'
    assert Summarizer().summarize(body) == '<button>Go</button><script src="/app.js"></script>'


def test_literal_selectors_preserve_targets_and_required_ancestors():
    body = '<div class="layout"><div class="parent"><span class="target"></span></div><div class="unused"></div></div><script>document.querySelector(".parent > .target").addEventListener("click",go)</script>'
    output = Summarizer().summarize(body)
    soup = BeautifulSoup(output, 'html5lib')
    assert soup.select_one('.parent > .target') is not None
    assert soup.select_one('.unused') is None


def test_inline_handler_and_aria_references_keep_targets():
    body = '<div class="target"></div><span id="label">Search</span><button aria-labelledby="label" onclick="document.querySelector(\'.target\').click()">Go</button>'
    output = Summarizer().summarize(body)
    assert 'class="target"' in output
    assert '<span id="label">Search</span>' in output


def test_explicit_tag_inclusion_can_keep_secondary_labels():
    config = SummaryConfig(filters={'html': FilterConfig(include_tags={'a', 'small'})})
    output = Summarizer(config).summarize('<a href="/">English<small>Article count</small></a>')
    assert '<small>Article count</small>' in output


def test_minimal_form_is_unchanged():
    body = '<form action="/login"><input name="user"><button>Login</button></form>'
    assert Summarizer().summarize(body) == body
