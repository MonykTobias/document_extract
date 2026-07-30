# Final Implementation Report

## 1. Result

**The lean corrective plan is fully implemented.** All fourteen tasks (LP-01
through LP-14) are implemented, tested, and committed across the three
repositories.

The prototype is **accepted**: one run of `verify_prototype.ps1` reports 17/17
checks passing, with no failed, blocked, missing, or skipped row, and exits
zero.

## 2. Scope

Implemented against `LEAN_CORRECTIVE_PLAN.md`, with `PROTOTYPE_DECISIONS.md`
authoritative, across three repositories plus one uncommittable scaffold.

## 3. Repository start and end state

| Repository | Start branch | Start HEAD | End branch | End HEAD |
|---|---|---|---|---|
| `claim_evidence` | `fix/m1-m9-remediation` | `0c245ad` | `feat/lean-prototype` | `4fdc2a1` |
| `document_extract` | `audit-fixes` | `8e59206` | `feat/lean-prototype` | `4cfd3f6` |
| `gw_detector_v2` | `master` | `29d095b` | `feat/lean-prototype` | `75db8a7` |
| `document_knowledge` | not a repository | — | not a repository | — (see `IMPL-001`) |

All three implementation branches were created fresh from the recorded starting
HEADs. Nothing here amended, squashed, rebased, reset, or force-pushed a commit.
Tooling outside this work did rewrite two `gw_detector_v2` commits — contents
byte-identical, one trailer removed — which is recorded as `IMPL-021`.

### Git history

| Task | Repository | Commit | Subject |
|---|---|---|---|
| LP-01 | claim_evidence | `060e28f` | refactor(packaging): import the installed packages, not a checkout path |
| LP-01 | document_extract | `898fe2f` | refactor(packaging): import the installed package, not a checkout path |
| LP-01 | document_extract | `215495c` | docs: record implementation progress, findings, and acceptance results |
| LP-01 | gw_detector_v2 | `ad0a05b` | feat(acceptance): document install/start and add the PA runner harness |
| LP-02 | document_extract | `487b9df` | feat(contracts): freeze and publish the current run and progress contracts |
| LP-02 | claim_evidence | `29b1ec8` | feat(contracts): consume the run contract and freeze the public API vocabulary |
| LP-02 | gw_detector_v2 | `7a804f0` | feat(contracts): drive progress and vocabulary from the contracts, not copies |
| LP-03 | claim_evidence | `e31679c` | feat(db): one current schema, and a reset that has to be asked for exactly |
| LP-03 | gw_detector_v2 | `fd28ae1` | feat(app): hold the application-running marker while the server runs |
| LP-04 | claim_evidence | `6fcf794` | fix(identity): key documents on their bytes and fingerprint the whole build |
| LP-05 | claim_evidence | `d98fc30` | fix(evidence): resolve narrative provenance and gate activation on containment |
| LP-06 | claim_evidence | `e8923d2` | feat(lifecycle): degraded builds, targeted fact retry, honest interruption |
| LP-06 | gw_detector_v2 | `2216457` | feat(jobs): treat degraded as usable and reconcile interrupted work at startup |
| LP-07 | claim_evidence | `e22e00f` | feat(claims): refuse everything outside the version-1 grammar before any work |
| LP-07 | gw_detector_v2 | `b8fe813` | feat(audit): require an explicit scope and reporting entity in the browser |
| LP-08 | claim_evidence | `ac4f35a` | feat(verdicts): exact comparison only, and one corpus-qualified answer |
| LP-08 | gw_detector_v2 | `66f41de` | feat(ingest): require the reporting entity when indexing a document |
| LP-08 | document_extract | `ccdb1cf` | docs: record LP-08 and LP-09 progress and findings |
| LP-08 | document_extract | `612a4ec` | docs: complete the LP-08 and LP-09 sections of the final report |
| LP-09 | claim_evidence | `601c665` | feat(vision): four visual outcomes, and support that has to be earned |
| LP-09 | gw_detector_v2 | `8de6520` | test(evidence): pin the crop, highlight, and fullscreen rendering path |
| LP-10 | claim_evidence | `127ceb9` | fix(safety): stop the CLI printing raw causes and delimit every prompt |
| LP-10 | gw_detector_v2 | `9852e29` | test(security): recursively scan every public surface for disclosure |
| LP-11 | gw_detector_v2 | `5375a39` | feat(security): loopback-only startup, CSRF, typed config, and real limits |
| LP-12 | gw_detector_v2 | `dd9e3ad` | feat(jobs): refuse a duplicate ingestion and journal state across restarts |
| LP-13 | gw_detector_v2 | `91699c5` | test(acceptance): a labelled corpus, and the workflow driven by a real browser |
| LP-13 | gw_detector_v2 | `b97a1d5` | fix(fixtures): generate the corpus with LF endings so its digests hold |
| LP-13 | claim_evidence | `4fdc2a1` | test(acceptance): audit the labelled corpus against a real database |
| LP-14 | claim_evidence | `57f0bfc` | feat(claims): answer whether a claim is auditable before any work is queued |
| LP-14 | gw_detector_v2 | `75db8a7` | feat(acceptance): one command that proves PA-01..PA-17 or fails saying why |
| LP-14 | document_extract | `4cfd3f6` | docs(readme): state the prototype's boundaries and its deferred groups |

## 4. Behaviour implemented

**LP-01 — packaging and startup.** Every `sys.path` insertion removed from the
test bootstrap and the offline reprocess tool; one documented Python 3.12 install
path for all three packages; `gw_detector_v2/README.md` created with the exact
install/start commands and the supported-boundary statement; `document_knowledge`
marked non-runtime; `scripts/verify_prototype.ps1` and the acceptance harness
added.

**LP-02 — frozen contracts.** `document_extract/contracts` publishes one run
contract (source hash and page count, selected pages, artifact roles as
contained relative paths, coordinate convention, fired warnings, evidence-
affecting settings) and one versioned JSONL progress contract emitted alongside
the human status lines. Unknown keys and unknown versions are rejected by name;
an unknown run version says to re-extract. `claim_evidence/contracts` publishes
the one public API vocabulary; public DTOs forbid unknown keys; `AuditRequest`
makes scope explicit. The frontend consumes the JSONL contract instead of
regex-matching log text and renders from the served vocabulary.

**LP-03 — one schema, one guarded reset.** The numbered SQL files are
consolidated into a single `schema.sql` with no migration history.
Initialization has three outcomes: install, confirm unchanged, or refuse
read-only with reset guidance — never repair. `claim-evidence db reset-dev`
requires `CE_ENVIRONMENT=development`, a `_dev`/`_test` name, exact database and
`RESET-INDEX-AND-AUDITS` confirmations, and an absent application marker, which
the frontend now holds while it runs; it defaults to a dry run and issues only
SQL.

**LP-04 — identity and fingerprint.** Identity follows PD-04 (PDF bytes, then
normalized URI, then strictly-resolved local path) as a digest of a versioned,
kind-tagged basis. The fingerprint is canonical UTF-8 JSON over everything that
decides what gets stored, and excludes timeouts, batch sizes, URLs, and
audit-only model settings. Model identity records a digest where Ollama exposes
one and says `tag_only` where it does not.

**LP-05 — provenance and order.** Narrative citations now resolve (they pointed
at a per-page `blocks.jsonl` that has never existed). Activation refuses a build
whose citable units name a missing or escaping artifact, or lack source order.
Rank fusion breaks ties on page and source order rather than evidence id.

**LP-06 — honest lifecycle.** Partial narrative-fact failure produces a
`degraded` version: queryable, with coverage counted and failed candidates
stored as a key and a reason code. `retry_facts` re-runs only those candidates
and promotes to `ready` when the last one clears. `reconcile_interrupted` marks
work a dead process left running as `interrupted`, never `failed`.

**LP-07 — the claim gate.** `claims.py` refuses sixteen unsupported claim
classes with stable reason codes before any audit row or model call, mapped to
HTTP 422. `audit_claim` requires an explicit scope and an explicit reporting
entity. The dead `require_ready` helper is removed after a caller/export search.

**LP-08 — exact verdicts.** The numeric comparator has one rule: equal or not
equal, on `Decimal`. The tolerance and the bound arithmetic are gone; anything
that is not an exact `=` is `incomparable`. The reporting entity now blocks a
comparison when it disagrees, which required making it explicit at ingestion
too — every stored fact is attributed to it, and it joins the build
fingerprint. Repeated identical evidence counts once, so the same figure
printed twice cannot look like corroboration, and verdict wording is
corpus-qualified throughout ("supported by the indexed sources").

**LP-09 — real visual evidence.** A crop verifies as `support`, `conflict`,
`illegible`, or `unrelated`. Support is held to the claimed figure actually
appearing in the text the model reports it can see; a conflict is exempt,
because a different number on the page is exactly the point. Each verification
persists its result, reason code, and visible text against the audit that ran
it; the model's prose is neither stored nor published. Crop and vision failures
return stable codes and never echo a server path, and the claim reaches the
vision model delimited as data. The browser gets a crop only through
`/api/evidence/<id>/image`, with crop/page, highlight, and fullscreen controls.

**LP-10 — safe errors, DTOs, and the prompt boundary.** Every public DTO
forbids unknown keys; the CLI classifies each failure to a typed code and
prints the raw cause only under `CLAIM_EVIDENCE_DEBUG`. Prompts state that text
inside the delimited passage is data, evidence is wrapped in tagged blocks with
closing tags neutralized, and the adjudicator is told where an evidence id may
come from. Progress details are filtered to numbers, booleans, and code-like
tokens, so a driver message cannot ride out on an event.

**LP-11 — loopback, CSRF, configuration, and limits.** The server refuses to
bind anywhere but loopback, carries a per-process CSRF token checked alongside
Origin and Referer, and validates its configuration into a typed object whose
errors name the variable and never echo its value. Upload size, page count,
runtime-directory budget, and concurrent jobs are all bounded before a byte is
written, and a rejected upload's directory — and only that directory — is
removed.

**LP-12 — the browser workflow, duplicate jobs, and the restart journal.** Two
requests to index the same logical document cannot both be admitted; the check
runs under the lock that admits the job. Job state is journalled with an
allowlist of safe fields, written append-only with `fsync` and rewritten
atomically, and never raises — journalling is bookkeeping, and a failed note
must not fail the work it describes. On startup, anything left non-terminal is
reconciled to `interrupted` and adopted into the registry so the browser that
was watching gets an answer rather than a 404.

**LP-13 — the evaluation corpus and the browser end-to-end suite.** A two-page
synthetic corpus carries narrative, table row, table value, visual, and page
markdown evidence plus a hostile passage, with eleven labelled cases pinning
verdict, page, artifact role, and kind, and a catalogue recording a SHA-256 per
artifact. The browser suite runs the real server on a loopback port under a
headless Chromium and fails — never skips — when no browser is present.

**LP-14 — one zero-skip runner.** All seventeen acceptance checks execute for
real against a disposable `_test` database and a temporary runtime root, with
the redacted target printed before anything destructive. Each run writes a
timestamped JSON and Markdown report recording repository revisions, schema
version and SQL checksum, contract versions, model tags with digests, and the
fixture version. The prototype's boundaries and deferred groups are now stated
in all three READMEs.

## 5. Findings

Twenty-one findings are recorded in `IMPLEMENTATION_FINDINGS.md`. Six are
material product bugs found and fixed in passing:

- `IMPL-013`: the fact subject was still filename-derived, so with the audit
  side made explicit every supported claim returned `insufficient`.
- `IMPL-008`: every narrative citation resolved its artifact to nothing.
- `IMPL-007`: extraction settings did not reach the build fingerprint, so a
  re-extraction under different rules could be reused as if unchanged.
- `IMPL-017`: `POST /api/audit` accepted every unsupported claim as a job and
  failed it a moment later, instead of refusing it at submission.
- `IMPL-018`: a job interrupted by a restart came back as HTTP 404 — the
  journal was written, reconciled, and then read by nobody.
- `IMPL-015` / `IMPL-019`: the test and acceptance harnesses each started the
  server with `stdout` on a pipe nobody drained, which deadlocked it after a
  few dozen requests and looked exactly like test-order flakiness.

`IMPL-011` and `IMPL-012` are both now resolved: the extraction runtime was
installed and PA-05 parses real PDF pages through docling, and every plan task
is implemented. `IMPL-021` records two commits rewritten by tooling outside
this work.

Findings closed from `COMPLETE_GAP_REGISTER.md`: GR-001, GR-002, GR-003,
GR-004, GR-008, GR-009, GR-010, GR-012, GR-013, GR-018, GR-020, GR-021,
GR-022, GR-023, GR-024, GR-025, GR-026, GR-028, GR-034, GR-035, GR-040,
GR-041, GR-047, GR-049, GR-051, GR-056, GR-059, GR-060, GR-061, GR-063,
GR-065, GR-066, GR-070, GR-071, GR-072, GR-C01, GR-C02, GR-C05, GR-C06,
GR-C10, GR-C11, GR-I02, GR-I03, GR-I04, GR-I05, GR-I09, GR-I11, GR-I12,
GR-I13, GR-I15, GR-I16, GR-I17, GR-P01, GR-P03, GR-P04, GR-P05, GR-P07,
GR-P11, GR-P12, GR-P15, GR-T04, GR-T06, GR-T07, GR-T09, PV-004, PV-007,
PV-008, PV-014.

Closed under LP-10 through LP-14: GR-014, GR-015, GR-027, GR-029, GR-030,
GR-031, GR-032, GR-033, GR-037, GR-038, GR-042, GR-045, GR-046, GR-048,
GR-057, GR-058, GR-064, GR-C04, GR-C09, GR-I06, GR-I14, GR-P06, GR-P08,
GR-P09, GR-P10, GR-P14, PV-015.

All remaining identifiers stay recorded in `COMPLETE_GAP_REGISTER.md` under
deferred groups DG-01 through DG-05.

## 6. Decisions followed

PD-01 through PD-12 as written. Two recorded interpretations: the verdict
literal is `insufficient` (`IMPL-004`), and the contract packages ship inside
the Python packages so installed consumers can reach the same checked-in
examples (`IMPL-005`). No deferred group (DG-01..DG-05) was implemented; in
particular no migration path, no cross-process locking, no remote or
multi-user support, and no performance certification.

## 7. Test commands and results

| Command | Result |
|---|---|
| `cd claim_evidence && python tests/run_all.py` | all suites pass |
| `cd document_extract && python tests/run_all.py` | 28 suites, all pass |
| `py -3.12 -m pytest gw_detector_v2/tests -q` | 246 passed, 2 skipped |
| `py -3.12 -m pytest gw_detector_v2/tests/e2e/test_browser.py -q` | 10 passed (real headless browser) |
| `py -3.12 -m pytest claim_evidence/tests/acceptance/test_corpus.py -q` | 9 passed (real PostgreSQL) |
| the four contract suites | 38 passed |
| `pytest claim_evidence/tests/test_db_init.py claim_evidence/tests/test_reset_dev.py -q` | 20 passed |
| `pytest claim_evidence/tests/test_identity.py claim_evidence/tests/test_fingerprint.py -q` | 22 passed |
| `pytest claim_evidence/tests/test_facts.py claim_evidence/tests/test_audit_semantics.py -q` | 35 passed |
| `pytest claim_evidence/tests/test_vision.py -q` | 15 passed |
| `powershell -File gw_detector_v2\scripts\verify_prototype.ps1` | **17/17 pass — PROTOTYPE ACCEPTED** |

The two skips in `gw_detector_v2/tests` are pre-existing and unrelated to this
cycle. No test was deleted, weakened, or skipped to obtain a green result. Four
browser tests that failed only in module order were fixed by removing the cause
(`IMPL-015`), not by relaxing what they assert; the two behaviour changes to
existing tests (`IMPL-006`, and the claim fixtures LP-07 made unsupported) are
recorded with their reasons.

## 8. Prototype acceptance

See `PROTOTYPE_ACCEPTANCE_RESULTS.md`. One run of
`gw_detector_v2\scripts\verify_prototype.ps1` produces seventeen present,
passing rows and exits zero. Nothing the checklist requires to be real is
faked: PA-05 parses a retained PDF through docling and indexes it through the
running frontend, PA-09 fetches a real crop over HTTP and renders it in a
headless browser, PA-16 kills the frontend mid-job and restarts it, and PA-04
exercises the reset guards against a disposable database. A blocked dependency
is reported as a failed run, never as a skip.

## 9. End-to-end workflow

Run, in one command. `verify_prototype.ps1` performs the whole sequence:
disposable database dropped and recreated → schema initialized and re-verified
→ guarded reset attempts refused → a retained PDF sliced, uploaded, parsed by
docling and indexed → the labelled corpus indexed and audited to `supported`,
`contradicted`, and `insufficient` verdicts with citations → citations resolved
through the public API → a real crop fetched and rendered in a headless browser
→ the frontend killed mid-job and restarted → the report checked against
itself.

## 10. Remaining blockers

None for the plan. Two standing limitations remain, both recorded rather than
fixed:

1. `IMPL-001`: `document_knowledge` is not a git repository, so the edits
   marking it non-runtime are uncommittable. The runtime packages do not
   reference it and PA-01 proves it is not importable.
2. The full 494-page Danone extraction stays a manual smoke test. PA-05 parses
   two of its pages for real; running all 494 in every acceptance run is
   exactly the kind of large-load fixture DG-05 defers.

Running the acceptance suite needs PostgreSQL on 5433, Ollama with the three
models, a Chromium-family browser, and the extraction runtime from
`document_extract/requirements-docling-gpu.txt`. Each of those was available
for the recorded run; any one of them missing is reported as a failed check
with a message naming what to install.

## 11. Rollback

Each repository's work is isolated on `feat/lean-prototype`, created from the
recorded starting HEAD. To roll back completely:

```bash
git -C claim_evidence checkout fix/m1-m9-remediation && git -C claim_evidence branch -D feat/lean-prototype
```

```bash
git -C document_extract checkout audit-fixes && git -C document_extract branch -D feat/lean-prototype
```

```bash
git -C gw_detector_v2 checkout master && git -C gw_detector_v2 branch -D feat/lean-prototype
```

To roll back one task, revert its commits; each is self-contained and named with
its `Task:` trailer. Two changes need a note: reverting LP-03 leaves databases
created under schema 6 unreadable by the older code (reset and re-init), and
reverting LP-07 restores the defaulted `audit_claim` signature.

The uncommitted `document_knowledge` edits must be reverted by hand.

Reverting LP-14 removes the acceptance runner but leaves the product
behaviour it exposed — `check_claim` (LP-14, `claim_evidence`) and
`adopt_interrupted` (LP-14, `gw_detector_v2`) are real fixes and are
reverted with their own commits.

## 12. Intentionally deferred

DG-01 through DG-05 in full: no migration ledger or in-place upgrade, no
cross-process locking or second worker, no authentication/TLS/remote bind, no
multi-document authority or multilingual or layout-graph work, and no archival,
telemetry, large fixtures, latency budgets, or hardware certification. The
prototype remains English-only, loopback-only, one-process, exact-claim, and
disposable-data.
