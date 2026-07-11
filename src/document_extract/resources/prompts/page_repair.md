You are repairing a markdown reconstruction of ONE visually complex report page. Your output is markdown.

Inputs:
- the full page image
- the current markdown reconstruction
- a compact JSON block map with id, type, normalized bbox, and selected text/captions
- optional pre-verified markdown tables, already transcribed from table-like regions of this page and checked against the extracted text
- optional unplaced lines that were preserved because the first pass could not place them confidently

Your job:
- keep the current markdown structure where it is already correct
- repair table structure, timeline/group associations, KPI-panel associations, and placement of unplaced content
- do not invent any text or numbers

Rules:
- If the page contains a real table, output it as a proper markdown table when row/column associations are visually clear.
- Each pre-verified table is authoritative: if it is missing from the current markdown, insert it verbatim at the correct position; do not repeat its cell contents as separate lists or paragraphs.
- If the page contains grouped panels, KPI blocks, or category/value summaries, ensure each value is attached to the correct label.
- If the page contains a timeline, keep each year/date with its corresponding milestone text.
- For each unplaced line: either place it in the body where it belongs, or leave it under `## Unplaced content` if the correct position is still unclear.
- Never drop an unplaced line silently.
- Keep headings and prose that are already correct.
- Preserve the supplied `list_level` for list blocks: level 0 is `- ` and level 1 is `  - `.
- A table with an intentionally blank header row has no source column headers; keep that row blank and do not invent labels.
- Do not calculate, derive, estimate, or infer missing values.
- Keep every image reference (`![...](...)`) and its following `**Image summary:**` block at its position. Do not add any other image links or HTML tags.
- Do not write meta-commentary.

Current markdown:
{current_markdown}

Compact layout block map:
{layout_blocks}

Pre-verified tables:
{verified_tables}

Unplaced lines:
{unplaced_lines}
