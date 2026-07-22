You are transcribing ONE table-like region cropped from a report page into markdown tables.

Inputs:
- the cropped region image
- a JSON list of text blocks extracted from this region, each with id, normalized bbox, and text — these texts are authoritative

Your job: arrange the given block texts into one or more markdown tables that match the visible row/column structure.

Rules:
- Use the block texts as cell contents. Do not paraphrase them.
- Do not invent, calculate, or infer any text or numbers that are not in the blocks.
- Repeat every visually spanning header or body value in each covered column and row. If row boundaries split a neighboring cell, repeat that value in every resulting row where it applies.
- Every table row, including the header, must have the same number of cells.
- Use the topmost row of column labels as the table header when one is visible. Emit that header row only once; never repeat the header labels as a body row.
- When a bold title row is immediately followed by its detail/description row, combine them into a single row: put the title and its detail together in the first cell and keep the other columns from the detail row. Never emit the title as its own separate row or as a heading.
- When a single cell contains several bulleted items, keep them in one cell as `- item` fragments joined with `<br>`; never spill them into separate rows.
- Leave a cell blank when it has no visible content.
- If the region is not actually a table or a grouped label/value panel, answer with the single word SKIP.
- Output only markdown tables (or SKIP). No commentary, no extra headings.

Region text blocks:
{region_blocks}
