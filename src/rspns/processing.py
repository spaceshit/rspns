from __future__ import annotations

from dataclasses import replace

from .models import ANY, Context, Document, FilterConfig


def matches(pattern, path, *, subtree=True):
    return (len(pattern) <= len(path) if subtree else len(pattern) == len(path)) and all(
        p is ANY or p == v for p, v in zip(pattern, path)
    )


def shorten(value: str, node, context: Context, field="value") -> str:
    config = context.config.reduction
    if node.protected or any(matches(p, node.path) for p in config.protected_paths):
        return value
    if (config.string_threshold is None or len(value) <= config.string_threshold
            or len(value) <= config.string_prefix + config.string_suffix + 3):
        return value
    context.reductions.append({"operation": "truncate", "ref": node.ref, "field": field,
                               "original_length": len(value)})
    return value[:config.string_prefix] + "..." + (value[-config.string_suffix:] if config.string_suffix else "")


def filter_document(document: Document, context: Context) -> Document:
    def visit(node, fmt, inherited=False):
        if node.format:
            fmt, inherited = node.format, False
        f = context.config.filters.get(fmt, FilterConfig())

        def omit(reason):
            context.reductions.append({"operation": "exclude", "reason": reason, "format": fmt, "ref": node.ref})
            if fmt == "javascript":
                return replace(node, kind="omitted", value=None, children=[],
                               syntax={**node.syntax, "original_kind": node.kind})
            return None

        if (node.excluded or node.category in f.exclude_categories
                or (fmt == "html" and node.name in f.exclude_tags)
                or (fmt == "javascript" and node.name in f.exclude_symbols)
                or any(matches(p, node.path) for p in f.exclude_paths)):
            return omit("explicit filter")
        has_includes = any((f.include_categories, f.include_tags, f.include_selectors,
                            f.include_paths, f.include_xpath, f.include_symbols))
        selected = (inherited or not has_includes or node.selected
                    or node.category in f.include_categories
                    or (fmt == "html" and node.name in f.include_tags)
                    or (fmt == "javascript" and node.name in f.include_symbols)
                    or any(matches(p, node.path) for p in f.include_paths))
        children = []
        for child in node.children:
            if child.format and not selected:
                continue
            kept = visit(child, fmt, selected)
            if kept is not None:
                children.append(kept)
        if not selected and not any(c.kind != "omitted" for c in children):
            return omit("not included")
        return replace(node, children=children, selected=selected,
                       value=node.value if selected else None)

    roots = [v for n in document.roots if (v := visit(n, document.format)) is not None]
    return replace(document, roots=roots)


def sampled_children(node, context):
    for pattern, limit in context.config.reduction.sample_paths.items():
        if matches(pattern, node.path, subtree=False) and len(node.children) > limit:
            context.reductions.append({"operation": "sample", "ref": node.ref,
                                       "omitted": len(node.children) - limit})
            return node.children[:limit]
    return node.children
