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

## IMPL-011 — the retained 494-page Danone extraction is no longer indexable

- **Evidence:** `claim_evidence/tests/test_source.py::test_real_danone_page_359`
  now skips: `gw_detector_v2/outputs_full_run/danoneurdaccessible/` has no
  `run.json`.
- **Severity:** note · **Component:** retained extraction output · **Under:** LP-02
- **Detail:** PD-02 states there is no compatibility reader for output written
  before the current run contract, and that such output is rejected with a
  re-extraction instruction. The existing Danone run predates it, so it must be
  re-extracted before it can be indexed again. That re-extraction needs Docling
  and a GPU and was not performed in this cycle.
- **Activation trigger:** any acceptance item that needs live Danone data.
  **Blocks an active task:** it will block the live end-to-end portion of LP-14
  until a current-contract extraction exists.

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

## IMPL-012 — five plan tasks are not implemented in this cycle

- **Evidence:** `IMPLEMENTATION_PROGRESS.md` records LP-10 through LP-13 as
  `Not started` and LP-14 as `Partially completed`.
- **Severity:** blocker · **Component:** the plan as a whole
- **Detail:** LP-01 through LP-09 are implemented, tested, and committed. The
  remaining work — the public-surface and prompt-injection boundary (LP-10),
  loopback/CSRF/limits (LP-11), the browser job workflow and restart journal
  (LP-12), the evaluation corpus and browser end-to-end suite (LP-13), and the
  remaining fifteen acceptance checks (LP-14) — has not been done. The
  acceptance runner reports those checks as `missing`, which fails the run, so
  nothing here reports itself as accepted.
- **Recommended action:** continue the plan from LP-10; its dependencies
  (LP-02, LP-05, LP-09) are complete. **Blocks an active task:** yes — the prototype is
  not accepted.
