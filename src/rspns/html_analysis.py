"""Identify concrete DOM references and secondary label text without execution."""
from __future__ import annotations

import ast
import re

import soupsieve
from bs4 import Comment, Tag

from .javascript import parse, walk_ast


def referenced_elements(soup):
    referenced = set()
    ids = set()
    for el in soup.find_all(True):
        for attr in ("aria-labelledby", "aria-describedby", "aria-controls", "aria-owns", "for", "form", "list", "popovertarget", "commandfor"):
            ids.update(str(el.get(attr, "")).split())
        href = el.get("href", "")
        if href.startswith("#") and len(href) > 1:
            ids.add(href[1:])
    sources = ["".join(map(str, el.contents)) for el in soup.find_all("script")
               if not el.get("type") or el.get("type") in {"module", "text/javascript", "application/javascript"}]
    sources.extend(str(value) for el in soup.find_all(True) for name, value in el.attrs.items() if name.startswith("on"))
    for source in sources:
        data = source.encode("utf-8")
        def text(node):
            return data[node.start_byte:node.end_byte].decode("utf-8") if node else ""
        for node in walk_ast(parse(source).root_node):
            if node.type == "member_expression" and text(node) in {"document.body", "document.documentElement"}:
                target = soup.body if text(node) == "document.body" else soup.html
                if target:
                    referenced.add(id(target))
            if node.type != "call_expression":
                continue
            fn, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
            if not fn or fn.type != "member_expression" or not args or not args.named_children:
                continue
            method = text(fn.child_by_field_name("property"))
            if method not in {"querySelector", "querySelectorAll", "getElementById", "getElementsByClassName", "getElementsByName", "getElementsByTagName"}:
                continue
            arg = args.named_children[0]
            if arg.type != "string":
                continue
            try:
                value = ast.literal_eval(text(arg))
            except (ValueError, SyntaxError):
                continue
            if not isinstance(value, str):
                continue
            if method == "getElementById":
                ids.add(value)
                continue
            if method in {"querySelector", "querySelectorAll"}:
                try:
                    matches = soup.select(value)
                except soupsieve.SelectorSyntaxError:
                    continue
            elif method == "getElementsByClassName":
                tokens = set(value.split())
                matches = [el for el in soup.find_all(True) if tokens and tokens <= set(el.get("class", []))]
            elif method == "getElementsByName":
                matches = soup.find_all(attrs={"name": value})
            else:
                matches = soup.find_all(True if value == "*" else value)
            for target in matches:
                referenced.add(id(target))
                # Combinator selectors depend on ancestry/sibling context.
                if method in {"querySelector", "querySelectorAll"} and re.search(r"[\s>+~]", value):
                    for ancestor in target.parents:
                        if isinstance(ancestor, Tag) and ancestor.name != "[document]":
                            referenced.add(id(ancestor))
                    if "+" in value or "~" in value:
                        referenced.update(id(s) for s in target.previous_siblings if isinstance(s, Tag))
    referenced.update(id(el) for el in soup.find_all(id=True) if el["id"] in ids)
    return referenced


def secondary_label_elements(soup, referenced):
    """Drop explicit secondary markup only when a separate label survives."""
    removed = set()
    secondary_tokens = {"description", "subtitle", "tagline", "caption", "secondary"}
    for owner in soup.find_all(["a", "button", "label", "summary"]):
        candidates = []
        for el in owner.find_all(True):
            tokens = {p for c in el.get("class", []) for p in re.split(r"[-_]", c.lower())}
            if el.name != "small" and not tokens & secondary_tokens:
                continue
            if (id(el) in referenced or el.name in {"input", "button", "select", "textarea", "a", "output"}
                    or el.find(["input", "button", "select", "textarea", "a", "output"])
                    or el.has_attr("id") or any(k.startswith("on") for k in el.attrs)):
                continue
            candidates.append(el)
        candidate_ids = {id(el) for el in candidates}
        primary_text = any(str(text).strip() and not isinstance(text, Comment)
                           and not any(id(parent) in candidate_ids for parent in text.parents)
                           for text in owner.find_all(string=True))
        if primary_text:
            removed.update(candidate_ids)
    return removed
