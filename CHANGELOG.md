# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- HTML detection inspects the actual root and doctype rather than namespaces in
  embedded CSS/SVG; misleading namespaces no longer cause an unchanged XML fallback.
- HTML removes class-only layout and empty containers even on scripted pages,
  preserving statically referenced DOM targets. Secondary label descriptions are
  removed when a primary label remains. Added a real-page regression fixture.
- `Summarizer.summarize()` now returns a string in the original response format.
- Optional diagnostics are available through `summarize_result()`; they are never
  embedded into the reduced body.
- HTML removes prose and presentation while retaining controls, labels, links and
  scripts. JavaScript keeps complete functional code and conservatively removes
  recognized presentation statements.
- JSON/XML preserve structure, types, duplicate entries and collection lengths;
  long string values are abbreviated without introducing tables or reference objects.
- Removed the intermediate graph output and its `deduplicate` / `compact_tables`
  options. `SummaryResult.to_json()` now returns native JSON only; diagnostic JSON
  is explicitly available through `to_report_json()`.

## [0.1.0] - 2026-09-18

### Added

- Static, configurable summaries for HTML, JSON, XML and JavaScript HTTP bodies.
- Automatic content detection, filters, reduction controls, extension hooks and custom strategies.
