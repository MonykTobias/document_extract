# Implementation Progress

Running status of every active `LP-*` task in `LEAN_CORRECTIVE_PLAN.md`.
Statuses: `Not started`, `In progress`, `Completed`, `Partially completed`,
`Blocked`.

**All fourteen tasks are `Completed`.** The acceptance runner reports 17/17 and
`PROTOTYPE ACCEPTED`.

## Repository starting state

| Repository | Starting branch | Starting HEAD | Implementation branch |
|---|---|---|---|
| `claim_evidence` | `fix/m1-m9-remediation` | `0c245adac67f4b6b18477fb07564eef0da0d9669` | `feat/lean-prototype` |
| `document_extract` | `audit-fixes` | `8e59206822a92eaadd1f8bc066e09f54d10da809` | `feat/lean-prototype` |
| `gw_detector_v2` | `master` | `29d095bdd293d040616e45b82e155b93d3ef5bf4` | `feat/lean-prototype` |
| `document_knowledge` | not a git repository | — | changes uncommittable (`IMPL-001`) |

`document_extract` carried 27 untracked planning documents at the start. They
are pre-existing user files, left untracked and unmodified; the running reports
this cycle creates are the only new files committed there besides the contracts
package and its tests.

Two commits in `gw_detector_v2` were rewritten by tooling outside this work —
contents byte-identical, only a trailer removed. See `IMPL-021`; the SHAs below
are the current ones.

## Task status

| Task | Status | Repository | Commit SHA | Tests | Acceptance |
|---|---|---|---|---|---|
| LP-01 | Completed | claim_evidence, document_extract, gw_detector_v2 | `060e28f`, `898fe2f`, `ad0a05b` | `python tests/run_all.py` (both packages), `pytest gw_detector_v2/tests` | PA-01, PA-02 pass |
| LP-02 | Completed | document_extract, claim_evidence, gw_detector_v2 | `487b9df`, `29b1ec8`, `7a804f0` | the four contract suites → 38 passed | PA-05 pass (the contract is what the real extraction is read through) |
| LP-03 | Completed | claim_evidence, gw_detector_v2 | `e31679c`, `fd28ae1` | `pytest test_db_init.py test_reset_dev.py -q` → 20 passed | PA-03, PA-04 pass |
| LP-04 | Completed | claim_evidence | `6fcf794` | `pytest test_identity.py test_fingerprint.py -q` → 22 passed | PA-06 pass |
| LP-05 | Completed | claim_evidence | `d98fc30` | `pytest test_source.py test_retrieve.py test_integration.py -q` | PA-07, PA-08, PA-14 pass |
| LP-06 | Completed | claim_evidence, gw_detector_v2 | `e8923d2`, `2216457` | `pytest test_lifecycle.py test_jobs.py -q` → 19 passed | PA-16 pass |
| LP-07 | Completed | claim_evidence, gw_detector_v2 | `e22e00f`, `b8fe813` | `pytest -k "claim or scope or unsupported or reporting_entity"` → 43 passed | PA-13 pass |
| LP-08 | Completed | claim_evidence, gw_detector_v2 | `ac4f35a`, `66f41de` | `pytest test_facts.py test_audit_semantics.py -q` → 35 passed | PA-10, PA-11, PA-12 pass |
| LP-09 | Completed | claim_evidence, gw_detector_v2 | `601c665`, `8de6520` | `pytest test_vision.py -q` → 15 passed | PA-09 pass, including the real crop and the browser gallery |
| LP-10 | Completed | claim_evidence, gw_detector_v2 | `127ceb9`, `9852e29` | `pytest test_prompt_security.py`, `pytest gw_detector_v2/tests/test_security.py` | PA-15 pass |
| LP-11 | Completed | gw_detector_v2 | `5375a39` | `pytest gw_detector_v2/tests -q` | PA-02, PA-15 pass |
| LP-12 | Completed | gw_detector_v2 | `dd9e3ad` | `pytest tests/test_jobs.py -q` | PA-16 pass |
| LP-13 | Completed | gw_detector_v2, claim_evidence | `91699c5`, `b97a1d5`, `4fdc2a1` | `pytest tests/e2e/test_browser.py` → 10 passed; `pytest tests/acceptance/test_corpus.py` → 9 passed | PA-09, PA-15 pass |
| LP-14 | Completed | gw_detector_v2, claim_evidence, document_extract | `75db8a7`, `57f0bfc`, `4cfd3f6` | `verify_prototype.ps1` → 17/17 | PA-01..PA-17 all pass |

## Regression status at the last commit

| Suite | Command | Result |
|---|---|---|
| `claim_evidence` | `python tests/run_all.py` | all suites green |
| `claim_evidence` corpus | `pytest tests/acceptance/test_corpus.py -q` | 9 passed against PostgreSQL |
| `document_extract` | `python tests/run_all.py` | 28 suites, all green |
| `gw_detector_v2` | `py -3.12 -m pytest tests -q` | 246 passed, 2 skipped (includes 10 real-browser checks) |
| acceptance | `scripts\verify_prototype.ps1` | 17/17 pass, PROTOTYPE ACCEPTED |
