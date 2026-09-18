import pytest

from rspns import FilterConfig, ReductionConfig, Summarizer, SummaryConfig
from rspns.javascript import parse


def summarize(source, config=None):
    output = Summarizer(config).summarize(source, content_type='js')
    assert not parse(output).root_node.has_error
    return output


def test_function_bodies_validation_and_events_survive():
    source = '''function validate(x) { if (!x || x.length < 8) { throw new Error("Too short"); } return true; }
    const button = document.querySelector("#submit");
    button.addEventListener("click", async () => { if (validate(value)) { await fetch("/save", {method:"POST",body:value}); } });'''
    output = summarize(source)
    for part in ('function validate', 'x.length<8', 'throw new Error', 'addEventListener', '"click"', 'await fetch', 'method:"POST"', 'body:value'):
        assert part in output
    assert len(output) < len(source)


def test_known_presentation_removed_but_state_and_network_preserved():
    source = '''const el = document.getElementById("x");
    el.style.color = "red";
    el.animate([{opacity:0},{opacity:1}], {duration:1000});
    el.style.display = "none";
    el.style.pointerEvents = "none";
    el.disabled = true;
    el.addEventListener("click", () => fetch("/api"));'''
    output = summarize(source)
    assert 'style.color' not in output and '.animate' not in output
    for part in ('style.display', 'style.pointerEvents', 'disabled', 'addEventListener', 'fetch'):
        assert part in output


@pytest.mark.parametrize('extra', [
    'el.addEventListener("animationend", submit);',
    'el.getAnimations().forEach(a => a.onfinish = submit);',
])
def test_animation_with_functional_dependency_preserved(extra):
    output = summarize('const el=document.querySelector("#x");el.animate([{opacity:1}],100);' + extra)
    assert '.animate' in output and 'submit' in output


@pytest.mark.parametrize('source', [
    'const el = document.querySelector("#x"); el.style.color = fetch("/x");',
    'const el = document.querySelector("#x"); el.animate(frames, {duration:notify()});',
    'const el = document.querySelector("#x"); const anim=el.animate([{opacity:1}],100);anim.finished.then(submit);',
    'const el = service.getObject(); el.animate([{opacity:1}],100);',
    'function f(document){ const el=document.querySelector("#x"); el.style.color="red"; }',
    'const el=document.querySelector("#x"); function f(el){el.style.color="red";}',
    'const el=document.querySelector("#x");el=service;el.style.color="red";',
])
def test_uncertain_or_side_effectful_presentation_kept(source):
    output = summarize(source)
    assert 'style.color' in output or '.animate' in output


def test_disable_presentation_removal():
    source = 'document.body.style.color="red";'
    assert summarize(source, SummaryConfig(reduction=ReductionConfig(remove_presentation=False))) == source


@pytest.mark.parametrize('inspection', [
    'if(el.style.color === "red") submit();',
    'send(el.style);',
    'send(el.style.cssText);',
    'if(getComputedStyle(el).color) submit();',
    'send(el.getAttribute("style"));',
])
def test_presentation_state_read_by_functional_logic_is_kept(inspection):
    source = 'const el=document.querySelector("#x");el.style.color="red";' + inspection
    assert 'el.style.color="red"' in summarize(source)


@pytest.mark.parametrize('source', [
    'function f(){return\n{ok:true}}',
    'let a=1,b=2; a + +b; a - -b;',
    'const url="https://example.com/a//b"; const re=/a\\/b/g; console.log(url,re);',
    'const t=`hello  ${user}\n  world`; send(t);',
    'function f(){return /* line\n comment */ {x:1};}',
    'const café = "è"; send(café);',
    'let x=1\nx++\nconsole.log(x)',
    'const n = 1 .toString();',
    'const jsx = <button onClick={save}>Save</button>;',
])
def test_lexical_edge_cases_parse_after_reduction(source):
    output = summarize(source)
    assert output
    if 'return\n' in source or 'return /* line' in source:
        assert 'return\n' in output
    if 'const t=' in source:
        assert '`hello  ${user}\n  world`' in output


def test_long_validation_and_endpoint_strings_not_truncated():
    long = 'x' * 1000
    source = f'if(token==="{long}"){{fetch("/{long}");}}'
    assert summarize(source) == source


def test_filter_symbols_inside_export_and_multi_declaration():
    config = SummaryConfig(filters={'javascript': FilterConfig(exclude_symbols={'secret', 'debug'})})
    output = summarize('export function secret(){fetch("/hidden")} function keep(){return 1} const debug=1,value=2;', config)
    assert 'secret' not in output and '/hidden' not in output and 'debug' not in output
    assert 'function keep(){return 1}' in output and 'value=2' in output


def test_included_function_keeps_body_and_dependencies_as_written():
    config = SummaryConfig(filters={'javascript': FilterConfig(include_symbols={'keep'})})
    output = summarize('function keep(){return check(value)}function skip(){fetch("/skip")}', config)
    assert 'function keep(){return check(value)}' in output
    assert 'skip' not in output


def test_invalid_javascript_returns_original_with_separate_warning():
    source = 'function broken( {'
    result = Summarizer().summarize_result(source, content_type='js')
    assert result.content == source and result.detection['fallback'] == 'original'
    assert 'parse failed' in ' '.join(result.warnings)


def test_embedded_js_filters_and_presentation():
    config = SummaryConfig(filters={'javascript': FilterConfig(exclude_symbols={'debug'})})
    source = '<script>function debug(){fetch("/debug")}document.body.style.color="red";function keep(){return validate();}</script>'
    output = Summarizer(config).summarize(source)
    assert output.startswith('<script>') and output.endswith('</script>')
    assert 'style.color' not in output and '/debug' not in output
    assert 'function keep(){return validate();}' in output
