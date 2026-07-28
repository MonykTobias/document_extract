# Contributing

## Project layout

- `src/document_extract/` is the installable package and CLI. Orchestration
  lives in `pipeline/`; layout, Markdown, LLM, table, and image logic live in
  the matching modules.
- `src/document_extract/resources/` holds the packaged YAML defaults and
  prompts. `config/` and `prompts/` are editable project-level copies of the
  same files. **Change both together** — `tests/test_resource_sync.py` asserts
  they are byte-identical.
- `tests/` holds package, integration, and post-processing checks.
- `tools/reprocess_pages.py` replays the deterministic post-processing chain
  over saved artifacts. It is not part of the installed package.
- `Dockerfile.docling`, `requirements-docling-gpu.txt`, and `START.md` document
  the GPU/Ollama path.

Generated PDFs, images, logs, and pipeline output directories stay untracked.

## Setup

```powershell
python -m pip install -r requirements-docling-gpu.txt
python -m pip install -e .
```

`docling` and `torch` are deliberately **not** declared in `pyproject.toml`, so
the package installs light for tests and CI. The CLI reports a missing docling
install with the command above rather than failing inside the convert worker.

## Checks to run before submitting

```powershell
python -m compileall -q src
python tests/run_all.py
python -m mypy --follow-imports=silent src/document_extract/markdown src/document_extract/layout src/document_extract/config.py src/document_extract/runtime.py src/document_extract/models.py
```

These are exactly what CI runs. `--follow-imports=silent` is required: without
it mypy also reports errors in every transitively imported module, which is the
set this check deliberately excludes.

## Coding style

Python 3.12+, four-space indentation, type hints, `snake_case` for modules,
functions, and variables, `PascalCase` for classes, uppercase for constants.
No formatter or linter is configured; keep changes PEP 8-compatible and match
the surrounding type-oriented style.

## Testing

Tests are plain scripts, not pytest: a `test_*.py` file with `test_*` or
`check_*` functions and a `main()` that returns a nonzero exit code on failure.
`tests/run_all.py` discovers and runs them all.

- Prefer deterministic synthetic cases. **No test may require Docker, a GPU, a
  PDF, Docling, or Ollama.** Checks against real artifacts must skip cleanly
  when the artifacts are absent.
- Add checks beside the affected area rather than in a new file, unless the
  area genuinely has none.
- A behaviour change needs a test that fails without it. Verify that by
  reverting the change locally before submitting.

## Compatibility rules

These are load-bearing; breaking one silently costs a user their data or a
re-run of an expensive job.

- **Checkpoint format.** `CHECKPOINT_SCHEMA_VERSION` is `1`. New `PageState`
  fields must be optional with a default so older checkpoints stay loadable.
- **`token_usage.json` schema is frozen.** It is read outside this repository
  for cost tracking. Additions only — never remove or repurpose a key, even one
  that is always zero.
- **`RECONSTRUCTION_VERSION`** gates checkpoint reuse: bumping it forces a
  `table_detect` replay of every saved page. Only bump it when the reconciler's
  output genuinely changes. `test_table_reconstruction.py` pins both the
  version and a digest of the reconciler's output for all six fixtures.
- **CLI behaviour and output formats** stay backward compatible. User-input
  errors print one `error: ...` line to stderr and exit `1`; anything else
  keeps its traceback.
- **Prompt templates** render with `str.format`. Any literal brace must be
  escaped as `{{` or `}}`; `assert_formattable` enforces this at load time.

## Concurrency

Configuration is applied to module-level globals, so **one extraction per
process**. Sequential calls in one process are fine because each reapplies its
configuration; concurrent threads are not supported. Parallelism is achieved
with process shards (`--shard`), not threads.

## Commits and pull requests

Use a concise imperative subject with an optional conventional prefix
(`fix:`, `feat:`, `refactor:`, `chore:`, `docs:`, `test:`, `ci:`).

A pull request should explain the behaviour change, list the validation
commands that were run, identify any config/prompt/model changes, and include
representative output when extraction layout changes.

Never commit secrets, local YAML overrides, model caches, or generated output
directories. Ollama URLs, model names, and runtime tuning belong in local
config or environment variables. The remote-Ollama auth token is read only from
`DOCLING_RAG_OLLAMA_AUTH_TOKEN` so it cannot end up in a committed file or a
process listing.
