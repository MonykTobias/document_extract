# document_extract

`document_extract` converts PDF reports into page-level, RAG-ready Markdown
using Docling, deterministic layout processing, optional picture triage, table
extraction, and optional Ollama VLM refinement.

The Python package is currently named `document_extract` and the installed CLI is
`document_extract`. The project is named `document_extract`.

The package is CLI-first and preserves the existing checkpoint, replay, and
output formats.

## Supported input formats

| Format | Status | Notes |
| --- | --- | --- |
| PDF (`.pdf`) | Supported | Main input format, including text-based and scanned/image-heavy reports. |
| DOCX (`.docx`) | Not supported directly | Convert to PDF first. |
| PowerPoint (`.pptx`, `.ppt`) | Not supported directly | Export slides to PDF first. |
| Excel (`.xlsx`, `.xls`, `.csv`) | Not supported directly | Export the relevant content to PDF first. |
| Standalone images (`.png`, `.jpg`, `.jpeg`, `.webp`) | Not supported as input | Images inside PDFs can be triaged and extracted. |
| HTML/web pages/URLs | Not supported | Download or print the content to PDF first. |

The current command-line entrypoint validates that the input path exists and
has a `.pdf` extension. Other formats may be added later through separate
adapters without changing the PDF pipeline.

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
python -c "import document_extract; print(document_extract.__version__)"
```

## Basic usage

The canonical entrypoint is:

```powershell
document_extract report.pdf --output-dir outputs
```

Equivalent forms are also supported:

```powershell
python -m document_extract report.pdf --output-dir outputs
```

To run without Ollama/VLM calls:

```powershell
document_extract report.pdf --skip-vlm
```

To process only selected pages:

```powershell
document_extract report.pdf --start-page 1 --end-page 5
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
| `--photo-skip-confidence N` | Triage confidence at or above which photo-type images skip extraction entirely (default 0.8). |
| `--photo-summaries` | Also transcribe photo-type images (skipped by default after confident triage). |
| `--skip-picture-triage` | Use heuristic routing without the triage VLM call. |
| `--temperature N` | Ollama temperature. |
| `--num-ctx N` | Ollama context size. |
| `--num-predict N` | Main VLM output token cap. |
| `--auto-num-ctx` | Estimate context size per call. |
| `--prompt-file FILE` | Custom page-refinement prompt. |
| `--no-divider-reorder` | Disable deterministic reading-order reconstruction. |
| `--resume-from STAGE` | Resume from a saved checkpoint stage. |
| `--shard` | Suffix run-level output files with the run's page range. |

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
document_extract report.pdf `
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
document_extract report.pdf `
  --config config/models.yaml `
  --config config/detection.yaml `
  --config my-run.yaml
```

Supported environment variables include:

The `DOCLING_RAG_*` names are retained for compatibility with existing
deployment scripts, even though the project is now named `document_extract`.

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

Prompts are bundled under `src/document_extract/resources/prompts/`. The editable
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

## Process-parallel shards

Run one process per page range against the same `--output-dir`, adding `--shard`
to each process. This writes range-suffixed run files such as
`manifest_p0001-p0048.json` while sharing page directories. The final `all/`
aggregation is rebuilt from page directories; concurrent workers can race on it,
but the last finisher writes the same merged view. Run a final `--shard`-less
aggregation if a single root-level run summary is needed.

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
photos. Small and decorative images are filtered before making the call. By
default, images the triage classifies as photos with high confidence
(`--photo-skip-confidence`, default 0.8) skip extraction entirely; pass
`--photo-summaries` to transcribe them anyway.

## Python integration

Use `run_extraction` for library callers:

```python
from document_extract import run_extraction

exit_code = run_extraction("report.pdf", output_dir="outputs", skip_vlm=True)
```

Use one run per process. Configuration updates module-level algorithm settings,
so concurrent threads in one process are unsupported; sequential calls in one
process are safe because each run reapplies its configuration.

The current stable integration surface is the argument-based runner:

```python
from document_extract.pipeline.runner import run_document

exit_code = run_document([
    "report.pdf",
    "--output-dir",
    "outputs",
    "--skip-vlm",
])
```

Configuration can also be loaded directly:

```python
from document_extract.config import load_config

config = load_config()
print(config.models.model)
```

## Docker

Build the image:

```powershell
docker build -f Dockerfile.docling -t document_extract .
```

Run the CLI:

```powershell
docker run --rm --gpus all `
  -v "${PWD}:/workspace" `
  document_extract `
  /workspace/report.pdf `
  --output-dir /workspace/outputs
```

## Offline replay tools

These tools operate on saved artifacts and do not call Ollama:

Offline replay scripts are not currently included in the standalone package.
Use the saved `page_state.json`, layout maps, and table candidate artifacts for
manual inspection or add the replay tools as a separate development module.

## Development checks

```powershell
python -m compileall -q src
python tests/test_package.py
python tests/test_docling_rag_slides.py
python tests/test_slide_postprocess.py
```

The maintained integration and post-processing checks live under `tests/`.
