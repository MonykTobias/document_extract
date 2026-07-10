You are transcribing ONE table-like region cropped from a report page into markdown tables.

Inputs:
- the cropped region image
- a JSON list of text blocks extracted from this region, each with id, normalized bbox, and text — these texts are authoritative

Your job: arrange the given block texts into one or more markdown tables that match the visible row/column structure.

Rules:
- Use the block texts as cell contents. Do not paraphrase them.
- Do not invent, calculate, or infer any text or numbers that are not in the blocks.
- If a label visually spans several rows, repeat it in each of those rows.
- Use the topmost row of column labels as the table header when one is visible.
- Leave a cell blank when it has no visible content.
- If the region is not actually a table or a grouped label/value panel, answer with the single word SKIP.
- Output only markdown tables (or SKIP). No commentary, no extra headings.

Region text blocks:
{region_blocks}
