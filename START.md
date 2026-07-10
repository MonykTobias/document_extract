START OLLAMA:

$env:OLLAMA_HOST="0.0.0.0:11434"
ollama serve

BUILD DOCKER IMAGE:

docker build -f Dockerfile.docling -t unlimited-ocr-docling-gpu .

RUN THE SCRIPT:

docker run --rm --gpus all `
  -v "${PWD}:/workspace" `
  -v unlimited-ocr-docling-models:/models `
  unlimited-ocr-docling-gpu `
  /workspace/danoneiar2025.pdf `
  --output-dir /workspace/outputs_docling_rag `
  --ollama-base-url "http://host.docker.internal:11434" `
  --ollama-model "hf.co/unsloth/Qwen3-VL-8B-Instruct-GGUF:UD-Q8_K_XL" `
  --num-ctx 16384 `
  --auto-num-ctx `
  --num-predict 3000

# Optional: use a smaller Ollama model for the visual picture-triage call.
# If omitted, triage falls back to --ollama-model.
# Add these arguments before the final line when using a separate triage model:
#   --triage-model "qwen2.5vl:3b" `
#   --triage-num-predict 64 `
#   --triage-confidence 0.65

docker run --rm --gpus all `
  -v "${PWD}:/workspace" `
  -v unlimited-ocr-docling-models:/models `
  unlimited-ocr-docling-gpu `
  /workspace/danoneiar2025.pdf --start-page 9 --end-page 13 ...
# Optional evaluation tooling is not included in the standalone package.


PACKAGE USAGE:

The pipeline is now installable from this directory.  The package entrypoint
is the canonical CLI; the old script remains a compatibility wrapper:

    python -m pip install -e .
    document_extract danoneiar2025.pdf --output-dir outputs_docling_rag
    python -m document_extract danoneiar2025.pdf --skip-vlm

Configuration is loaded in this order: bundled defaults, each repeated
`--config` YAML overlay, environment variables, and explicit CLI arguments.
Use the split examples in `config/` for runtime, model, and detection values:

    document_extract danoneiar2025.pdf --config config/models.yaml --config my-run.yaml

The package keeps prompts in `src/document_extract/resources/prompts/`; the copies
in `prompts/` are convenient editable project-level references.  Existing
`page_state.json` checkpoints and `--resume-from` replay stages are unchanged.



READING ORDER (divider-aware, deterministic, runs before everything else):

Docling reads some banded pages as full-height columns and interleaves content
across horizontal section dividers (e.g. danoneurdaccessible p8/p14: the first
band's right column ends up below the second band's text). A deterministic
pass now re-orders the page items before serialization:

- rule segments come from the PDF's vector drawings (thin strokes/rects via
  fitz get_drawings; split/dashed strokes are merged)
- recursive region cut, horizontal before vertical; leaves keep Docling's
  own order, so the whole pass is a NO-OP (identity -> Docling serializer,
  byte-identical output) on pages whose order already matches the regions
- horizontal band cuts need strong evidence: a rule spanning >=60% of the
  region WITH its section heading directly above it (the heading+underline
  pattern), a wide (>=50% region) heading owning its row, or a >=5%-height
  full-width whitespace band. Heading-less rules (table row separators,
  decorative strips) never cut.
- vertical cuts: a drawn v-rule spanning >=35% of the region height, or a
  >=3.5%-width whitespace gutter; a cut is vetoed when any block straddles it
  (tables/pictures are atomic, so grids and charts cannot be shredded)
- margin furniture (running heads, nav bars, page numbers, edge logos) and
  blocks without geometry stay glued to their position in Docling's stream
- caption items: Docling streams a caption after its picture regardless of
  where it sits; on reordered pages a caption that is visually ABOVE its
  picture (chart titles typed as captions, e.g. p8 SALES BY GEOGRAPHICAL
  ZONE) is serialized before the image marker, below-captions stay after
- when a page IS reordered, the raw markdown is serialized from the items
  (not Docling's exporter); manifest.json gets a per-page "reading_order"
  entry; --no-divider-reorder disables the whole pass

Known limitations: bordered boxes only group via their surrounding gutters
(box edges are not band evidence); colored banner bars whose heading is the
only text are found via the heading, not the bar; rotated pages untested.

Offline validation against a finished run (no Docker):

# Optional reading-order replay tooling is not included in the standalone package.
# expected: p8/p14 reorder into band-major order; slide-deck anchor pages
# 6, 9, 12, 13, 26 of danoneiar2025 must stay identity

TABLE-REGION STAGE (per page_NNNN dir):

- table_candidates.json        detected regions with score stats + verification result
- table_candidates_overlay.png region boxes drawn on the page
- table_candidates/tcNNN_crop.png      region crop sent to the VLM
- table_candidates/tcNNN_table.md      verified table (goes into the refine prompt)
- table_candidates/tcNNN_rejected.md   transcription that failed verification (debug only)

A repair pass result is now rejected (warnings.repair_rejected in manifest.json) when it
truncates, loses content, or drops table rows vs. the pre-repair markdown.

EVAL (run after a full pass, diff eval_report.json against the previous run):

# Optional evaluation tooling is not included in the standalone package.

Replay pages 9 (must stay table-free), 12 + 13 (dashboard tables, no duplicate
2-column twins, all separators valid), 24 + 39 (severed goals/targets columns,
detected via region merge), 26 (picture-only emissions diagram must get an
image summary) before shipping prompt or detector changes:
--start-page 9 --end-page 13, then 24, 26, 39

NOTE: non-tiny pictures first pass the deterministic prefilter and then visual
triage. Pictures selected for extraction get a VLM image summary under the
image reference. Use --skip-picture-triage to restore the old heuristic-only
routing.

PICTURE TRIAGE:

- Pictures below 1% of the page and small decorative images are skipped before
  any VLM call.
- Remaining pictures receive a short JSON-only visual triage call.
- High-confidence table/KPI, chart, photo, infographic/map/diagram results use
  type-specific extraction prompts. Low-confidence or malformed triage falls
  back to the generic picture prompt.
- Triage metadata and usage are stored in page_state.json, image_summaries.jsonl,
  manifest.json, and token_usage.json.

STAGE CHECKPOINTS AND REPLAY:

Every page writes page_state.json after each stage. A replay starts at a named
stage and reruns that stage plus all dependent later stages:

docker run --rm --gpus all `
  -v "${PWD}:/workspace" `
  -v unlimited-ocr-docling-models:/models `
  unlimited-ocr-docling-gpu `
  /workspace/danoneiar2025.pdf `
  --output-dir /workspace/outputs_docling_rag `
  --resume-from page_refine `
  --start-page 12 --end-page 12 `
  --ollama-base-url "http://host.docker.internal:11434"

Supported replay stages are picture_triage, picture_extract, table_detect,
table_extract, page_refine, page_repair, and finalize. Failed pages are saved
in the manifest and later pages continue; the process exits nonzero if any page
failed.

Image summaries use a typed contract: the model answers 'TYPE: photo|table|chart|
kpi|infographic|map|diagram' + type-specific content; a deterministic shape check
triggers ONE focused retry (table prompt or label:value prompt) when the first
answer has the wrong shape. The VLM sees an EXPANDED crop from page.png
(picture bbox + adjacent title/total lines + margin, saved as
images/picture_*_vlm.png), not Docling's own picture image, so titles and
footer lines that exist only in pixels are captured. The refine prompt now
requires keeping '<!-- image -->' markers in position so summaries land where
the figure sits instead of at the page end.

Detector-only changes can be validated offline against a finished run (no Docker):

# Optional table-detector replay tooling is not included in the standalone package.
# expected candidate pages for danoneiar2025: 8, 12, 13, 24, 39, 53

UNIT TESTS (no Docker/Ollama needed):

py -3 test_docling_rag_slides.py
