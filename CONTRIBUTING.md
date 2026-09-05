# Contributing

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```

## Pull requests

- Keep changes focused — one concern per PR.
- Tests are required for new features and bug fixes.
- Run the full lint + test suite before opening a PR.
- Follow the [Google Python style guide](https://google.github.io/styleguide/pyguide.html) for docstrings and formatting (enforced by ruff).

## Release process

1. Merge changes to `main`.
2. Tag with `v<major>.<minor>.<patch>`:

```bash
git tag v0.3.0 && git push origin v0.3.0
```

3. CI builds and publishes the image to `ghcr.io/tedski/ynab-backup:<version>` and `:latest`.