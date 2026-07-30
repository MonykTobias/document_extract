# Prototype Acceptance Results

Results for every check in `PROTOTYPE_ACCEPTANCE_CHECKLIST.md`.

One run of the documented command, on 2026-07-30:

```powershell
Set-Location "C:\Users\Tobia\Documents\Tobi&Anna\gw_detector_v2"
powershell -ExecutionPolicy Bypass -File ".\scripts\verify_prototype.ps1"
```

**Overall outcome: ACCEPTED — 17/17 passed.** Zero failed, zero blocked, zero
missing, zero skipped. Machine-readable evidence:
`gw_detector_v2\verification\prototype\prototype-20260730T183616Z.json`, with a
matching `.md`. Reports are gitignored development artifacts, as the checklist
requires; the latest is kept locally for handoff.

## Environment behind that result

Recorded in the `metadata` block of every report:

| Key | Value |
|---|---|
| Python | 3.12.10, in a dedicated `.acceptance-venv` |
| Database | `claim_evidence_test` on `localhost:5433`, dropped and recreated by the run |
| Schema version | 6 |
| Schema SQL SHA-256 | `e3c0254e299ddf068c640f5c29a49da566bc289ecaffd641ed9bcbb01e16de95` |
| API contract | `claim_evidence/api` |
| Extraction contracts | `document_extract/run`, `document_extract/progress` |
| Embedding model | `qwen3-embedding:4b` @ `df5bd2e3c74cd8d0` |
| Chat / vision model | `hf.co/unsloth/Qwen3-VL-4B-Instruct-GGUF:UD-Q8_K_XL` @ `ee0d341d11c29a4c` |
| Fixture version | 1 — 14 artifacts, 11 labelled cases |
| Browser | headless Edge (Chromium), Selenium 4.46 |
| Extraction runtime | docling 2.70.0, torch 2.8.0+cu128 |

Repository revisions are recorded per run. At this run they were
`claim_evidence@127ceb9`, `document_extract@612a4ec`, `gw_detector_v2@b97a1d5`;
the commits after those are the runner itself and documentation, listed in
`FINAL_IMPLEMENTATION_REPORT.md`.

## The seventeen checks

| ID | Result | What actually ran | Tasks |
|---|---|---|---|
| PA-01 | Passed | Both packages imported from a neutral directory with no `PYTHONPATH`; no repository file inserts a checkout onto `sys.path`; `document_knowledge` is not importable; the CLI runs. | LP-01 |
| PA-02 | Passed | The real `main.py` started on a loopback port and served `/` and `/api/health` with 200 twice — once against PostgreSQL, once against the deterministic fake adapter. | LP-01, LP-11 |
| PA-03 | Passed | An empty database initialized to schema 6; a second initialization returned `unchanged` with an identical marker and no missing objects. | LP-03 |
| PA-04 | Passed | Five reset attempts refused (wrong phrase, wrong database name, a production-looking name, wrong environment, application marker present); the fully confirmed call still defaulted to a dry run; all 14 corpus artifacts byte-identical afterwards; the reported target carries no credential. | LP-03 |
| PA-05 | Passed | Two pages sliced from the retained `danoneurdaccessible.pdf`, uploaded over HTTP, parsed by real docling, and indexed to a queryable version carrying evidence — through the running frontend, not a library call. | LP-02, LP-06 |
| PA-06 | Passed | The same extraction indexed twice: one document id, one ready version, `reused_existing` true, identical fingerprint. | LP-04 |
| PA-07 | Passed | Every citable narrative unit served with unaltered text on its stored page, its artifact resolving to a real file inside the output root, in source order. | LP-05 |
| PA-08 | Passed | The labelled table case returned `supported` citing page 1 with regions, naming `table_candidates`, and recorded the unit the page stated. | LP-05, LP-08 |
| PA-09 | Passed | A crop and a full page fetched over HTTP as distinct PNGs, plus the gallery and fullscreen paths driven in a real headless browser. | LP-09, LP-13 |
| PA-10 | Passed | `supported`, decided by `deterministic_comparison`, with a matching numeric comparison and citations carrying evidence ids. | LP-08 |
| PA-11 | Passed | `contradicted`, with a comparison whose outcome is `conflict`. | LP-08 |
| PA-12 | Passed | An absent metric returned `insufficient` with zero citations — absence is not contradiction. | LP-08 |
| PA-13 | Passed | Four typed refusals (approximate, compound, qualitative, missing scope) each with its expected reason code, HTTP 422 from the running server, and zero audit rows written. | LP-07, LP-14 |
| PA-14 | Passed | Every citation resolved through the public evidence endpoint to the same page, and every one belonged to the version the audit pinned. | LP-05, LP-10 |
| PA-15 | Passed | Nine public surfaces plus CLI output scanned for paths, credentials, tracebacks, driver names and prompts — none present; JSON surfaces parse as JSON; the repository's own disclosure sweep and the browser hostile-text check both pass. | LP-10, LP-11 |
| PA-16 | Passed | The frontend killed with a job in flight, restarted, and the job reported `interrupted` — never `completed` — while a version ready before the restart still audited to `supported`. | LP-06, LP-12 |
| PA-17 | Passed | The report itself: exactly PA-01..PA-17, unique ids, every status `pass`, no skip-equivalent outcome. | LP-14 |

## How the runner fails

- A missing dependency is reported `blocked`, which counts as a failed run and
  never as a skip.
- A check with no registered implementation is reported `missing`, which is
  also a failed run — this is what kept the earlier partial state honest.
- `--only` runs exit on their own checks alone and print that a partial run
  does not accept the prototype; `PA-17` refuses to pass unless the run
  selected all seventeen ids.

No mandatory item is marked *not applicable*, and no check is satisfied by a
static source assertion.

## Not covered by this run

- The full 494-page Danone extraction. Two of its pages are parsed for real by
  PA-05; the whole document stays a manual smoke test, as the checklist
  intends.
- Anything in deferred groups DG-01 through DG-05 — see the boundaries section
  now present in all three READMEs.
