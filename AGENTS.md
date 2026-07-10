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
