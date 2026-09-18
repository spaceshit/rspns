"""Syntax-based JavaScript reduction; functional code is kept as source code."""
from __future__ import annotations

import re
from collections import Counter

from tree_sitter import Language, Parser
import tree_sitter_javascript

from .models import Document, Node, ParseError


CATEGORIES = {
    "import_statement": "imports", "export_statement": "exports",
    "function_declaration": "functions", "function_expression": "functions",
    "arrow_function": "functions", "generator_function_declaration": "functions",
    "method_definition": "functions", "class_declaration": "classes", "class": "classes",
    "call_expression": "calls", "new_expression": "calls",
    "assignment_expression": "assignments", "variable_declarator": "variables",
    "string": "strings", "template_string": "strings", "comment": "comments",
    "if_statement": "logic", "switch_statement": "logic", "for_statement": "logic",
    "for_in_statement": "logic", "while_statement": "logic", "do_statement": "logic",
    "try_statement": "logic", "return_statement": "logic", "throw_statement": "logic",
    "break_statement": "logic", "continue_statement": "logic", "with_statement": "logic",
    "ternary_expression": "logic", "await_expression": "logic", "yield_expression": "logic",
}
ATOMIC = {"string", "template_string", "regex"}
PAINT_PROPERTIES = {
    "color", "backgroundColor", "background", "fontSize", "fontFamily", "fontWeight",
    "fontStyle", "textAlign", "textDecoration", "lineHeight", "letterSpacing",
    "borderColor", "borderWidth", "borderRadius", "boxShadow", "textShadow",
    "opacity", "transform", "transition", "animation", "animationDuration",
    "margin", "marginTop", "marginBottom", "marginLeft", "marginRight",
    "padding", "paddingTop", "paddingBottom", "paddingLeft", "paddingRight",
}


def parse(text):
    return Parser(Language(tree_sitter_javascript.language())).parse(text.encode("utf-8"))


def walk_ast(node):
    yield node
    for child in node.named_children:
        yield from walk_ast(child)


def pure_literal(node):
    if node.type in {"number", "string", "true", "false", "null"}:
        return True
    if node.type in {"array", "object", "parenthesized_expression", "unary_expression", "binary_expression"}:
        return all(pure_literal(c) for c in node.named_children)
    if node.type == "pair":
        key, value = node.child_by_field_name("key"), node.child_by_field_name("value")
        return key.type in {"property_identifier", "string", "number"} and pure_literal(value)
    return False


def presentation_detector(root, data):
    def text(n):
        return data[n.start_byte:n.end_byte].decode("utf-8") if n else ""

    declarations = Counter()
    animation_dependencies = False
    paint_reads = set()
    for node in walk_ast(root):
        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            if obj and obj.type == "member_expression" and text(obj.child_by_field_name("property")) == "style":
                parent = node.parent
                if not (parent and parent.type == "assignment_expression" and parent.child_by_field_name("left") == node):
                    paint_reads.add(text(node.child_by_field_name("property")))
            if text(node.child_by_field_name("property")) == "style" and not (
                    node.parent and node.parent.type == "member_expression" and node.parent.child_by_field_name("object") == node):
                animation_dependencies = True
        if node.type == "call_expression":
            function = text(node.child_by_field_name("function"))
            args = node.child_by_field_name("arguments")
            if function.endswith("getComputedStyle") or (function.endswith("getAttribute") and args and text(args) in {'("style")', "('style')"}):
                animation_dependencies = True
        if node.type in {"string", "property_identifier"} and text(node).strip("\"'") in {
                "animationend", "animationstart", "animationiteration", "animationcancel",
                "transitionend", "transitionstart", "transitioncancel", "finish", "cancel",
                "onfinish", "oncancel", "getAnimations"}:
            animation_dependencies = True
        if node.type in {"variable_declarator", "function_declaration", "class_declaration"}:
            name = node.child_by_field_name("name")
            if name:
                for ident in walk_ast(name):
                    if ident.type in {"identifier", "shorthand_property_identifier_pattern"}:
                        declarations[text(ident)] += 1
        elif node.type == "formal_parameters":
            for ident in walk_ast(node):
                if ident.type in {"identifier", "shorthand_property_identifier_pattern"}:
                    declarations[text(ident)] += 1
        elif node.type == "arrow_function":
            parameter = node.child_by_field_name("parameter")
            if parameter:
                declarations[text(parameter)] += 1
        elif node.type == "catch_clause":
            parameter = node.child_by_field_name("parameter")
            if parameter:
                for ident in walk_ast(parameter):
                    if ident.type in {"identifier", "shorthand_property_identifier_pattern"}:
                        declarations[text(ident)] += 1
        elif node.type in {"assignment_expression", "augmented_assignment_expression"}:
            if text(node.child_by_field_name("left")) in {"document", "document.getElementById", "document.querySelector", "document.createElement"}:
                declarations["document"] += 1
        elif node.type == "import_clause":
            for ident in walk_ast(node):
                if ident.type == "identifier":
                    declarations[text(ident)] += 1
    dom_names = set()

    def is_dom(node):
        if not node or declarations["document"]:
            return False
        if node.type == "identifier":
            return text(node) in dom_names
        if node.type == "member_expression":
            return text(node.child_by_field_name("object")) == "document" and text(node.child_by_field_name("property")) in {"body", "documentElement"}
        if node.type != "call_expression":
            return False
        fn = node.child_by_field_name("function")
        return (fn and fn.type == "member_expression"
                and text(fn.child_by_field_name("object")) == "document"
                and text(fn.child_by_field_name("property")) in {"getElementById", "querySelector", "createElement"}
                and all(pure_literal(arg) for arg in node.child_by_field_name("arguments").named_children))

    for node in walk_ast(root):
        if node.type == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name and declarations[text(name)] == 1 and is_dom(value):
                # Reassigned aliases can refer to arbitrary application objects.
                reassigned = any(n.type in {"assignment_expression", "augmented_assignment_expression"}
                                 and text(n.child_by_field_name("left")) == text(name) for n in walk_ast(root))
                if not reassigned:
                    dom_names.add(text(name))

    def presentation(statement):
        if animation_dependencies or statement.type != "expression_statement" or len(statement.named_children) != 1:
            return False
        expr = statement.named_children[0]
        if expr.type == "assignment_expression":
            left, right = expr.child_by_field_name("left"), expr.child_by_field_name("right")
            if left.type != "member_expression" or not pure_literal(right):
                return False
            style = left.child_by_field_name("object")
            return (style.type == "member_expression" and text(style.child_by_field_name("property")) == "style"
                    and is_dom(style.child_by_field_name("object"))
                    and text(left.child_by_field_name("property")) in PAINT_PROPERTIES
                    and text(left.child_by_field_name("property")) not in paint_reads
                    and "cssText" not in paint_reads)
        if expr.type == "call_expression":
            fn, args = expr.child_by_field_name("function"), expr.child_by_field_name("arguments")
            return (fn.type == "member_expression" and text(fn.child_by_field_name("property")) == "animate"
                    and is_dom(fn.child_by_field_name("object")) and args is not None
                    and all(pure_literal(arg) for arg in args.named_children))
        return False

    return presentation


class JavaScriptStrategy:
    parse = staticmethod(parse)

    def recognize(self, text):
        if parse(text).root_node.has_error:
            return False
        return bool(re.search(r"\b(?:const|let|var|function|class|import|export)\s|=>|\b(?:return|throw)\s|[\w.$]+\s*\([^)]*\)\s*;", text))

    def extract(self, input, context):
        data = input.body.encode("utf-8")
        tree = parse(input.body)
        if tree.root_node.has_error:
            raise ParseError("Invalid JavaScript syntax; original source retained")
        presentation = presentation_detector(tree.root_node, data)

        def text(n):
            return data[n.start_byte:n.end_byte].decode("utf-8")

        def convert(n, path=()):
            name = n.child_by_field_name("name")
            if n.type in {"call_expression", "assignment_expression"}:
                name = n.child_by_field_name("function") or n.child_by_field_name("left")
            ref = {"start_byte": n.start_byte, "end_byte": n.end_byte}
            children = [] if n.type in ATOMIC else n.named_children
            gaps, pos = [], n.start_byte
            for child in children:
                gaps.append(data[pos:child.start_byte].decode("utf-8"))
                pos = child.end_byte
            gaps.append(data[pos:n.end_byte].decode("utf-8"))
            return Node(n.type, path, name=text(name) if name else None,
                        value=text(n) if not children else None,
                        children=[convert(child, (*path, i)) for i, child in enumerate(children)],
                        category=CATEGORIES.get(n.type, "syntax"), ref=ref,
                        syntax={"gaps": gaps, "presentation": presentation(n),
                                "statement_list": n.parent is not None and n.parent.type in {"program", "statement_block"}})

        return Document("javascript", [convert(tree.root_node)], source=input.body)

    def render(self, document, context):
        def omitted(node):
            kind = node.syntax.get("original_kind", node.kind)
            if kind in {"program", "method_definition", "comment"}:
                return ""
            if kind in {"statement_block", "class_body"}:
                return "{}"
            if kind.endswith("_statement") or kind in {"function_declaration", "class_declaration", "generator_function_declaration", "lexical_declaration", "variable_declaration"}:
                return ";"
            if kind == "string":
                return '""'
            if kind in {"function_expression", "arrow_function"}:
                return "(()=>{})"
            return "(void 0)"

        def render(node):
            if node.kind == "omitted":
                return omitted(node)
            if context.config.reduction.remove_presentation and node.syntax.get("presentation"):
                context.reductions.append({"operation": "remove_presentation", "ref": node.ref})
                return "" if node.syntax.get("statement_list") else ";"
            if node.kind == "comment":
                return "\n" if "\n" in (node.value or "") else " "
            if not node.children:
                return node.value if node.value is not None else node.syntax.get("gaps", [""])[0]
            gaps = node.syntax["gaps"]
            if node.kind in {"lexical_declaration", "variable_declaration"} and any(c.kind == "omitted" for c in node.children):
                kept = [render(c) for c in node.children if c.kind != "omitted"]
                suffix = ";" if gaps[-1].rstrip().endswith(";") else ""
                return gaps[0] + ",".join(kept) + suffix if kept else suffix
            if node.kind == "export_statement" and all(c.kind == "omitted" for c in node.children):
                return ";"
            return gaps[0] + "".join(render(child) + gaps[i + 1] for i, child in enumerate(node.children))

        source = "".join(render(node) for node in document.roots)
        if parse(source).root_node.has_error:
            raise ParseError("JavaScript filters/hooks would produce invalid syntax; choose a whole declaration or statement")
        return compact(source)


def compact(source):
    """Remove trivia without changing the syntax tree or ASI-sensitive lines."""
    data = source.encode("utf-8")
    root = parse(source).root_node

    def tokens(n):
        if n.type == "comment":
            return
        if n.type in ATOMIC or not n.children:
            yield n
        else:
            for child in n.children:
                yield from tokens(child)

    def signature(n, raw):
        if n.type == "comment":
            return None
        if n.type in ATOMIC or not n.children:
            return (n.type, raw[n.start_byte:n.end_byte])
        return (n.type, tuple(s for child in n.children if (s := signature(child, raw)) is not None))

    original_signature = signature(root, data)
    sequence = list(tokens(root))
    for conservative in (False, True):
        pieces, previous, end = [], "", 0
        for token in sequence:
            value = data[token.start_byte:token.end_byte].decode("utf-8")
            gap = data[end:token.start_byte].decode("utf-8")
            separator = ""
            if previous and ("\n" in gap or "\r" in gap):
                separator = "\n"
            elif previous and gap:
                word_pair = (previous[-1].isalnum() or previous[-1] in "_$\\") and (value[0].isalnum() or value[0] in "_$\\")
                operator_pair = previous[-1] + value[0] in {"++", "--", "//", "/*", "**", "=>", "==", "!=", "<=", ">=", "<<", ">>", "&&", "||", "??", "?.", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^="}
                if conservative or word_pair or operator_pair:
                    separator = " "
            pieces.extend((separator, value))
            previous, end = value, token.end_byte
        result = "".join(pieces)
        encoded = result.encode("utf-8")
        parsed = parse(result).root_node
        if not parsed.has_error and signature(parsed, encoded) == original_signature:
            return result
    return source
