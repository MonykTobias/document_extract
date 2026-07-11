You are transcribing ONE KPI panel cropped from a report page. It shows large display figures, each with a short caption label. It is NOT a data table.

Inputs:
- the cropped panel image
- a JSON list of text blocks extracted from this panel, each with id, normalized bbox, and text — these texts are authoritative

Your job: pair every display figure with its caption label using the visible layout (a caption is the short text printed directly above, below, or beside its figure).

Rules:
- Output one line per KPI: `- LABEL: value` (example: `- LIKE-FOR-LIKE SALES GROWTH: +4.5%`).
- Keep visible group headings as plain lines above their KPIs.
- Use only text and numbers from the blocks. Do not calculate, infer, or complete missing values.
- Do not output markdown tables. No pipes.
- Include every block text exactly once; if a text has no partner, output it as `- text` alone.
- If the region is actually a data table with rows and columns, answer with the single word SKIP.
- No commentary.

Region text blocks:
{region_blocks}
