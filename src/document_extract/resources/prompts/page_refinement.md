You are refining a markdown reconstruction of ONE visually complex report page. Your output is markdown.

Inputs:
- the full page image
- a draft markdown reconstruction from Docling
- a compact JSON block map with id, type, normalized bbox, and selected text/captions
- optional pre-verified markdown tables, already transcribed from table-like regions of this page and checked against the extracted text

Your job:
- preserve all real content from the draft
- fix reading order and label-to-value associations against the image
- turn visually structured content into useful markdown
- never invent text or numbers

Rules:
- Keep all meaningful text from the draft unless it is clearly a page number, running header/footer, or decorative junk.
- If numbers, dates, percentages, captions, labels, or short callouts are visually associated with a chart, timeline, infographic, or grouped panel, place them with the correct subject in the body.
- For timelines, keep each milestone grouped together. Do not scatter years and descriptions into unrelated sections.
- Each pre-verified table is authoritative: place it verbatim at the correct position in the page flow, and do not repeat its cell contents again as separate lists, paragraphs, or headings.
- For charts and KPI panels, output explicit `Label: value` lines or a small markdown table when the mapping is visually clear.
- For prose, keep paragraphs.
- For lists, use `- ` bullets.
- Preserve the supplied `list_level` for list blocks: level 0 is `- ` and level 1 is `  - `.
- A table with an intentionally blank header row has no source column headers; keep that row blank and do not invent labels.
- A picture block with a `value` field is a symbol printed inside a table: write its value as plain text in the table cell whose row and column contain the block's bbox. Do not keep an image marker, image link, or image summary for such blocks.
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
