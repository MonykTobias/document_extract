Classify the provided report image for downstream extraction.

Return JSON only, with exactly these fields:
{"type":"photo|table|chart|kpi|infographic|map|diagram|decorative|unclear","confidence":0.0}

Use the visible image, not the metadata. Do not transcribe text. Use a confidence
between 0 and 1. Mark decorative only when the image has no meaningful report
content.
