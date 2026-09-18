# rspns

Reduce HTTP response bodies for LLMs **in their original format**:
HTML → HTML, JavaScript → JavaScript, JSON → JSON, XML → XML.

The output is a string containing only the reduced body. No node schema, metadata
envelope, synthetic reference objects or renamed fields are added.

```sh
pip install .
# Development:
uv sync --extra dev
uv run pytest -q
```

Python 3.11+; synchronous, local processing. No LLM, browser or HTTP client required.

## Quick start

```python
from rspns import Summarizer

body = '<form action="/login"><input name="user"><button>Login</button></form>'
output = Summarizer().summarize(body)
print(output)
assert output == body  # Already minimal: nothing to remove.
```

Output:

```html
<form action="/login"><input name="user"><button>Login</button></form>
```

With presentation and ordinary text:

```python
body = """<html><head><style>.card { color: red; }</style></head><body>
<h1>Welcome</h1><p>A long introduction that the LLM does not need.</p>
<div class="card"><form action="/login"><input name="user"><button>Login</button></form></div>
</body></html>"""
assert Summarizer().summarize(body) == '<form action="/login"><input name="user"><button>Login</button></form>'
```

## Per-format behavior

| Input | Output behavior |
|---|---|
| HTML | Remove prose, page headings/titles, comments, CSS and presentation wrappers. Keep forms, controls, links, buttons, their labels and values, ARIA-associated text, event handlers, scripts and functional attributes. Preserve DOM targets identified by literal selectors and references. |
| JavaScript | Keep functions and their complete bodies, validation, conditions, events, network calls and other functional logic. Remove recognized standalone cosmetic style assignments and DOM animations with literal arguments when no animation-event dependency is visible. Compact whitespace/comments without changing the parsed syntax or ASI-sensitive newlines. |
| JSON | Compact whitespace and shorten long string values. Preserve keys, nesting, scalar types, duplicate object keys, exact numeric spellings, array order and **all array entries**, including duplicates. |
| XML | Preserve elements, attributes, namespaces, order and mixed content. Remove indentation where appropriate and shorten long text/attribute values. Keep all repeated elements. Preserve `xml:space` and unresolved entity declarations/references. |

A JSON array with 1,000 users remains an array with 1,000 users. Shortening a field
changes only its string value, for example `"abcdefghilmnopqrtsuvz"` → `"abcd...uvz"`
with suitably configured limits. There is no automatic sampling or global size cap.

These bodies are intended for LLM analysis. Runtime equivalence is not guaranteed:
HTML presentation/text is deliberately removed and JSON/XML values may be shortened.
JavaScript removal is deliberately conservative: unknown APIs, calls with side effects,
animation completion dependencies, display/visibility changes, class toggles and
control state are retained. Long JS literals are kept intact because they may be
validation values, endpoints or other functional data. Third-party animation libraries
are not guessed to be harmless. Scripts remain `<script>` elements containing code;
they are never executed by the library, nor replaced with inert template elements.

Keep the original response if you need to inspect discarded content later. A summary
does not guarantee retention of every possible security clue. No content is fetched
from external script URLs, XML DTDs/entities or XInclude references.

## Detection and input

```python
output = Summarizer().summarize(
    '{ "ok": true }',
    content_type="auto",  # default
    headers=[("Content-Type", "application/json")],
)
assert output == '{"ok":true}'
```

Accepts `str` or already decompressed `bytes`. Byte decoding uses BOMs, declared
charsets, XML encoding declarations or UTF-8. Headers are pairs so duplicates survive.
Optional `url`, `status_code` and `source_id` are recorded only in diagnostics.

`auto` treats Content-Type headers as hints and verifies the body: complete JSON
first, then XML/HTML markup, then JavaScript syntax plus structural evidence.
XML declarations/root namespaces distinguish XML; an HTML doctype or root identifies
HTML. Namespaces inside CSS, scripts or nested SVG do not turn an HTML page into XML.
Ambiguous markup fragments default to
HTML with a diagnostic warning. Explicit MIME types and aliases (`html`, `json`,
`xml`, `js`, `javascript`) select the strategy directly. MIME parameters and
`+json`/`+xml` suffixes are supported.

HTML class-only layout wrappers are unwrapped unless a recognized inline DOM
reference needs them. External scripts are retained but do not force every wrapper
to survive: selectors constructed dynamically or hidden in external code cannot be
inferred. Identifiers, control state and functional attributes remain available.
Within links/buttons/labels, `<small>` and descriptive classes such as `subtitle`
are removed only when another label survives; a sole label is retained. Explicit
tag/selector inclusions can preserve this secondary content.

Malformed JSON/XML/JS is returned unchanged, with a warning available through
`summarize_result()`. Filters on an unparseable body raise `ParseError` rather than
silently failing to exclude content. Empty bodies return an empty string. Unsupported
text receives local string shortening. Binary bodies raise `ValueError` from
`summarize()`; `summarize_result()` provides their diagnostic metadata instead.

## Filters and reduction settings

```python
from rspns import ANY, FilterConfig, ReductionConfig, SummaryConfig, Summarizer

config = SummaryConfig(
    filters={
        "html": FilterConfig(exclude_categories={"buttons", "links"}),
        "json": FilterConfig(exclude_paths=[("users", ANY, "internal_notes")]),
        "xml": FilterConfig(
            exclude_xpath=["//api:debug"], namespaces={"api": "urn:api"},
        ),
        "javascript": FilterConfig(exclude_symbols={"debugLog"}),
    },
    reduction=ReductionConfig(
        string_threshold=512,
        string_prefix=320,
        string_suffix=128,
        protected_paths=[("users", ANY, "id")],
        remove_presentation=True,
    ),
)
output = Summarizer(config).summarize('<form><input name="x"><button>Go</button></form>')
assert output == '<form><input name="x"></form>'
```

- HTML: `include_categories` / `exclude_categories`, `include_tags` / `exclude_tags`,
  `include_selectors` / `exclude_selectors` (CSS).
- JSON: `include_paths` / `exclude_paths`, tuples of keys and integer array indices;
  `ANY` matches a single segment, whereas `"*"` is a literal key.
- XML: `include_xpath` / `exclude_xpath` with a namespace map. Select elements,
  comments or processing instructions, not scalar XPath results or attributes.
- JavaScript: `include_categories` / `exclude_categories` and exact
  `include_symbols` / `exclude_symbols`. Explicit exclusions can remove functional
  code; the renderer rejects combinations that would create invalid syntax.

Exclusions take precedence and remove subtrees. Inclusions retain enough ancestor
context to interpret selected nodes. HTML categories include `forms`, `controls`,
`buttons`, `links`, `labels`, `scripts`, `styles`, `metadata`, `resources`, `comments`,
`text` and `structure`. JavaScript categories include `functions`, `classes`,
`variables`, `imports`, `exports`, `calls`, `assignments`, `strings`, `comments` and
`logic`. HTML inclusion of `text`, a particular presentation tag or selector can
explicitly retain content otherwise removed by default. Explicit `style` inclusion
can retain CSS. Embedded scripts/data use their corresponding language filters.

By default strings longer than 512 characters keep 320 leading characters, `...`,
and 128 trailing characters. Configure `string_threshold=None` to disable shortening.
Keys, numbers, HTML attribute/control values and JS literals stay intact. Protected
paths apply to subtrees. XML paths use child indices and `"text"` / `"tail"` segments.

Sampling is available only by explicit opt-in: `sample_paths={("telemetry",): 10}`
keeps the first ten children at that path. The first matching configured path applies.
Do not enable it for collections whose complete membership you need. XML mixed-content
children include text/tails, so use it on collection containers carefully.

## Optional diagnostics

```python
result = Summarizer().summarize_result('{ "ok": true }')
assert result.content == result.to_text() == '{"ok":true}'
assert result.to_json() == '{"ok":true}'  # JSON response only
assert result.statistics["output_characters"] == len(result.content)
report = result.to_dict()  # Explicit report containing content and diagnostics
```

`summarize_result()` returns a `SummaryResult` with `content`, detected `format`,
warnings, reductions, source metadata and statistics. `to_report_json()` serializes
this optional report; never use it when you want only the reduced body for an LLM.
`to_json()` is reserved for JSON responses and raises on HTML/XML/JS.

The reduction log records dropped sections and original lengths outside the output.
References address parsed DOM paths, ordered JSON member positions, XML paths, or
decoded UTF-8 byte offsets for JavaScript. Embedded script offsets are script-local.
The caller owns source storage. A preprocessor that changes the body marks its source
as `transformed` in the report.

## Python hooks and custom strategies

Pipeline: preprocessors → detection/extraction → document processors → filters →
native rendering → postprocessors → result validation.

```python
from dataclasses import replace

def remove_prefix(response, context):
    return replace(response, body=response.body.removeprefix("PREFIX:"))

def adjust_document(document, context):
    # Node values/attributes/children are available before rendering.
    return document

def adjust_output(result, context):
    result.content = result.content.replace('"internal_name":', '"name":')
    return result

summarizer = Summarizer(SummaryConfig(
    preprocessors=[remove_prefix],
    document_processors=[adjust_document],
    postprocessors=[adjust_output],
))
assert summarizer.summarize('PREFIX:{ "internal_name": "Ada" }') == '{"name":"Ada"}'
```

Hooks receive `(value, context)` and return the same public model: `ResponseInput`,
`Document`, or `SummaryResult`. Empty lists disable a stage; callables run in order.
Failures name the stage and callable in `HookError`. Hooks are trusted Python code.
Document hooks preserve the parsed paths/selectors; JS nodes also have syntax layout
information, so prefer input/output hooks for quick JavaScript transformations.

Register a strategy with `StrategyRegistry.register(name, strategy,
recognizer=callable, media_types=[...])`. It supplies `extract(input, context) ->
Document` and `render(document, context) -> str`. Custom recognizers run before
built-in detection and receive decoded text. Existing format extractors can be replaced.
Raise `ParseError` for malformed content; programming/configuration errors propagate.

## Development and packaging

```sh
uv sync --extra dev
uv run pytest -q
uv run python examples/measure.py
uv build
```

The measurement script compares native input/output character counts. A small
functional form stays identical; an HTML page dominated by CSS/prose can shrink
substantially. A JS file dominated by functional string data can remain almost
unchanged. Preservation takes precedence over an arbitrary compression ratio.

This revision changes the earlier experimental API: replace
`summarizer.summarize(body).to_json()` with `summarizer.summarize(body)`.
Use `summarize_result(body)` only when you also want diagnostics. Removed graph-output
options `deduplicate` and `compact_tables` have no equivalent: native collections
retain their entries and original shape.

## Publishing on GitHub and PyPI

CI tests Python 3.11–3.14 and builds distributions. The release workflow uses PyPI
Trusted Publishing; no PyPI API token is stored in the repository.

Before publishing, create the GitHub repository with `main` as its default branch,
verify the PyPI package name is available, and configure a PyPI trusted publisher
for the repository and workflow **`publish.yml`**, with GitHub environment `pypi`.
Create/protect that environment in GitHub. Add the final repository URL under
`[project.urls]` in `pyproject.toml`. If the package name changes, update it in the
metadata and workflow URL and refresh `uv.lock`.

For a release, update the version/changelog, run `uv lock`, the tests and `uv build`,
then publish the matching GitHub Release `v<version>`. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [SECURITY.md](SECURITY.md). License: [MIT](LICENSE).
