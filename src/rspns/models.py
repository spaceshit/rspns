from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class _Wildcard:
    def __repr__(self) -> str:
        return "ANY"


ANY = _Wildcard()
Path = tuple[str | int | _Wildcard, ...]


@dataclass
class FilterConfig:
    include_categories: set[str] = field(default_factory=set)
    exclude_categories: set[str] = field(default_factory=set)
    include_tags: set[str] = field(default_factory=set)
    exclude_tags: set[str] = field(default_factory=set)
    include_selectors: list[str] = field(default_factory=list)
    exclude_selectors: list[str] = field(default_factory=list)
    include_paths: list[Path] = field(default_factory=list)
    exclude_paths: list[Path] = field(default_factory=list)
    include_xpath: list[str] = field(default_factory=list)
    exclude_xpath: list[str] = field(default_factory=list)
    namespaces: dict[str, str] = field(default_factory=dict)
    include_symbols: set[str] = field(default_factory=set)
    exclude_symbols: set[str] = field(default_factory=set)


@dataclass
class ReductionConfig:
    string_threshold: int | None = 512
    string_prefix: int = 320
    string_suffix: int = 128
    protected_paths: list[Path] = field(default_factory=list)
    sample_paths: dict[Path, int] = field(default_factory=dict)
    remove_presentation: bool = True

    def __post_init__(self) -> None:
        if self.string_threshold is not None and self.string_threshold < 1:
            raise ValueError("string_threshold must be positive or None")
        if self.string_prefix < 0 or self.string_suffix < 0:
            raise ValueError("string prefix/suffix must be nonnegative")
        if any(n < 0 for n in self.sample_paths.values()):
            raise ValueError("sample limits must be nonnegative")


@dataclass
class SummaryConfig:
    filters: dict[str, FilterConfig] = field(default_factory=dict)
    reduction: ReductionConfig = field(default_factory=ReductionConfig)
    preprocessors: list[Callable] = field(default_factory=list)
    document_processors: list[Callable] = field(default_factory=list)
    postprocessors: list[Callable] = field(default_factory=list)


@dataclass
class ResponseInput:
    body: str | bytes
    content_type: str = "auto"
    url: str | None = None
    status_code: int | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    source_id: str | None = None


@dataclass
class Node:
    kind: str
    path: tuple = ()
    name: str | None = None
    value: Any = None
    attributes: dict[str, Any] = field(default_factory=dict)
    children: list[Node] = field(default_factory=list)
    category: str | None = None
    ref: dict[str, Any] = field(default_factory=dict)
    protected: bool = False
    # Original selector membership survives document hooks; not serialized.
    selected: bool = False
    excluded: bool = False
    format: str | None = None
    syntax: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Document:
    format: str
    roots: list[Node]
    source: str = ""


@dataclass
class Context:
    config: SummaryConfig
    warnings: list[str] = field(default_factory=list)
    reductions: list[dict[str, Any]] = field(default_factory=list)
    source_version: str = "original"
    registry: Any = None


class Strategy(Protocol):
    def extract(self, input: ResponseInput, context: Context) -> Document: ...
    def render(self, document: Document, context: Context) -> str: ...


class ParseError(ValueError):
    pass


class HookError(RuntimeError):
    pass


@dataclass
class SummaryResult:
    format: str
    content: str
    detection: dict[str, Any]
    metadata: dict[str, Any]
    warnings: list[str]
    reductions: list[dict[str, Any]]
    statistics: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        """An optional diagnostic report, separate from the native output."""
        return json.loads(self.to_report_json())

    def to_json(self) -> str:
        """Return the reduced JSON body (only for a JSON response)."""
        if self.format != "json":
            raise ValueError("Use to_text() for native output; to_json() requires a JSON response")
        return self.content

    def to_report_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def to_text(self) -> str:
        return self.content

    def __str__(self) -> str:
        return self.content
