You are repairing a markdown reconstruction of ONE visually complex report page. Your output is markdown.

Inputs:
- the full page image
- the current markdown reconstruction
- a compact JSON block map with id, type, normalized bbox, and selected text/captions
- optional pre-verified markdown tables or KPI label/value lists, already transcribed from table-like regions of this page and checked against the extracted text
- optional unplaced lines that were preserved because the first pass could not place them confidently

Your job:
- keep the current markdown structure where it is already correct
- repair table structure, timeline/group associations, KPI-panel associations, and placement of unplaced content
- do not invent any text or numbers

Rules:
- If the page contains a real table, output it as a proper markdown table when row/column associations are visually clear.
- Each pre-verified table or KPI list is authoritative: if it is missing from the current markdown, insert it verbatim at the correct position; do not repeat its contents as separate lists or paragraphs.
- A pre-verified table may be supplied as a group of consecutive `###`-titled subtables that repeat the same column headers: place the whole group verbatim and in order, and never merge them back into one table, convert them to lists, or turn a section title into anything other than its `###` heading.
- A pre-verified table has exactly one header row. Never repeat its header labels as a body row, and never turn a data row into a `### heading`, a section banner, or a separate `- label: value` field block.
- A pre-verified table row whose first cell joins a bold title and its detail text with `<br>` is one single row: keep the title and detail together in that one cell. Never split them into two rows, promote the title to a heading, or drop the detail text.
- A pre-verified table cell may contain a bulleted list written as `<br>`-joined `- item` fragments: keep those fragments together in that one cell exactly as given. Never spill them into separate rows or drop the `<br>` separators.
- If the page contains grouped panels, KPI blocks, or category/value summaries, ensure each value is attached to the correct label and output as `- LABEL: value` lines.
- If the page contains a timeline, keep each year/date with its corresponding milestone text.
- For each unplaced line: either place it in the body where it belongs, or leave it under `## Unplaced content` if the correct position is still unclear. Only place lines listed below; never output an `## Unplaced content` heading when that list is empty, and never copy the text `(none)`.
- Never drop an unplaced line silently.
- Keep headings and prose that are already correct.
- Preserve the supplied `list_level` for list blocks: level 0 is `- ` and level 1 is `  - `.
- A table with an intentionally blank header row has no source column headers; keep that row blank and do not invent labels.
- A picture block with a `value` field is a symbol printed inside a table: write its value as plain text in the table cell whose row and column contain the block's bbox. Do not keep an image marker, image link, or image summary for such blocks. When several symbol values share one cell, keep their left-to-right/top-to-bottom order and separate them with a comma and space (`E1, E2, S3`). Never render a symbol value as an image link such as `![S4](#)`.
- Do not calculate, derive, estimate, or infer missing values.
- Keep every image reference (`![...](...)`) and its following `**Image summary:**` block at its position. Do not add any other image links or HTML tags.
- Do not transcribe running page headers, running footers, page numbers, or page-edge chapter tab characters; they are navigation furniture, not content.
- Do not write meta-commentary.

Current markdown:
{current_markdown}

Compact layout block map:
{layout_blocks}

Pre-verified tables:
{verified_tables}

Unplaced lines:
{unplaced_lines}
