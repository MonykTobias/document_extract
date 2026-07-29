# Prototype Acceptance Results

Results for every check in `PROTOTYPE_ACCEPTANCE_CHECKLIST.md`.

Runner: `gw_detector_v2\scripts\verify_prototype.ps1`
Target database: `claim_evidence_test` (disposable)
Latest partial run: `gw_detector_v2/verification/prototype/prototype-20260729T175949Z.json`

**Overall outcome: NOT ACCEPTED.** Two of seventeen checks are implemented and
pass. The other fifteen are not registered with the runner, which reports them
as `missing` and exits non-zero — a missing check is never a pass.

Results: `Passed`, `Failed`, `Blocked`, `Not implemented`.

| Acceptance item | Result | Command/test | Evidence | Related tasks | Commit SHAs |
|---|---|---|---|---|---|
| PA-01 clean install, three packages, `document_knowledge` not a runtime backend | Passed | `verify_prototype.ps1 -Only PA-01` | Both packages import from a neutral directory with no `PYTHONPATH`; `document_knowledge` is not importable; no repository file inserts a checkout onto `sys.path`; `claim-evidence` CLI exits 0 | LP-01 | `e6d4696`, `6a2a520`, `1000eb4` |
| PA-02 real frontend starts on loopback | Passed | `verify_prototype.ps1 -Only PA-02` | Bound `127.0.0.1:55154`; `GET /` 200; `GET /api/health` 200 with `database_reachable: true`; also starts under `GW_FAKE_ADAPTER=1` with both endpoints 200 | LP-01 | `1000eb4` |
| PA-03 empty database initializes once and repeats without mutation | Not implemented (behaviour covered by tests) | `pytest claim_evidence/tests/test_db_init.py -q` → 10 passed | Marker records version, schema-file SHA-256, and initialization time; a repeat init returns `unchanged` and rewrites nothing | LP-03 | `c754016` |
| PA-04 destructive reset is confined and preserves source data | Not implemented (behaviour covered by tests) | `pytest claim_evidence/tests/test_reset_dev.py -q` → 10 passed | Every PD-03 guard refuses; a confirmed reset leaves 10 source/config/extraction files byte-identical | LP-03 | `c754016`, `1a363fe` |
| PA-05 retained PDF parses and indexes through the frontend workflow | Not implemented | — | The run contract, its consumer, and the progress consumer exist and are tested; the end-to-end run has not been performed | LP-02, LP-06 | `5f5ed04`, `65e7137`, `0c099da` |
| PA-06 re-indexing is deterministic and reuses identity | Not implemented (behaviour covered by tests) | `pytest claim_evidence/tests/test_identity.py claim_evidence/tests/test_fingerprint.py -q` → 22 passed | Same PDF at another path is one document; every evidence-bearing artifact moves the fingerprint; operational settings do not; an exact repeat is byte-identical | LP-04 | `aaa1eed` |
| PA-07 narrative evidence retains quote, page, bbox, artifact locator | Not implemented (partly covered) | `pytest claim_evidence/tests/test_source.py -q -k "artifact or provenance"` | Narrative provenance now resolves to the real root `blocks.jsonl`; activation refuses a build whose citations do not resolve | LP-05 | `6df072b` |
| PA-08 table evidence retains cell/value context, page, and bbox | Not implemented (partly covered) | `pytest claim_evidence/tests/test_source.py -q` | Table row/value units carry descriptor, header path, unit, and four cited regions | LP-05 | `6df072b` |
| PA-09 real visual evidence path returns and displays a source crop | Not implemented | — | LP-09 not started | LP-09, LP-13 | — |
| PA-10 supported claim returns `supported` with deterministic comparison | Not implemented | — | LP-08 not started | LP-08 | — |
| PA-11 conflicting evidence returns `contradicted` | Not implemented | — | LP-08 not started | LP-08 | — |
| PA-12 absent evidence returns `insufficient` | Not implemented | — | LP-08 not started. The verdict literal is frozen as `insufficient` (`IMPL-004`) | LP-02, LP-08 | `65e7137` |
| PA-13 unsupported claims are rejected before retrieval with no audit row | Not implemented (behaviour covered by tests) | `pytest claim_evidence/tests/test_claim_contract.py -q` → 25 passed | Sixteen unsupported classes refuse by stable reason code, mapped to HTTP 422, with no database or model touched | LP-07 | `c669097`, `16c0596` |
| PA-14 every citation and asset resolves and stays bound to its version | Not implemented | — | Activation-time containment is in place; the public traversal check is not | LP-05, LP-10 | `6df072b` |
| PA-15 public surfaces leak no paths, credentials, exceptions, or prompts | Not implemented (partly covered) | `pytest claim_evidence/tests/test_progress.py -q`, `pytest gw_detector_v2/tests -q` | Progress events carry code-like scalars only; contract refusals never echo values; existing sentinel tests still pass. The LP-10 recursive sentinel sweep is not written | LP-10 | — |
| PA-16 interruption is represented honestly and ready data survives | Not implemented (behaviour covered by tests) | `pytest claim_evidence/tests/test_lifecycle.py -q` → 10 passed | Dead builds and audits reconcile to `interrupted`, never `failed`; a ready version is untouched and still queryable | LP-06 | `c889f94`, `9f4402a` |
| PA-17 one runner completes reset-to-render with all rows present and no skip | Failed | `verify_prototype.ps1` | The runner, report format, and zero-skip rule exist and work; fifteen checks report `missing`, so the run exits non-zero as designed | LP-14 | `1000eb4` |

## What "Not implemented" means here

It is not a skip and not a pass. The runner has no check registered for that
id, so a full run reports it `missing` and fails. Where a row says *behaviour
covered by tests*, the product behaviour the check describes is implemented and
has its own passing suite — what is missing is the acceptance check that
exercises it through the runner, end to end, against a live database and model.

No mandatory item is marked *not applicable*.
