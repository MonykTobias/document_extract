# Implementation Progress

Running status of every active `LP-*` task in `LEAN_CORRECTIVE_PLAN.md`.
Statuses: `Not started`, `In progress`, `Completed`, `Partially completed`,
`Blocked`.

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

## Task status

| Task | Status | Repository | Commit SHA | Tests | Acceptance | Notes |
|---|---|---|---|---|---|---|
| LP-01 | Completed | claim_evidence, document_extract, gw_detector_v2 | `e6d4696`, `6a2a520`, `1000eb4` | `python tests/run_all.py` (both packages), `pytest gw_detector_v2/tests` | PA-01 pass, PA-02 pass | `document_knowledge` marked non-runtime but uncommittable (`IMPL-001`) |
| LP-02 | Completed | document_extract, claim_evidence, gw_detector_v2 | `5f5ed04`, `65e7137`, `0c099da` | `pytest document_extract/tests/test_run_contract.py document_extract/tests/test_progress_contract.py claim_evidence/tests/test_contract_v2.py gw_detector_v2/tests/test_contract_v2.py -q` → 38 passed | not yet exercised by a PA check | `insufficient` frozen as the one verdict literal (`IMPL-004`); contracts ship inside the packages (`IMPL-005`) |
| LP-03 | Completed | claim_evidence, gw_detector_v2 | `c754016`, `1a363fe` | `pytest claim_evidence/tests/test_db_init.py claim_evidence/tests/test_reset_dev.py -q` → 20 passed | PA-03, PA-04 covered by tests; not yet wired into the runner | legacy-migration integration check replaced by its successor (`IMPL-006`) |
| LP-04 | Completed | claim_evidence | `aaa1eed` | `pytest claim_evidence/tests/test_identity.py claim_evidence/tests/test_fingerprint.py -q` → 22 passed | PA-06 covered by tests; not yet wired into the runner | run.json settings had to join the fingerprint (`IMPL-007`) |
| LP-05 | Completed | claim_evidence | `6df072b` | `pytest claim_evidence/tests/test_source.py claim_evidence/tests/test_retrieve.py claim_evidence/tests/test_integration.py -q -k "artifact or provenance or source_order or activation"` → 10 passed | PA-07, PA-08 partially covered | narrative artifact path was broken and is fixed (`IMPL-008`) |
| LP-06 | Completed | claim_evidence, gw_detector_v2 | `c889f94`, `9f4402a` | `pytest claim_evidence/tests/test_lifecycle.py gw_detector_v2/tests/test_jobs.py -q` → 19 passed | PA-16 covered by tests; not yet wired into the runner | |
| LP-07 | Completed | claim_evidence, gw_detector_v2 | `c669097`, `16c0596` | `pytest claim_evidence/tests/test_claim_contract.py claim_evidence/tests/test_contract_v2.py gw_detector_v2/tests/test_web.py -q -k "claim or scope or unsupported or reporting_entity"` → 43 passed | PA-13 covered by tests; not yet wired into the runner | `audit_claim` signature changed (`IMPL-009`); UI now asks for the reporting entity |
| LP-08 | Not started | | | | | Depends on LP-05, LP-07 — both complete, so unblocked |
| LP-09 | Not started | | | | | Depends on LP-05, LP-08 |
| LP-10 | Not started | | | | | Depends on LP-02, LP-05, LP-09 |
| LP-11 | Not started | | | | | Depends on LP-01, LP-10 |
| LP-12 | Not started | | | | | Depends on LP-06, LP-11 |
| LP-13 | Not started | | | | | Depends on LP-08, LP-09, LP-10, LP-12 |
| LP-14 | Partially completed | gw_detector_v2 | `1000eb4` | `powershell -File .\scripts\verify_prototype.ps1 -Only PA-01,PA-02` → 2/2 pass | PA-01, PA-02 implemented; PA-03..PA-17 report `missing` | The runner, its report format, and the zero-skip rule exist; 15 of 17 checks are not yet registered |

## Regression status at the last commit

| Suite | Command | Result |
|---|---|---|
| `claim_evidence` | `python tests/run_all.py` | 16 suites, all green |
| `document_extract` | `python tests/run_all.py` | 28 suites, all green |
| `gw_detector_v2` | `py -3.12 -m pytest tests -q` | 191 passed, 2 skipped |
