from __future__ import annotations

import codecs
import hashlib
import re
from dataclasses import replace
from html.parser import HTMLParser
from typing import Callable

from lxml import etree

from .models import (Context, Document, HookError, Node, ParseError, ResponseInput,
                     Strategy, SummaryConfig, SummaryResult)
from .processing import filter_document
from .rendering import render_document
from .strategies import (HTMLStrategy, JSONStrategy, JavaScriptStrategy, XMLStrategy,
                         mime_format, parse_json, xml_bytes, xml_parser)


class StrategyRegistry:
    def __init__(self):
        self.strategies: dict[str, Strategy] = {
            "html": HTMLStrategy(), "json": JSONStrategy(),
            "xml": XMLStrategy(), "javascript": JavaScriptStrategy(),
        }
        self.recognizers: dict[str, Callable[[ResponseInput, Context], bool]] = {}
        self.media_types: dict[str, str] = {}

    def register(self, format: str, strategy: Strategy, *, recognizer=None, media_types=()):
        if format in {"auto", "empty", "binary", "text"}:
            raise ValueError(f"Reserved format: {format}")
        self.strategies[format] = strategy
        if recognizer is not None:
            self.recognizers[format] = recognizer
        for media in media_types:
            self.media_types[media.lower().split(";", 1)[0].strip()] = format

    def format_for(self, media):
        media = media.split(";", 1)[0].strip().lower()
        return self.media_types.get(media) or (media if media in self.strategies else mime_format(media))


def run_hooks(hooks, value, context, phase, expected):
    for hook in hooks:
        name = getattr(hook, "__qualname__", type(hook).__name__)
        try:
            value = hook(value, context)
            if not isinstance(value, expected):
                raise TypeError(f"Expected {expected.__name__}, got {type(value).__name__}")
        except Exception as exc:
            raise HookError(f"{phase} hook {name}: {exc}") from exc
    return value


def decode(body, media, warnings):
    if isinstance(body, str):
        return body, False
    if not body:
        return "", False
    encoding = None
    for bom, candidate in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                           (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
                           (codecs.BOM_UTF8, "utf-8-sig")):
        if body.startswith(bom):
            encoding = candidate
            break
    if encoding is None:
        match = re.search(r'charset\s*=\s*["\']?([^;\s"\']+)', media, re.I)
        if match:
            encoding = match[1]
    if encoding is None:
        match = re.match(br'\s*<\?xml[^>]*encoding=["\']([^"\']+)', body)
        if match:
            encoding = match[1].decode("ascii", errors="replace")
    try:
        text = body.decode(encoding or "utf-8")
    except (LookupError, UnicodeError):
        warnings.append("Body cannot be decoded with the declared encoding or UTF-8; binary fallback")
        return "", True
    controls = sum(ord(c) < 32 and c not in "\t\r\n" for c in text)
    if "\x00" in text or controls > max(1, len(text) // 100):
        return "", True
    return text, False


def markup_root(text):
    """Read only declarations and the first real start tag, never embedded text."""
    class RootFound(Exception):
        pass

    class Probe(HTMLParser):
        root = None
        attrs = ()
        html_doctype = False

        def handle_decl(self, decl):
            if re.match(r"doctype\s+html(?:\s|$)", decl, re.I):
                self.html_doctype = True

        def handle_starttag(self, tag, attrs):
            self.root, self.attrs = tag, attrs
            raise RootFound

    probe = Probe(convert_charrefs=False)
    try:
        probe.feed(text)
    except RootFound:
        pass
    return probe.root, dict(probe.attrs), probe.html_doctype


class Summarizer:
    def __init__(self, config: SummaryConfig | None = None, *, registry: StrategyRegistry | None = None):
        self.config = config or SummaryConfig()
        self.registry = registry or StrategyRegistry()

    def _detect(self, input, context, header):
        text = input.body.lstrip("\ufeff \t\r\n")
        hint = self.registry.format_for(header)
        if not text:
            return "empty", "empty body"
        for fmt, recognize in self.registry.recognizers.items():
            if recognize(input, context):
                return fmt, "registered recognizer"
        try:
            parse_json(text)
            return "json", "complete JSON parse"
        except (ValueError, RecursionError):
            pass
        if re.match(r"<(?:[A-Za-z_!?]|/[A-Za-z_])", text):
            root_name, root_attrs, html_doctype = markup_root(text)
            if re.match(r"<\?xml\s", text):
                return "xml", "XML declaration"
            if html_doctype or (root_name == "html" and hint != "xml"):
                return "html", "HTML doctype or root"
            xml_valid = False
            try:
                etree.fromstring(xml_bytes(text), xml_parser())
                xml_valid = True
            except (etree.XMLSyntaxError, ValueError):
                pass
            if hint == "xml" and xml_valid:
                return "xml", "XML header validated by parser"
            if hint == "html":
                return "html", "HTML header and markup"
            if root_name == "html":
                return "html", "HTML doctype or root"
            if any(k == "xmlns" or k.startswith("xmlns:") for k in root_attrs):
                return "xml", "XML root namespace"
            context.warnings.append("Ambiguous markup fragment; HTML selected")
            return "html", "markup fragment default"
        js = JavaScriptStrategy()
        if js.recognize(text):
            return "javascript", "JavaScript syntax and structural evidence"
        if hint == "javascript" and not js.parse(text).root_node.has_error:
            return "javascript", "JavaScript header validated by parser"
        return "text", "insufficient format evidence"

    def summarize(self, body: str | bytes, content_type: str = "auto", *, url=None,
                  status_code=None, headers=None, source_id=None) -> str:
        """Return only the reduced body, in the input format."""
        result = self.summarize_result(body, content_type, url=url, status_code=status_code,
                                       headers=headers, source_id=source_id)
        if result.format == "binary" or result.detection["fallback"] == "binary":
            raise ValueError("Binary bodies have no native text summary; use summarize_result() for diagnostics")
        return result.content

    def summarize_result(self, body: str | bytes, content_type: str = "auto", *, url=None,
                         status_code=None, headers=None, source_id=None) -> SummaryResult:
        """Return the reduced body together with optional diagnostics."""
        if not isinstance(body, (str, bytes)):
            raise TypeError("body must be str or bytes")
        if not isinstance(content_type, str):
            raise TypeError("content_type must be a string, including 'auto'")
        original_size = len(body)
        context = Context(self.config, registry=self.registry)
        input = ResponseInput(body, content_type, url, status_code, list(headers or []), source_id)
        original_body = body
        input = run_hooks(self.config.preprocessors, input, context, "preprocessors", ResponseInput)
        if not isinstance(input.body, (str, bytes)):
            raise TypeError("preprocessors must return a str or bytes body")
        if input.body != original_body or type(input.body) is not type(original_body):
            context.source_version = "transformed"
        header_values = [v for k, v in input.headers if k.lower() == "content-type"]
        header = header_values[-1] if header_values else ""
        if len(set(header_values)) > 1:
            context.warnings.append("Conflicting Content-Type headers; last used as hint")
        auto = input.content_type.lower() == "auto"
        text, binary = decode(input.body, header if auto else input.content_type, context.warnings)
        raw = input.body if isinstance(input.body, bytes) else input.body.encode("utf-8", errors="surrogatepass")
        decoded_input = replace(input, body=text.lstrip("\ufeff"))
        if auto:
            fmt, method = ("binary", "non-text body") if binary else self._detect(decoded_input, context, header)
        else:
            fmt = self.registry.format_for(input.content_type) or ("binary" if binary else "text")
            method = "explicit content_type"
        hint = self.registry.format_for(header)
        if auto and hint and fmt not in {hint, "empty"}:
            context.warnings.append(f"Content-Type header indicates {hint}; detected {fmt}")
        fallback = None
        if binary:
            document = Document(fmt, [Node("binary", attributes={"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})])
            if fmt != "binary":
                fallback = "binary"
                context.warnings.append(f"Cannot parse explicit {fmt}: body is binary")
        elif fmt == "empty":
            document = Document(fmt, [])
        elif fmt in self.registry.strategies:
            try:
                document = self.registry.strategies[fmt].extract(decoded_input, context)
            except (ParseError, RecursionError) as exc:
                fallback = "original"
                context.warnings.append(f"{fmt} parse failed: {exc}")
                document = Document(fmt, [Node("unparsed", value=decoded_input.body)], source=decoded_input.body)
        else:
            fallback = "text"
            document = Document(fmt, [Node("text", value=decoded_input.body)])
        document = run_hooks(self.config.document_processors, document, context, "document_processors", Document)
        if fallback == "original":
            if self.config.filters.get(fmt):
                raise ParseError(f"Cannot apply {fmt} filters to an unparseable body")
        else:
            document = filter_document(document, context)
        content = render_document(document, context)
        if (not self.config.document_processors and fmt in {"html", "json", "xml", "javascript"}
                and len(content) >= len(decoded_input.body)
                and not any(r["operation"] != "unwrap" for r in context.reductions)):
            # Canonical quoting/serialization alone should not enlarge an
            # already minimal body. No removed content is restored here.
            content = decoded_input.body
        result = SummaryResult(
            format=fmt, content=content,
            detection={"method": method, "requested": input.content_type, "header": header or None, "fallback": fallback},
            metadata={"url": input.url, "status_code": input.status_code, "headers": input.headers,
                      "source_id": input.source_id, "source_version": context.source_version,
                      "source_sha256": hashlib.sha256(raw).hexdigest(),
                      "reference_semantics": "paths address parsed source; JS offsets address decoded UTF-8; embedded offsets are script-local"},
            warnings=list(dict.fromkeys(context.warnings)), reductions=context.reductions,
            statistics={"input_length": original_size, "decoded_characters": len(text)},
        )
        result = run_hooks(self.config.postprocessors, result, context, "postprocessors", SummaryResult)
        if (not isinstance(result.format, str) or not isinstance(result.content, str)
                or not isinstance(result.detection, dict) or not isinstance(result.metadata, dict)
                or not isinstance(result.warnings, list) or not all(isinstance(w, str) for w in result.warnings)
                or not isinstance(result.reductions, list) or not all(isinstance(r, dict) for r in result.reductions)
                or not isinstance(result.statistics, dict)
                or not all(isinstance(v, int) and v >= 0 for v in result.statistics.values())):
            raise ValueError("Invalid result returned by postprocessor")
        result.statistics["output_characters"] = len(result.content)
        if self.config.postprocessors and result.content and fallback != "original":
            try:
                if result.format == "json":
                    parse_json(result.content)
                elif result.format == "xml":
                    etree.fromstring(xml_bytes(result.content), xml_parser())
                elif result.format == "javascript" and JavaScriptStrategy().parse(result.content).root_node.has_error:
                    raise ValueError("Invalid JavaScript")
            except (ValueError, etree.XMLSyntaxError) as exc:
                raise ValueError(f"Postprocessors produced invalid {result.format} output") from exc
        result.to_dict()
        return result
