# Docling RAG

`docling-rag` converts PDF reports into page-level, RAG-ready Markdown using
Docling, deterministic layout processing, optional picture triage, table
extraction, and optional Ollama VLM refinement.

The package is CLI-first and preserves the existing checkpoint, replay, and
output formats.

## Installation

Create or activate a virtual environment, then install the pipeline
dependencies and this package:

```powershell
python -m pip install -r requirements-docling-gpu.txt
python -m pip install -e .
```

For an offline environment where build isolation cannot download setuptools:

```powershell
python -m pip install --no-build-isolation -e .
```

Verify the installation:

```powershell
python -c "import docling_rag; print(docling_rag.__version__)"
```

## Basic usage

The canonical entrypoint is:

```powershell
docling-rag report.pdf --output-dir outputs
```

Equivalent forms are also supported:

```powershell
python -m docling_rag report.pdf --output-dir outputs
python docling_rag_slides.py report.pdf --output-dir outputs
```

To run without Ollama/VLM calls:

```powershell
docling-rag report.pdf --skip-vlm
```

To process only selected pages:

```powershell
docling-rag report.pdf --start-page 1 --end-page 5
```

## Command-line options

| Option | Description |
| --- | --- |
| `--output-dir DIR` | Output root directory. |
| `--config FILE` | YAML overlay; may be repeated. |
| `--dpi N` | Rasterization DPI. |
| `--start-page N` | First 1-based page. |
| `--end-page N` | Last page; `0` means the end. |
| `--skip-vlm` | Disable all Ollama calls. |
| `--ollama-base-url URL` | Ollama server URL. |
| `--ollama-model MODEL` | Main multimodal model. |
| `--triage-model MODEL` | Optional faster model for picture triage. |
| `--triage-num-predict N` | Token cap for triage responses. |
| `--triage-confidence N` | Confidence required for specialist routing. |
| `--skip-picture-triage` | Use heuristic routing without the triage VLM call. |
| `--temperature N` | Ollama temperature. |
| `--num-ctx N` | Ollama context size. |
| `--num-predict N` | Main VLM output token cap. |
| `--auto-num-ctx` | Estimate context size per call. |
| `--prompt-file FILE` | Custom page-refinement prompt. |
| `--no-divider-reorder` | Disable deterministic reading-order reconstruction. |
| `--resume-from STAGE` | Resume from a saved checkpoint stage. |

Valid replay stages are:

```text
picture_triage
picture_extract
table_detect
table_extract
page_refine
page_repair
finalize
```

Example:

```powershell
docling-rag report.pdf `
  --output-dir outputs `
  --resume-from page_refine `
  --start-page 1 `
  --end-page 10
```

## Configuration

Configuration precedence is:

```text
bundled defaults
  -> repeated --config YAML files
  -> environment variables
  -> explicit CLI arguments
```

Project-level examples are in `config/`:

```text
config/default.yaml
config/models.yaml
config/detection.yaml
```

Use multiple overlays when needed:

```powershell
docling-rag report.pdf `
  --config config/models.yaml `
  --config config/detection.yaml `
  --config my-run.yaml
```

Supported environment variables include:

```text
OLLAMA_BASE_URL
OLLAMA_MODEL
OLLAMA_TRIAGE_MODEL
DOCLING_RAG_OUTPUT_DIR
DOCLING_RAG_DPI
DOCLING_RAG_NUM_CTX
DOCLING_RAG_NUM_PREDICT
DOCLING_RAG_TRIAGE_MODEL
DOCLING_RAG_TRIAGE_NUM_PREDICT
DOCLING_RAG_TRIAGE_CONFIDENCE
DOCLING_RAG_SKIP_VLM
DOCLING_RAG_SKIP_PICTURE_TRIAGE
DOCLING_RAG_AUTO_NUM_CTX
DOCLING_RAG_DIVIDER_REORDER
```

Prompts are bundled under `src/docling_rag/resources/prompts/`. The editable
copies in `prompts/` are useful for inspection and project-level management.

## Output structure

Each PDF receives its own output directory:

```text
outputs/
└── report/
    ├── manifest.json
    ├── blocks.jsonl
    ├── token_usage.json
    ├── combined_docling_raw.md
    ├── combined_docling_final.md
    └── page_0001/
        ├── page_state.json
        ├── docling_raw.md
        ├── docling_final.md
        ├── layout_prompt_map.json
        ├── table_candidates.json
        ├── image_summaries.jsonl
        └── images/
```

`page_state.json` is the checkpoint used by `--resume-from`. It records stage
history, extracted records, table candidates, warnings, and VLM usage.

## Pipeline stages

The normal page flow is:

```text
prepare
  -> picture_triage
  -> picture_extract
  -> table_detect
  -> table_extract
  -> page_refine
  -> page_repair
  -> finalize
```

Picture triage is a short VLM classification call for eligible images. It can
route images to specialist prompts for tables, charts, grouped values, or
photos. Small and decorative images are filtered before making the call.

## Python integration

The current stable integration surface is the argument-based runner:

```python
from docling_rag.pipeline.runner import run_document

exit_code = run_document([
    "report.pdf",
    "--output-dir",
    "outputs",
    "--skip-vlm",
])
```

Configuration can also be loaded directly:

```python
from docling_rag.config import load_config

config = load_config()
print(config.models.model)
```

## Docker

Build the image:

```powershell
docker build -f Dockerfile.docling -t docling-rag .
```

Run the CLI:

```powershell
docker run --rm --gpus all `
  -v "${PWD}:/workspace" `
  docling-rag `
  /workspace/report.pdf `
  --output-dir /workspace/outputs
```

## Offline replay tools

These tools operate on saved artifacts and do not call Ollama:

```powershell
python replay_reading_order.py outputs/report
python replay_table_detector.py outputs/report
```

## Development checks

```powershell
python -m compileall -q src
python tests/test_package.py
python tests/test_docling_rag_slides.py
python tests/test_slide_postprocess.py
```

The original root-level tests remain available for compatibility as well.
