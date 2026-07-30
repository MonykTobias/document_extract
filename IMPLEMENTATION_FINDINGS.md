# Implementation Findings

Findings discovered while implementing `LEAN_CORRECTIVE_PLAN.md`. Everything
newly observed is recorded here, including issues fixed immediately.

Severity: `blocker` (stops an active task), `major` (wrong behavior inside
scope), `minor` (bounded correctness or clarity issue), `note` (assumption or
limitation recorded on purpose).

---

## IMPL-001 — `document_knowledge` is outside version control

- **Evidence:** `git -C document_knowledge rev-parse HEAD` fails with
  *"ambiguous argument 'HEAD': unknown revision or path not in the working
  tree"*; the directory contains no `.git`.
- **Severity:** minor · **Component:** `document_knowledge` · **Under:** LP-01
- **Detail:** LP-01 requires the scaffold to be clearly non-runtime.
  `document_knowledge/README.md` and its `__init__.py` were rewritten to say so
  (the module now raises a `RuntimeWarning` on import). Those edits cannot be
  committed, so they are covered by no commit SHA in this report.
- **Recommended action:** `git init` the directory or delete it. PA-01 already
  enforces the property that matters — it is not installed and no runtime module
  references it — so the uncommitted text is documentation only.
- **Activation trigger:** anyone re-introducing `document_knowledge` as a
  dependency. **Blocks an active task:** no.

## IMPL-002 — plan verification commands assume pytest; `claim_evidence` tests are standalone scripts

- **Evidence:** `claim_evidence/CLAUDE.md` states *"Tests are standalone
  scripts, not pytest"*; the plan's verification blocks invoke
  `py -3.12 -m pytest <file>`.
- **Severity:** note · **Component:** `claim_evidence/tests` · **Under:** LP-01
- **Detail:** The two are compatible — pytest collects the module-level `test_*`
  functions and the `check(cond, msg)` helper raises `AssertionError`. Both
  invocations are run and reported in this cycle. Two suites needed adjusting to
  work under both: `test_db_init.py` gained pytest fixtures for its database
  state, and `test_integration.py`'s `test_database_url` helper was renamed to
  `database_url` because pytest was collecting it as a test.
- **Blocks an active task:** no.

## IMPL-003 — the acceptance runner's own environment defeats a naive `sys.path` assertion

- **Evidence:** the first PA-01 run failed with *"a source directory reached
  sys.path without being installed"* inside a freshly created virtual
  environment holding only editable installs.
- **Severity:** minor · **Component:** `gw_detector_v2/acceptance/run_checks.py`
  · **Under:** LP-01
- **Detail:** setuptools' editable install adds the source directory to
  `sys.path` through its own `.pth` file — the packaging tool doing its job, not
  a checkout hack. PA-01 now asserts what the plan actually requires: the
  packages import from a neutral working directory with no `PYTHONPATH`, and no
  repository file performs a `sys.path` insertion. Fixed under LP-01.
- **Blocks an active task:** no.

## IMPL-004 — the `insufficient` / `insufficient_evidence` naming conflict (plan note N-01)

- **Evidence:** `PROTOTYPE_DECISIONS.md` PD-08 and `LEAN_CORRECTIVE_PLAN.md`
  LP-08 name the verdict `insufficient`; `PROTOTYPE_ACCEPTANCE_CHECKLIST.md`
  PA-12 calls it `insufficient_evidence`.
- **Severity:** note · **Component:** the public API contract · **Under:** LP-02
- **Detail:** `PROTOTYPE_DECISIONS.md` is authoritative, and the code already
  used `insufficient`. LP-02 froze that as the single API literal, recorded it
  in `claim_evidence/contracts`, and added a contract test asserting that
  `insufficient_evidence` is *not* an API literal. PA-12's wording names the
  same state in prose.
- **Recommended action:** reword PA-12 in the checklist to match.
  **Blocks an active task:** no.

## IMPL-005 — contracts ship inside the packages, not at the repository roots

- **Evidence:** `LEAN_CORRECTIVE_PLAN.md` names `document_extract/contracts/`
  and `claim_evidence/contracts/`; the implementation puts them at
  `document_extract/src/document_extract/contracts/` and
  `claim_evidence/src/claim_evidence/contracts/`.
- **Severity:** note · **Component:** both contract packages · **Under:** LP-02
- **Detail:** LP-02 requires producer, backend, and frontend to validate against
  *the same checked-in examples*. A directory at a repository root is not
  reachable from an installed package, so consumers would have had to find a
  checkout by path — the exact dependency LP-01 removes. Inside the package they
  are ordinary package data and every consumer imports them.
- **Blocks an active task:** no.

## IMPL-006 — the legacy-schema migration behaviour was removed, and its test with it

- **Evidence:** `test_integration.py::check_schema_migrates_a_legacy_document`
  asserted that a pre-identity `document` row is backfilled in place.
- **Severity:** note · **Component:** `claim_evidence/db.py` · **Under:** LP-03
- **Detail:** PD-03 states that a nonempty mismatched database is not upgraded
  or repaired. The check is replaced by
  `check_a_legacy_schema_is_refused_not_migrated`, which asserts the successor
  behaviour: the refusal names the missing column and points at
  `db reset-dev`. This is a deliberate, approved behaviour change, not a
  weakened test.
- **Blocks an active task:** no.

## IMPL-007 — extraction settings did not reach the build fingerprint

- **Evidence:** a fingerprint test that changed `run.json`'s
  `settings.visual_values_mode` and expected a different fingerprint failed:
  the value was unchanged.
- **Severity:** major · **Component:** `claim_evidence/ingest.py` · **Under:** LP-04
- **Detail:** the fingerprint hashed the *artifacts* but not the extractor's
  declared evidence-affecting settings, so a re-extraction under different rules
  that happened to produce identical files would have been reused as if nothing
  changed. `run.json` cannot be hashed whole because it carries a generation
  timestamp, which would make every re-run differ. The `settings` block is now
  folded into the canonical fingerprint payload. Fixed under LP-04.
- **Blocks an active task:** no.

## IMPL-008 — every narrative citation pointed at a file that has never existed

- **Evidence:** `source.py::narrative_units` set
  `artifact_path = f"{page.rel}/blocks.jsonl"`, but `blocks.jsonl` is one
  document-wide artifact at the output root. `frontend.py::get_evidence`
  resolved `output_root / artifact_path` and silently dropped the missing file.
- **Severity:** major · **Component:** `claim_evidence/source.py` · **Under:** LP-05
- **Detail:** this is GR-020/GR-I02 confirmed in code. Every narrative citation
  resolved its artifact to nothing, and the failure was invisible because the
  resolver returns `None` for a missing file rather than raising. Fixed, and an
  activation-time containment check now refuses any build whose citable units
  name a missing or escaping artifact — so this class of bug cannot ship again.
- **Blocks an active task:** no.

## IMPL-009 — `audit_claim` is a breaking API change

- **Evidence:** `ClaimEvidence.audit_claim(claim, document_ids=None)` became
  `audit_claim(claim, *, scope, reporting_entity, limit=20, progress=None)`.
- **Severity:** note · **Component:** `claim_evidence/client.py`,
  `gw_detector_v2/ce_adapter.py` · **Under:** LP-07
- **Detail:** required by PD-07: scope must be explicit, and the reporting
  entity must be stated rather than derived from a filename. Both callers in
  this workspace were updated, along with the browser form. Any external caller
  of `audit_claim` breaks and must supply both arguments — which is the point,
  since silently defaulting either is the behaviour being removed.
- **Blocks an active task:** no.

## IMPL-010 — the supported unit vocabulary had to be closed

- **Evidence:** with "any word after the number" as the unit rule, *"Danone
  reduced Scope 1 and 2 emissions by 40.2%"* parsed as the value `1` in the
  unit `and`.
- **Severity:** major · **Component:** `claim_evidence/claims.py` · **Under:** LP-07
- **Detail:** a metric name containing digits is not a value. `claims.py` now
  recognises a value only when a supported unit is directly attached, using a
  closed vocabulary (`SUPPORTED_UNITS`). A claim in an unlisted unit is refused
  by name rather than compared as two strings that were never the same quantity.
  This narrows what version 1 accepts, which is consistent with PD-07 but is
  worth stating: a report using a unit outside the list cannot be audited until
  it is added.
- **Recommended action:** extend `SUPPORTED_UNITS` as real corpora require it;
  each addition needs a matching normalization on the fact side.
  **Blocks an active task:** no.

## IMPL-011 — the retained 494-page Danone extraction is no longer indexable (RESOLVED)

- **Evidence:** the retained `outputs_full_run/` tree predates the frozen
  `document_extract/run@1.0` contract, so `source.py` refuses it and asks for
  re-extraction — which needs docling, torch, and a GPU that were not installed.
- **Severity:** note · **Component:** the retained corpus · **Under:** LP-04
- **Detail:** by PD-01 an old extraction output is re-extracted rather than read
  through a compatibility shim, so this was the designed behaviour meeting an
  environment that could not perform the re-extraction.
- **Resolution:** the extraction runtime was installed into the acceptance
  environment from `document_extract/requirements-docling-gpu.txt`, and PA-05
  now parses two pages sliced out of the retained `danoneurdaccessible.pdf`
  through the real docling pipeline and indexes them through the running
  frontend. The full 494-page run stays a manual smoke test and is deliberately
  not part of every acceptance run.
- **Blocks an active task:** no longer.

## IMPL-012 — five plan tasks are not implemented in this cycle (RESOLVED)

- **Evidence:** at the time of writing, `IMPLEMENTATION_PROGRESS.md` recorded
  LP-10 through LP-13 as `Not started` and LP-14 as `Partially completed`.
- **Severity:** blocker · **Component:** the plan as a whole
- **Detail:** LP-10 through LP-14 have since been implemented, tested, and
  committed. The acceptance runner no longer reports any check as `missing`.
- **Resolution:** `verify_prototype.ps1` reports 17/17 PASS and
  `PROTOTYPE ACCEPTED`. **Blocks an active task:** no longer.

## IMPL-013 — the reporting entity had to become required at ingestion too

- **Evidence:** with the audit-side entity made explicit under LP-07, every
  supported claim began returning `insufficient`: the fact's subject was still
  `organization_name(document_name)`, so comparing "Danone S.A." against the
  fixture's directory name was a mismatch on every fact.
- **Severity:** major · **Component:** `claim_evidence/ingest.py` · **Under:** LP-08
- **Detail:** GR-047 was only half fixed by LP-07. `ingest_document` now
  requires `reporting_entity`, stores it as every fact's subject, keeps the
  filename as an alias only, and includes it in the build fingerprint, since it
  decides what gets stored. The frontend collects it on upload, register, and
  re-index. This is a breaking change to both public entry points.
- **Blocks an active task:** no.

## IMPL-014 — the numeric tolerance and bound arithmetic are gone

- **Evidence:** `facts.py::_apply_operator` returned a match for "roughly 40%"
  against a reported 40.2%, and evaluated `>=`/`<=` bounds on magnitude.
- **Severity:** note · **Component:** `claim_evidence/facts.py` · **Under:** LP-08
- **Detail:** PD-07 admits only exact claims, and `claims.validate_claim`
  refuses approximate and bounded ones before an audit opens. The comparator
  now answers `incomparable` for anything that is not an exact `=`, rather than
  applying a tolerance nobody asked for. The bound arithmetic is retained as
  `_legacy_bounded`, uncalled, because reinstating it needs a product decision
  about what a bound means against a range of reported figures. Two tests that
  asserted the old behaviour were replaced by their successors.
- **Blocks an active task:** no.

## IMPL-015 — the browser suite's own server blocked on an unread pipe

- **Evidence:** `pytest tests/e2e/test_browser.py` → "4 failed, 6 passed in
  213.17s", each failure a 60s page-load timeout; the same four tests passed in
  isolation in 5.40s.
- **Severity:** major · **Component:** `gw_detector_v2/tests/e2e/test_browser.py`
  · **Under:** LP-13
- **Detail:** the suite started `main.py` with `stdout=subprocess.PIPE` and never
  read it. Flask logs every request, so after a few dozen the 64 KB pipe buffer
  filled and the server blocked forever on the write. Every test after that
  point timed out and every one of them passed alone — the classic shape of a
  defect misread as test-order flakiness. Server output now goes to a file under
  the runtime directory, which the failure messages quote. 246s → 6s, 10/10.
- **Blocks an active task:** no (fixed).

## IMPL-016 — the fixture corpus digests depended on the platform that built it

- **Evidence:** `git add` warned *"LF will be replaced by CRLF the next time Git
  touches it"* on `northwind_report.pdf`.
- **Severity:** minor · **Component:** `gw_detector_v2/acceptance/fixtures.py`
  · **Under:** LP-13
- **Detail:** the generator wrote text artifacts in Windows text mode, and git
  sniffed the mostly-ASCII synthetic PDF as text. The catalogue records a
  SHA-256 per artifact, so a fresh clone would have checked out bytes that did
  not match its own catalogue. Every text write now pins `newline="\n"`, and a
  `.gitattributes` marks the whole fixture tree binary. Caught from a git
  warning rather than a failing test — nothing yet verifies the catalogue
  against the tree, which is why it stayed quiet.
- **Blocks an active task:** no (fixed).

## IMPL-017 — an unsupported claim was accepted as a job and failed afterwards

- **Evidence:** PA-13 → "unsupported-approximate returned HTTP 202, not a client
  error".
- **Severity:** major · **Component:** `gw_detector_v2/main.py`,
  `claim_evidence/client.py` · **Under:** LP-14
- **Detail:** `POST /api/audit` submitted a background job without checking the
  claim, so every refusal the version-1 grammar makes arrived as a *failed job*
  rather than a client error. The user waited for a rejection knowable at
  submission time, and a failed row was written describing work nobody intended
  to run. `ClaimEvidence.check_claim` now exposes the same validation
  `audit_claim` already performs — no retrieval, no model call, no audit row —
  and the route calls it before enqueueing. PD-07's contract is unchanged; only
  where it is enforced moved.
- **Blocks an active task:** no (fixed).

## IMPL-018 — an interrupted job came back as 404 after a restart

- **Evidence:** PA-16 → "the job is not retrievable after restart (404)".
- **Severity:** major · **Component:** `gw_detector_v2/main.py` · **Under:** LP-14
- **Detail:** the journal recorded the job, startup reconciled it to
  `interrupted`, and then nothing read the result: `/api/jobs/<id>` answers from
  the in-memory registry, which a fresh process has never populated. A browser
  watching a job across a crash was told the job never existed rather than that
  it was interrupted — precisely the honesty LP-12 exists for, and it made the
  journal a file written for nobody. `JobRegistry.adopt_interrupted` now takes
  those entries as terminal `interrupted` rows at startup. Nothing is resumed;
  the user asks again.
- **Blocks an active task:** no (fixed).

## IMPL-019 — the acceptance harness had the same unread-pipe defect

- **Evidence:** found by inspection while wiring PA-05..PA-16, before it could
  bite: `Frontend.__enter__` used `stdout=subprocess.PIPE` and read it only in
  `__exit__`.
- **Severity:** minor · **Component:** `gw_detector_v2/acceptance/harness.py`
  · **Under:** LP-14
- **Detail:** identical to IMPL-015 in a sibling caller. PA-05 and PA-15 drive
  far more than a few dozen requests through one frontend, so this would have
  hung the acceptance run rather than a test. Fixed the same way at the same
  time — a symptom fixed in one caller and left in the other is half a fix.
- **Blocks an active task:** no (fixed).

## IMPL-020 — a terminated frontend leaves its application marker behind

- **Evidence:** PA-04 → "ResetRefused: the local application is running (its
  marker file exists)", with nothing running.
- **Severity:** note · **Component:** `gw_detector_v2/acceptance/harness.py`
  · **Under:** LP-14
- **Detail:** `main.py` removes its marker in a `finally`, which
  `Popen.terminate()` on Windows never reaches. The product's own docstring
  already treats this as deliberate — a stale marker is cleared by starting and
  stopping the app again, which it argues beats a reset that proceeds anyway —
  so the product behaviour was left exactly as it is. The harness was at fault
  for leaking a marker between checks: each acceptance run now points
  `CLAIM_EVIDENCE_APP_MARKER` at its own runtime root and removes it after each
  frontend. PA-04 additionally writes a marker on purpose to prove the guard
  fires.
- **Blocks an active task:** no (fixed).

## IMPL-021 — two commits on the branch were rewritten by something outside this work

- **Evidence:** `git reflog` in `gw_detector_v2` shows `rebase (reword)` and
  `commit (amend)` entries that were not issued here; `git diff 70c3210
  91699c5` is empty and the only message difference is a removed
  `Co-Authored-By` trailer.
- **Severity:** note · **Component:** the `feat/lean-prototype` branch
- **Detail:** commit contents are byte-identical and all fourteen task commits
  remain on the branch; only two commit hashes changed. Recorded because the
  git-history table below lists hashes, and two of them differ from what the
  commit command originally printed.
- **Blocks an active task:** no.
