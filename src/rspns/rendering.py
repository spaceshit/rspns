"""Serialize reduced documents in their original formats, without envelopes."""
from __future__ import annotations

import html
import json
import re

from lxml import etree

from .models import Document, FilterConfig, ParseError
from .processing import sampled_children, shorten


def json_string(value):
    # Keep Unicode compact while ensuring isolated surrogate escapes remain valid.
    return re.sub(r"[\ud800-\udfff]", lambda m: f"\\u{ord(m[0]):04x}", json.dumps(value, ensure_ascii=False))


def render_json(document, context):
    def render(node):
        if node.kind == "object":
            return "{" + ",".join(json_string(c.name) + ":" + render(c) for c in node.children) + "}"
        if node.kind == "array":
            return "[" + ",".join(render(c) for c in sampled_children(node, context)) + "]"
        if node.kind == "number":
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", node.value):
                raise ValueError("Invalid JSON numeric value returned by document hook")
            return node.value
        if node.kind == "string":
            return json_string(shorten(node.value, node, context))
        if node.kind in {"null", "boolean"}:
            return json.dumps(node.value, allow_nan=False)
        raise ValueError(f"Unsupported JSON node: {node.kind}")

    return render(document.roots[0]) if document.roots else ""


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BOOLEAN_ATTRS = {"disabled", "required", "checked", "selected", "multiple", "readonly", "autofocus", "hidden", "novalidate", "formnovalidate", "async", "defer", "open", "controls", "loop", "muted", "autoplay", "inert", "ismap"}


def html_attributes(attributes):
    result = []
    for name, value in attributes.items():
        if not re.fullmatch(r"[^\s\x00\"'>/=]+", name):
            raise ValueError(f"Invalid HTML attribute name: {name!r}")
        if value is None or (name in BOOLEAN_ATTRS and (value is True or value == "")):
            result.append(" " + name)
        else:
            text = " ".join(map(str, value)) if isinstance(value, list) else str(value)
            result.append(f' {name}="{html.escape(text, quote=True)}"')
    return "".join(result)


def render_html(document, context):
    filters = context.config.filters.get("html", FilterConfig())

    def render(node):
        if node.kind == "text":
            if node.syntax.get("functional_text") or "text" in filters.include_categories:
                value = node.value or ""
                if not node.protected:
                    value = re.sub(r"[\t\n\r\f ]+", " ", value)
                return html.escape(shorten(value, node, context), quote=False)
            if node.value:
                context.reductions.append({"operation": "remove_text", "ref": node.ref})
            return ""
        if node.kind == "comment":
            if "comments" in filters.include_categories:
                return "<!--" + (node.value or "").replace("-->", "--&gt;") + "-->"
            context.reductions.append({"operation": "remove_comment", "ref": node.ref})
            return ""
        if node.kind != "element":
            if node.kind == "doctype":
                context.reductions.append({"operation": "remove_doctype", "ref": node.ref})
            return ""
        tag = node.name
        if not re.fullmatch(r"[A-Za-z][\w:.-]*", tag):
            raise ValueError(f"Invalid HTML tag: {tag!r}")
        attrs = dict(node.attributes)
        explicit = node.syntax.get("explicit")
        if node.syntax.get("secondary_label") and not explicit:
            context.reductions.append({"operation": "remove_secondary_label", "ref": node.ref})
            return ""
        if tag == "style" or (tag == "link" and set(attrs.get("rel", [])) & {"stylesheet", "icon", "apple-touch-icon", "mask-icon", "preconnect", "dns-prefetch"}):
            if not explicit:
                context.reductions.append({"operation": "remove_css", "ref": node.ref})
                return ""
        if tag == "title" and not explicit and not node.syntax.get("functional_text"):
            context.reductions.append({"operation": "remove_title", "ref": node.ref})
            return ""
        if tag == "meta" and not explicit:
            name = str(attrs.get("name", attrs.get("property", ""))).lower()
            if name and not any(part in name for part in ("csrf", "token", "nonce", "security", "referrer")):
                context.reductions.append({"operation": "remove_metadata", "ref": node.ref})
                return ""
        if "style" in attrs and not explicit:
            attrs.pop("style")
            context.reductions.append({"operation": "remove_inline_css", "ref": node.ref})
        if tag == "script":
            fmt = node.syntax.get("embedded_format")
            if fmt:
                embedded = Document(fmt, node.children, source=node.syntax.get("embedded_source", ""))
                source = render_document(embedded, context)
                # A newly shortened JSON string must not introduce a raw end tag.
                if fmt == "json":
                    source = re.sub(r"</script", r"<\\/script", source, flags=re.I)
            else:
                source = node.value or ""
            return f"<script{html_attributes(attrs)}>{source}</script>"
        if tag == "style":
            return f"<style{html_attributes(attrs)}>{node.value or ''}</style>"
        children = "".join(render(child) for child in sampled_children(node, context))
        if tag in {"a", "button", "label", "summary", "legend"}:
            children = children.strip()
        if node.category == "structure" or tag in {"html", "head", "body"}:
            if tag in {"html", "head", "body"} and not attrs and not children and not explicit:
                context.reductions.append({"operation": "unwrap", "ref": node.ref})
                return ""
            functional_attrs = any(k in {"id", "role", "name", "tabindex", "contenteditable", "hidden", "inert"}
                                   or k.startswith("on")
                                   or (k.startswith("aria-") and k != "aria-hidden")
                                   or (k.startswith("data-") and not any(t in k for t in ("i18n", "l10n", "slogan")))
                                   for k in attrs)
            dynamic_label = (node.syntax.get("functional_text") and not children and "class" in attrs
                             and attrs.get("aria-hidden") != "true")
            if not explicit and not functional_attrs and not node.syntax.get("referenced") and not dynamic_label:
                context.reductions.append({"operation": "unwrap" if children else "remove_empty_container", "ref": node.ref})
                return children
        suffix = "" if tag in VOID_TAGS else children + f"</{tag}>"
        return f"<{tag}{html_attributes(attrs)}>" + suffix

    return "".join(render(node) for node in document.roots)


def render_xml(document, context):
    def build(node, preserve_space=False):
        if node.kind == "comment":
            return etree.Comment(node.value or "")
        if node.kind == "instruction":
            return etree.ProcessingInstruction(node.name, node.value or "")
        if node.kind == "entity":
            return etree.Entity(node.name)
        if node.kind != "element":
            raise ValueError(f"Unsupported XML node: {node.kind}")
        attrs = {k: shorten(v, node, context, k) for k, v in node.attributes.items() if k != "$namespaces"}
        nsmap = {k or None: v for k, v in node.attributes.get("$namespaces", {}).items()}
        el = etree.Element(node.name, attrib=attrs, nsmap=nsmap)
        space = node.attributes.get("{http://www.w3.org/XML/1998/namespace}space")
        preserve_space = space == "preserve" or (space != "default" and preserve_space)
        children = sampled_children(node, context)
        mixed = any(c.kind == "text" and (c.value or "").strip() for c in children)
        last = None
        for child in children:
            if child.kind == "text":
                value = shorten(child.value or "", child, context)
                if not preserve_space and not mixed and not value.strip() and ("\n" in value or "\r" in value):
                    continue
                if last is None:
                    el.text = (el.text or "") + value
                else:
                    last.tail = (last.tail or "") + value
            else:
                last = build(child, preserve_space)
                el.append(last)
        return el

    if not any(node.kind == "element" for node in document.roots):
        return ""
    result = []
    for node in document.roots:
        if node.kind == "doctype":
            result.append(node.value)
        else:
            result.append(etree.tostring(build(node), encoding="unicode", with_tail=False))
    return "".join(result)


def render_document(document, context):
    if document.roots and document.roots[0].kind == "binary":
        return ""
    if document.roots and document.roots[0].kind == "unparsed":
        return document.roots[0].value or ""
    strategy = context.registry.strategies.get(document.format)
    if strategy and hasattr(strategy, "render"):
        return strategy.render(document, context)
    if document.format == "json":
        return render_json(document, context)
    if document.format == "html":
        return render_html(document, context)
    if document.format == "xml":
        return render_xml(document, context)
    return "".join(shorten(str(node.value or ""), node, context) for node in document.roots)
