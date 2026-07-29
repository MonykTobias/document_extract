# Final Implementation Report

## 1. Result

**The lean corrective plan is not fully implemented.** Nine of fourteen tasks
(LP-01 through LP-09) are implemented, tested, and committed. LP-14 is
partially implemented — the acceptance runner and its report format exist and
enforce the zero-skip rule, with two of seventeen checks registered. LP-10
through LP-13 are not started.

The prototype is **not accepted**: a full `verify_prototype.ps1` run reports
fifteen checks as `missing` and exits non-zero.

## 2. Scope

Implemented against `LEAN_CORRECTIVE_PLAN.md`, with `PROTOTYPE_DECISIONS.md`
authoritative, across three repositories plus one uncommittable scaffold.

## 3. Repository start and end state

| Repository | Start branch | Start HEAD | End branch | End HEAD |
|---|---|---|---|---|
| `claim_evidence` | `fix/m1-m9-remediation` | `0c245ad` | `feat/lean-prototype` | `ab0b34a` |
| `document_extract` | `audit-fixes` | `8e59206` | `feat/lean-prototype` | `5f5ed04` |
| `gw_detector_v2` | `master` | `29d095b` | `feat/lean-prototype` | `e7caa91` |
| `document_knowledge` | not a repository | — | not a repository | — (see `IMPL-001`) |

All three implementation branches were created fresh from the recorded starting
HEADs. No existing commit was amended, squashed, rebased, reset, or force-pushed.

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

## 5. Findings

Fourteen new findings are recorded in `IMPLEMENTATION_FINDINGS.md`. Three are
material product bugs found and fixed in passing:

- `IMPL-013`: the fact subject was still filename-derived, so with the audit
  side made explicit every supported claim returned `insufficient`.

- `IMPL-008`: every narrative citation resolved its artifact to nothing.
- `IMPL-007`: extraction settings did not reach the build fingerprint, so a
  re-extraction under different rules could be reused as if unchanged.

`IMPL-012` records the incomplete plan. `IMPL-011` records that the retained
494-page Danone extraction predates the run contract and must be re-extracted
before it can be indexed.

Findings closed from `COMPLETE_GAP_REGISTER.md`: GR-001, GR-002, GR-003,
GR-004, GR-008, GR-009, GR-010, GR-012, GR-013, GR-018, GR-020, GR-021,
GR-023, GR-024, GR-025, GR-034, GR-035, GR-041, GR-047, GR-049, GR-051,
GR-056, GR-059, GR-060, GR-061, GR-063, GR-065, GR-066, GR-070, GR-071,
GR-072, GR-C01, GR-C02, GR-C05, GR-C06, GR-C10, GR-C11, GR-I02, GR-I03,
GR-I04, GR-I09, GR-I11, GR-I15, GR-I16, GR-I17, GR-I12, GR-I13, GR-P01,
GR-P03, GR-P04, GR-P05, GR-P07, GR-P11, GR-P12, GR-P15, GR-T04, GR-T06,
GR-T09, PV-004, PV-007, PV-008, PV-014.

Also closed under LP-08 and LP-09: GR-022, GR-026, GR-028, GR-040, GR-I05,
GR-T07.

Not addressed: GR-014, GR-015, GR-027, GR-029, GR-030, GR-031, GR-032,
GR-033, GR-037, GR-038, GR-042, GR-045, GR-046, GR-048, GR-057, GR-058,
GR-064, GR-C04, GR-C09, GR-I06, GR-I14, GR-P06, GR-P08, GR-P09, GR-P10,
GR-P14, PV-015 (partially — the runner exists).

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
| `cd claim_evidence && python tests/run_all.py` | 18 suites, all pass |
| `cd document_extract && python tests/run_all.py` | 28 suites, all pass |
| `py -3.12 -m pytest gw_detector_v2/tests -q` | 197 passed, 2 skipped |
| `py -3.12 -m pytest document_extract/tests/test_run_contract.py document_extract/tests/test_progress_contract.py claim_evidence/tests/test_contract_v2.py gw_detector_v2/tests/test_contract_v2.py -q` | 38 passed |
| `py -3.12 -m pytest claim_evidence/tests/test_db_init.py claim_evidence/tests/test_reset_dev.py -q` | 20 passed |
| `py -3.12 -m pytest claim_evidence/tests/test_identity.py claim_evidence/tests/test_fingerprint.py -q` | 22 passed |
| `py -3.12 -m pytest claim_evidence/tests/test_source.py claim_evidence/tests/test_retrieve.py claim_evidence/tests/test_integration.py -q -k "artifact or provenance or source_order or activation"` | 10 passed, 38 deselected |
| `py -3.12 -m pytest claim_evidence/tests/test_integration.py claim_evidence/tests/test_lifecycle.py gw_detector_v2/tests/test_jobs.py -q` | 19 passed |
| `py -3.12 -m pytest claim_evidence/tests/test_claim_contract.py claim_evidence/tests/test_contract_v2.py gw_detector_v2/tests/test_web.py -q -k "claim or scope or unsupported or reporting_entity"` | 43 passed, 159 deselected |
| `py -3.12 -m pytest claim_evidence/tests/test_facts.py claim_evidence/tests/test_audit_semantics.py -q` | 35 passed |
| `py -3.12 -m pytest claim_evidence/tests/test_vision.py -q` | 15 passed |
| `powershell -File gw_detector_v2\scripts\verify_prototype.ps1 -Only PA-01,PA-02` | 2/2 pass |
| `powershell -File gw_detector_v2\scripts\verify_prototype.ps1` | **fails** — 15 checks `missing` |

The two skips in `gw_detector_v2/tests` are pre-existing and unrelated to this
cycle. No test was deleted, weakened, or skipped to obtain a green result; the
two behaviour changes to existing tests (`IMPL-006`, and the claim fixtures that
LP-07 made unsupported) are recorded with their reasons.

## 8. Prototype acceptance

See `PROTOTYPE_ACCEPTANCE_RESULTS.md`. PA-01 and PA-02 pass with recorded
evidence. PA-17 fails. The other fourteen are not implemented as runner checks,
though the product behaviour behind PA-03, PA-04, PA-06, PA-13, and PA-16 is
implemented and has its own passing suite.

## 9. End-to-end workflow

Not run. The full sequence — guarded reset → schema init → PDF extraction →
indexing → audits → citations → real visual evidence → browser display —
requires LP-08 through LP-13 and a current-contract extraction (`IMPL-011`).
PostgreSQL and Ollama were both available throughout, so the blocker is
unwritten code, not an unavailable service.

## 10. Remaining blockers

1. LP-10, LP-11, LP-12, LP-13 not started.
2. LP-14 partially done: fifteen acceptance checks unregistered.
3. `IMPL-011`: the retained Danone extraction must be re-extracted under the
   current run contract before it can be indexed.
4. `IMPL-001`: `document_knowledge` edits are uncommittable.
5. The browser end-to-end suite (LP-13) needs a Chromium binary and Selenium,
   neither of which is installed here.

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

## 12. Intentionally deferred

DG-01 through DG-05 in full: no migration ledger or in-place upgrade, no
cross-process locking or second worker, no authentication/TLS/remote bind, no
multi-document authority or multilingual or layout-graph work, and no archival,
telemetry, large fixtures, latency budgets, or hardware certification. The
prototype remains English-only, loopback-only, one-process, exact-claim, and
disposable-data.
