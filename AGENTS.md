# Planning workflow

For planning requests, remain read-only unless implementation is explicitly
requested.

## Orchestrator responsibilities

The main model owns:

- requirement interpretation
- architectural decisions
- conflict resolution
- validation of subagent findings
- final plan quality

Subagents gather evidence only. Their output is not authoritative.

## Exploration policy

Use `local_explorer` first for bounded repository research, including:

- locating files and symbols
- tracing callers and imports
- finding tests
- identifying configuration
- mapping an execution path

Give every explorer:

- one narrow question
- likely directories when known
- a concise output limit
- a requirement to cite paths and symbols

Do not send the explorer the entire conversation when a narrower task is
sufficient.

Use no more than two exploration tasks per planning request unless the task is
exceptionally broad.

Because local inference resources are limited, prefer sequential local
exploration over concurrent local generations.

## Cloud fallback policy

Use `cloud_explorer` only when:

- `local_explorer` reports meaningful uncertainty
- its result lacks repository evidence
- findings are contradictory
- it misses an expected execution path
- the question requires subtle cross-module reasoning
- a load-bearing claim cannot be cheaply verified by the orchestrator

Do not automatically ask both agents the same question.

## Plan output

Write the approved plan to `PLAN.md`.

The plan must be self-contained for an implementation model that has no access
to the planning conversation.

Include:

- objective
- non-goals
- current behavior
- desired behavior
- architectural decisions and rationale
- exact files and symbols involved
- ordered implementation milestones
- tests for each milestone
- exact validation commands
- invariants
- risks
- stop-and-escalate conditions

Do not include raw subagent reports in `PLAN.md`.
Do not begin implementation while operating in planning mode.

# Repository Guidelines

## Project Structure & Module Organization

- `src/document_extract/` contains the installable package and CLI. Pipeline orchestration is in `pipeline/`; layout, Markdown, LLM, table, and image logic live in their corresponding modules.
- `src/document_extract/resources/` contains packaged YAML defaults and prompts. The editable project-level copies are in `config/` and `prompts/`; keep both copies synchronized when changing a bundled prompt or default.
- `tests/` contains package, integration, and post-processing checks.
- `Dockerfile.docling`, `requirements-docling-gpu.txt`, and `START.md` document the GPU/Ollama execution path. Generated PDFs, images, logs, and pipeline outputs should remain untracked.

## Build, Test, and Development Commands

```powershell
python -m pip install -r requirements-docling-gpu.txt
python -m pip install -e .
python -m compileall -q src
python tests/test_package.py
python tests/test_docling_rag_slides.py
python tests/test_slide_postprocess.py
```

The first two commands install dependencies and the editable package. Compileall catches syntax errors; the test scripts exit nonzero on failure. Run the CLI locally with `document_extract report.pdf --output-dir outputs --skip-vlm`. For the full GPU path, follow the Docker commands in `START.md` and ensure Ollama is reachable.

## Coding Style & Naming Conventions

Use Python 3.12+, four-space indentation, type hints, and `snake_case` for modules, functions, and variables. Use `PascalCase` for classes and uppercase names for constants. No formatter or linter is configured in `pyproject.toml`; keep changes PEP 8-compatible and preserve existing type-oriented style. Keep CLI behavior and checkpoint/output formats backward compatible.

## Testing Guidelines

Add focused checks beside the affected area, using `test_*.py` filenames and `test_*` functions. Prefer deterministic, synthetic cases that do not require Docker, GPUs, PDFs, or Ollama; real-artifact checks should skip cleanly when artifacts are absent. Run the affected test script plus `python -m compileall -q src` before submitting.

## Commit & Pull Request Guidelines

Use a concise imperative commit subject, optionally with a conventional prefix such as `refactor:` (example: `refactor: preserve prompt resource loading`). Pull requests should explain the behavior change, list validation commands, identify config/prompt/model changes, and include representative output or screenshots when extraction layout changes. Do not commit secrets, local YAML overrides, model caches, or generated output directories.

## Security & Configuration Tips

Keep Ollama URLs, model names, and runtime tuning in local config or environment variables. Never add credentials to YAML, prompts, logs, or committed examples. Validate changes with `--skip-vlm` first when possible, then test the Ollama path separately.
