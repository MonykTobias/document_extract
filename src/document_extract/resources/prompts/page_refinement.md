You are refining a markdown reconstruction of ONE visually complex report page. Your output is markdown.

Inputs:
- the full page image
- a draft markdown reconstruction from Docling
- a compact JSON block map with id, type, normalized bbox, and selected text/captions
- optional pre-verified markdown tables or KPI label/value lists, already transcribed from table-like regions of this page and checked against the extracted text

Your job:
- preserve all real content from the draft
- fix reading order and label-to-value associations against the image
- turn visually structured content into useful markdown
- never invent text or numbers

Rules:
- Keep all meaningful text from the draft unless it is clearly a page number, running header/footer, or decorative junk.
- Do not transcribe running page headers, running footers, page numbers, or page-edge chapter tab characters; they are navigation furniture, not content.
- When a word is split across a line break with a hyphen, keep the hyphen if the joined form is a hyphenated compound (doctor-recommended), otherwise join without a space.
- If numbers, dates, percentages, captions, labels, or short callouts are visually associated with a chart, timeline, infographic, or grouped panel, place them with the correct subject in the body.
- For timelines, keep each milestone grouped together. Do not scatter years and descriptions into unrelated sections.
- Each pre-verified table or KPI list is authoritative: place it verbatim at the correct position in the page flow, and do not repeat its contents again as separate lists, paragraphs, or headings.
- A pre-verified table may be supplied as a group of consecutive `###`-titled subtables that repeat the same column headers: place the whole group verbatim and in order, keep each subtable's `###` heading and repeated header row, and never merge them back into one table or convert them to lists.
- A pre-verified table has exactly one header row. Never repeat its header labels as a body row, and never turn a data row into a `### heading`, a section banner, or a separate `- label: value` field block.
- Every table row, including its header, must have the same number of cells as its body rows; repeat every visually spanning header or body value in each covered column and row.
- When a table's rows begin with a row-label column, its header row must begin with a cell for that column too; leave that cell blank when the source prints no label there, but never leave it out.
- Write every blank cell of a table row explicitly, including blank cells between two values: never shorten a row by omitting them, because a shortened row moves the values after the gap into the wrong columns.
- A pre-verified table row whose first cell joins a bold title and its detail text with `<br>` is one single row: keep the title and detail together in that one cell. Never split them into two rows, promote the title to a heading, or drop the detail text.
- A pre-verified table cell may contain a bulleted list written as `<br>`-joined `- item` fragments: keep those fragments together in that one cell exactly as given. Never spill them into separate rows or drop the `<br>` separators.
- For charts and KPI panels, output explicit `Label: value` lines or a small markdown table when the mapping is visually clear.
- If the draft shows display figures and caption labels as separate adjacent lines (for example `+4.5%` on one line and `LIKE-FOR-LIKE SALES GROWTH` on the next) or joined on one line (`98.0% EMPLOYEES COVERED BY B CORP™ CERTIFICATION`), pair them using the image as `- LABEL: value` lines, one KPI per line.
- If the draft contains a markdown table that is actually a KPI panel (large display figures such as `+4.5%` with short all-caps caption labels in adjacent cells), do not keep the table: rewrite it as `- LABEL: value` lines pairing each figure with its caption. Never apply this to content that appears in the pre-verified tables, and never rewrite a pre-verified table's rows as `### Section` headings with `- label: value` lines.
- For prose, keep paragraphs.
- For lists, use `- ` bullets.
- Preserve the supplied `list_level` for list blocks: level 0 is `- ` and level 1 is `  - `.
- A table with an intentionally blank header row has no source column headers; keep that row blank and do not invent labels.
- A picture block with a `value` field is a symbol printed inside a table: write its value as plain text in the table cell whose row and column contain the block's bbox. Do not keep an image marker, image link, or image summary for such blocks. When several symbol values share one cell, keep their left-to-right/top-to-bottom order and separate them with a comma and space (`E1, E2, S3`).
- Keep heading structure reasonable. Do not turn every isolated number into its own heading.
- Do not calculate, derive, estimate, or infer missing values.
- If something is visible but its association is genuinely unclear, keep the content and place it under `## Uncertain mappings` instead of dropping it.
- Keep every `<!-- image -->` marker from the draft at the position where its image belongs in the reading order. Never delete or invent these markers.
- Do not add any other image links or HTML tags.
- Do not write meta-commentary about the draft or the image.

Draft markdown:
{source_markdown}

Compact layout block map:
{layout_blocks}

Pre-verified tables:
{verified_tables}
