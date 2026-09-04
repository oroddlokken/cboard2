# cboard2

## Development

```bash
uv sync --group dev
just --list
```

`just lint-all` runs ruff and pyright. `just test` runs the suite; `just test-changed`
runs only what your edits touched.
