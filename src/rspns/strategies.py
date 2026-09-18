from __future__ import annotations

import json
import re
from dataclasses import replace

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, Tag
from lxml import etree
from .models import Context, Document, FilterConfig, Node, ParseError, ResponseInput
from .javascript import JavaScriptStrategy
from .html_analysis import referenced_elements, secondary_label_elements


class Pairs(list):
    """Distinguish JSON objects from arrays, retaining duplicate keys."""


class Number(str):
    pass


def parse_json(text: str):
    def invalid(value):
        raise ValueError(f"Non-JSON constant: {value}")
    return json.loads(text, object_pairs_hook=Pairs, parse_int=Number,
                      parse_float=Number, parse_constant=invalid)


def mime_format(value: str) -> str | None:
    value = value.split(";", 1)[0].strip().lower()
    if value in {"html", "text/html"}:
        return "html"
    if value in {"json", "application/json", "text/json"} or value.endswith("+json"):
        return "json"
    if value in {"xml", "application/xml", "text/xml"} or value.endswith("+xml"):
        return "xml"
    if value in {"js", "javascript", "application/javascript", "text/javascript",
                 "application/ecmascript", "text/ecmascript"}:
        return "javascript"
    return None


class JSONStrategy:
    def render(self, document, context):
        from .rendering import render_json
        return render_json(document, context)

    def extract(self, input: ResponseInput, context: Context) -> Document:
        try:
            data = parse_json(input.body)
        except (ValueError, RecursionError) as exc:
            raise ParseError(str(exc)) from exc

        def walk(value, path=(), occurrence=0, member_path=()):
            ref = {"path": list(path), "occurrence": occurrence, "member_path": list(member_path)}
            if isinstance(value, Pairs):
                node = Node("object", path, ref=ref)
                counts = {}
                for position, (key, val) in enumerate(value):
                    index = counts.get(key, 0)
                    child = walk(val, (*path, key), index, (*member_path, position))
                    child.name = key
                    node.children.append(child)
                    counts[key] = index + 1
                return node
            if isinstance(value, list):
                return Node("array", path, children=[walk(v, (*path, i), member_path=(*member_path, i)) for i, v in enumerate(value)], ref=ref)
            if isinstance(value, Number):
                return Node("number", path, value=str(value), protected=True, ref=ref)
            kind = "null" if value is None else "boolean" if isinstance(value, bool) else "string"
            return Node(kind, path, value=value, ref=ref)

        return Document("json", [walk(data)], source=input.body)


def xml_parser():
    return etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True,
                           recover=False, strip_cdata=False, remove_comments=False)


def xml_bytes(body: str) -> bytes:
    # Input has already been decoded; an old encoding declaration must not
    # reinterpret the new UTF-8 bytes.
    body = re.sub(r'(<\?xml\b[^?]*encoding\s*=\s*)[\'"][^\'"]+[\'"]',
                  r'\1"UTF-8"', body, count=1)
    return body.encode("utf-8")


def doctype_source(source):
    # Locate a real declaration, ignoring lookalikes in comments/PIs. The XML
    # parser has already checked the document's syntax without loading the DTD.
    for match in re.finditer(r"<!--.*?-->|<\?.*?\?>|<!DOCTYPE", source, re.S):
        if match[0] != "<!DOCTYPE":
            continue
        start, pos, quote, depth = match.start(), match.end(), None, 0
        while pos < len(source):
            if not quote and source.startswith("<!--", pos):
                pos = source.index("-->", pos + 4) + 3
                continue
            if not quote and source.startswith("<?", pos):
                pos = source.index("?>", pos + 2) + 2
                continue
            ch = source[pos]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in {'"', "'"}:
                quote = ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            elif ch == ">" and depth == 0:
                return source[start:pos + 1]
            pos += 1
    return ""


class XMLStrategy:
    def render(self, document, context):
        from .rendering import render_xml
        return render_xml(document, context)

    def extract(self, input: ResponseInput, context: Context) -> Document:
        try:
            root = etree.fromstring(xml_bytes(input.body), xml_parser())
        except (etree.XMLSyntaxError, ValueError) as exc:
            raise ParseError(str(exc)) from exc
        tree = root.getroottree()
        filters = context.config.filters.get("xml", FilterConfig())

        def matches(expressions):
            found = set()
            for expression in expressions:
                values = tree.xpath(expression, namespaces=filters.namespaces)
                if not isinstance(values, list) or any(not isinstance(v, etree._Element) for v in values):
                    raise ValueError("XML filters must select nodes, not scalars or attributes")
                found.update(values)
            return found

        included, excluded = matches(filters.include_xpath), matches(filters.exclude_xpath)

        def walk(el, path):
            selection = {"selected": el in included, "excluded": el in excluded}
            if isinstance(el, etree._Entity):
                context.warnings.append("XML entity retained without expansion")
                return Node("entity", path, name=el.name, ref={"path": list(path)}, **selection)
            ref = {"xpath": tree.getpath(el)}
            if isinstance(el, etree._Comment):
                return Node("comment", path, value=el.text, category="comments", ref=ref, **selection)
            if isinstance(el, etree._ProcessingInstruction):
                return Node("instruction", path, name=el.target, value=el.text, ref=ref, **selection)
            attrs = dict(el.attrib)
            if el.nsmap:
                attrs["$namespaces"] = {k or "": v for k, v in el.nsmap.items()}
            node = Node("element", path, name=el.tag, attributes=attrs, ref=ref,
                        selected=el in included, excluded=el in excluded)
            if el.text:
                node.children.append(Node("text", (*path, "text"), value=el.text, ref=ref))
            for i, child in enumerate(el):
                node.children.append(walk(child, (*path, i)))
                if child.tail:
                    node.children.append(Node("text", (*path, i, "tail"), value=child.tail, ref=ref))
            return node

        roots = []
        if tree.docinfo.doctype:
            # Keep the complete internal subset so unresolved entity references
            # remain valid XML; parsing never expands or fetches them.
            roots.append(Node("doctype", value=doctype_source(input.body) or tree.docinfo.doctype))
            context.warnings.append("XML DTD not loaded; entity declarations are not analyzed")
        previous = list(root.itersiblings(preceding=True))
        for i, el in enumerate(reversed(previous)):
            roots.append(walk(el, ("before", i)))
        roots.append(walk(root, ()))
        for i, el in enumerate(root.itersiblings()):
            roots.append(walk(el, ("after", i)))
        return Document("xml", roots, source=input.body)


HTML_CATEGORIES = {
    "form": "forms", "button": "buttons", "a": "links", "area": "links",
    "input": "controls", "select": "controls", "textarea": "controls", "option": "controls",
    "label": "labels", "legend": "labels", "fieldset": "controls", "output": "controls", "optgroup": "controls",
    "script": "scripts", "style": "styles", "meta": "metadata", "base": "metadata",
    "title": "metadata", "link": "resources", "img": "resources", "iframe": "resources",
    "video": "resources", "audio": "resources", "source": "resources", "object": "resources",
    "embed": "resources", "details": "controls", "summary": "buttons",
}


class HTMLStrategy:
    def render(self, document, context):
        from .rendering import render_html
        return render_html(document, context)

    def extract(self, input: ResponseInput, context: Context) -> Document:
        soup = BeautifulSoup(input.body, "html5lib")
        filters = context.config.filters.get("html", FilterConfig())
        included = {id(n) for sel in filters.include_selectors for n in soup.select(sel)}
        excluded = {id(n) for sel in filters.exclude_selectors for n in soup.select(sel)}
        label_ids = {ref for el in soup.find_all(True) for attr in ("aria-labelledby", "aria-describedby")
                     for ref in str(el.get(attr, "")).split()}
        referenced = referenced_elements(soup)
        secondary = secondary_label_elements(soup, referenced)

        def walk(el, path, protected=False, keep_text=False):
            ref = {"dom_path": list(path)}
            if isinstance(el, Doctype):
                return Node("doctype", path, value=str(el), category="metadata", ref=ref)
            if isinstance(el, Comment):
                return Node("comment", path, value=str(el), category="comments", ref=ref)
            if isinstance(el, NavigableString):
                if not str(el).strip() and not (protected or keep_text):
                    return None
                return Node("text", path, value=str(el), protected=protected, ref=ref, category="text",
                            syntax={"functional_text": keep_text})
            if not isinstance(el, Tag):
                return None
            category = HTML_CATEGORIES.get(el.name, "structure")
            if el.name == "input" and el.get("type", "").lower() in {"button", "submit", "reset", "image"}:
                category = "buttons"
            attrs = dict(el.attrs)
            interactive = (any(key.startswith("on") for key in attrs)
                           or attrs.get("role") in {"button", "link", "textbox", "checkbox", "radio", "switch", "option", "menuitem", "tab", "combobox"}
                           or "contenteditable" in attrs)
            keep_text = (keep_text or category in {"buttons", "links", "labels"} or interactive
                         or el.name in {"textarea", "option", "output"} or el.get("id") in label_ids
                         or (category == "structure" and (id(el) in included or el.name in filters.include_tags)))
            node = Node("element", path, name=el.name, attributes=attrs, category=category,
                        ref=ref, selected=id(el) in included, excluded=id(el) in excluded,
                        syntax={"functional_text": keep_text, "interactive": interactive,
                                "referenced": id(el) in referenced,
                                "secondary_label": id(el) in secondary,
                                "explicit": id(el) in included or el.name in filters.include_tags or category in filters.include_categories})
            if el.name in {"script", "style"}:
                raw = "".join(str(child) for child in el.contents)
                if el.name == "style":
                    node.value = raw
                else:
                    if el.get("src"):
                        context.warnings.append("External scripts are retained as references, not fetched")
                    script_type = el.get("type", "").strip().lower()
                    fmt = "javascript" if script_type in {"", "module"} else mime_format(script_type)
                    if raw and fmt in {"json", "javascript"}:
                        try:
                            embedded = context.registry.strategies[fmt].extract(replace(input, body=raw), context)
                            for child in embedded.roots:
                                child.format = fmt
                            node.children = embedded.roots
                            node.syntax["embedded_format"] = fmt
                            node.syntax["embedded_source"] = raw
                        except ParseError as exc:
                            node.value = raw
                            context.warnings.append(f"Embedded {fmt} parse failed: {exc}")
                    else:
                        node.value = raw
                        if raw:
                            context.warnings.append("Unsupported script type retained unchanged")
                return node
            for i, child in enumerate(el.children):
                item = walk(child, (*path, i), protected or el.name in {"textarea", "option"}, keep_text)
                if item is not None:
                    node.children.append(item)
            return node

        roots = [walk(el, (i,)) for i, el in enumerate(soup.children)]
        context.warnings.append("Static HTML analysis; external resources and runtime DOM changes are not analyzed")
        return Document("html", [n for n in roots if n is not None], source=input.body)
