# Contributing

## Development setup

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --extra dev
uv run pytest -q
uv build
```

Keep changes focused and add regression tests for behavior changes. Run the full
test suite before opening a pull request. Update `CHANGELOG.md` for user-visible
changes.

## Releases

Maintainers publish a release by:

1. Updating the version in `pyproject.toml` and `CHANGELOG.md`.
2. Running `uv lock`, `uv run pytest -q`, and `uv build`.
3. Merging the release change into `main`.
4. Creating a GitHub Release for the matching tag `v<version>`.

The release workflow builds the distribution from that release commit and uses PyPI
Trusted Publishing. It does not use a PyPI API token.

Before the first release, create the GitHub repository and configure the PyPI trusted
publisher as documented in the README. The PyPI project name must be available; if
`rspns` is already claimed, choose an available name and change it consistently in
`pyproject.toml`, the PyPI workflow URL, and this documentation.
