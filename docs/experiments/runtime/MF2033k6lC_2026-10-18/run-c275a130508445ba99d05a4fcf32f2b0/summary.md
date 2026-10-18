# Runtime Summary

## Run

Paper ID: MF2033k6lC
Run ID: run-c275a130508445ba99d05a4fcf32f2b0
Entrypoint: paper_input
Input Identity: {"paper_json": "outputs/MF2033k6lC/paper-input/others/paper.json", "paper_sha256": "89fcc578efcc840daa9c4e0698943314f794e096e176c067dbbd6e603d42ea66", "novelty_point_id": null, "task_id": null, "search_plan_id": null}
Status: FAILED
Start: 2026-10-18T16:45:06.218386+00:00
End: 2026-10-18T16:50:14.791994+00:00
Duration: 308611 ms

## Environment

Git Branch: feat/p0-06-renderer-default
Git Commit: 35e7a0b55b50814d7f4ad20ce951c1679401e9ab
Model: openai_compatible / qwen2.5-7b-instruct
Python: 3.11.15

## Stage Status

| Stage | Status | Duration |
|---|---|---:|
| extract_points | SUCCESS | 20538 ms |
| plan | SUCCESS | 0 ms |
| dispatch_planning_tasks | SUCCESS | 0 ms |
| plan_research_task | SUCCESS | 6701 ms |
| plan_research_task | SUCCESS | 6806 ms |
| plan_research_task | SUCCESS | 6726 ms |
| plan_research_task | SUCCESS | 6867 ms |
| dispatch_research_tasks | SUCCESS | 0 ms |
| run_research_task | SUCCESS | 104420 ms |
| run_research_task | SUCCESS | 175097 ms |
| run_research_task | SUCCESS | 114327 ms |
| run_research_task | SUCCESS | 45619 ms |
| validate_evidence | SUCCESS | 14 ms |
| review_evidence | SUCCESS | 29303 ms |
| validate_synthesis_input | SUCCESS | 4 ms |
| check_final_evidence_sufficiency | SUCCESS | 3 ms |
| synthesize_report | FAILED | 24037 ms |
| plan_supplement | NOT_RUN | - ms |
| validate_report_integrity | NOT_RUN | - ms |
| persist_report | NOT_RUN | - ms |
| render_report | NOT_RUN | - ms |

## Tool Calls

| Tool | Calls | Success | Failed | Empty |
|---|---:|---:|---:|---:|
| database_search | 4 | 4 | 0 | 1 |
| reader | 39 | 37 | 2 | 0 |
| reference_search | 1 | 1 | 0 | 1 |

## Runtime Diagnostics

| Diagnostic | Status | Calls | Failed | Primary finding | Detail |
|---|---|---:|---:|---|---|
| reader | WARNING | 39 | 2 | NEVER_PERSISTED | `diagnostics/reader.json` |
| reference_namespace | ERROR | 39 | 0 | - | `diagnostics/reference_namespace.json` |
| reviewer | WARNING | 0 | 0 | - | `diagnostics/reviewer.json` |

reader classifications: NEVER_PERSISTED=1, OTHER=1

## Run Outcome

- None

## LLM Token Usage and Cost

Calls: 60
Input tokens: 471877
Cached input tokens: 0
Output tokens: 10902
Reasoning tokens: 0
Total tokens: 482779
Estimated API cost (RMB): Unknown (priced subtotal 0.00000000)
Cost completeness: PARTIAL

| Model | Calls | Input | Cached | Output | Total | Cost (RMB) | Unpriced |
|---|---:|---:|---:|---:|---:|---:|---:|
| qwen2.5-7b-instruct | 60 | 471877 | 0 | 10902 | 482779 | Unknown (priced subtotal 0.00000000) | 60 |

## Final Evidence Sufficiency Checks

### Round 1

Configured cut: 1
Stage status: SUCCESS
Check status: INSUFFICIENT
Input final valid Cards: 2
Ignored Cards: 0
Output matches calculation: True
Round limit allows supplement: False
Actual next stage: synthesize_report

| Novelty Point | Valid Cards | Required Cards | Status |
|---|---:|---:|---|
| NP-1 | 0 | 1 | INSUFFICIENT |
| NP-2 | 2 | 1 | PASS |
| NP-3 | 0 | 1 | INSUFFICIENT |
| NP-4 | 0 | 1 | INSUFFICIENT |


## Reviewer Information Adjudication

### Stage stage_0014

Stage status: SUCCESS
Cards preserved: True
Supplement requests control route: False
Missing reviews: []
Unexpected reviews: []
Duplicate reviews: []
Unresolved Evidence IDs: []
Actual next stage: validate_synthesis_input

| Novelty Point | Status | Verdict | Relevant Works | Supplement |
|---|---|---|---:|---|
| NP-1 | insufficient_evidence | - | 0 | True |
| NP-2 | insufficient_evidence | - | 1 | True |
| NP-3 | insufficient_evidence | - | 0 | True |
| NP-4 | insufficient_evidence | - | 0 | True |


## Errors

- run_research_task / tool_0026: HarnessPolicyError: reader tool-call budget exhausted
- run_research_task / tool_0043: ValueError: unknown artifact_id 'art_3f89aec978a4eed8f221cf1b' in the research or subject reference manifest
- synthesize_report / -: WorkflowExecutionError: Coordinator 未生成合法 NoveltyReport：draft_contract_error: missing conclusion: NP-3; missing conclusion: NP-4
- - / -: WorkflowExecutionError: Coordinator 未生成合法 NoveltyReport：draft_contract_error: missing conclusion: NP-3; missing conclusion: NP-4

## Integrity Gates

- validate_evidence: PASS
- validate_synthesis_input: PASS

## Last Completed Stage

check_final_evidence_sufficiency
