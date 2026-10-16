# Runtime Summary

## Run

Paper ID: MF2033k6lC
Run ID: run-46029135375c43c99bac5709b69f3ce9
Entrypoint: paper_input
Input Identity: {"paper_json": "outputs/MF2033k6lC/paper-input/others/paper.json", "paper_sha256": "89fcc578efcc840daa9c4e0698943314f794e096e176c067dbbd6e603d42ea66", "novelty_point_id": null, "task_id": null, "search_plan_id": null}
Status: SUCCESS
Start: 2026-10-16T10:29:19.092055+00:00
End: 2026-10-16T10:34:23.725691+00:00
Duration: 304653 ms

## Environment

Git Branch: main
Git Commit: d7948cc455ceb235a23e448f056859d993b76ee5
Model: openai_compatible / qwen2.5-7b-instruct
Python: 3.11.15

## Stage Status

| Stage | Status | Duration |
|---|---|---:|
| extract_points | SUCCESS | 20062 ms |
| plan | SUCCESS | 0 ms |
| dispatch_planning_tasks | SUCCESS | 0 ms |
| plan_research_task | SUCCESS | 7845 ms |
| plan_research_task | SUCCESS | 5502 ms |
| plan_research_task | SUCCESS | 6986 ms |
| plan_research_task | SUCCESS | 6662 ms |
| dispatch_research_tasks | SUCCESS | 0 ms |
| run_research_task | SUCCESS | 194335 ms |
| run_research_task | SUCCESS | 116907 ms |
| run_research_task | SUCCESS | 177068 ms |
| run_research_task | SUCCESS | 136728 ms |
| validate_evidence | SUCCESS | 2 ms |
| review_evidence | SUCCESS | 3 ms |
| validate_synthesis_input | SUCCESS | 1 ms |
| check_final_evidence_sufficiency | SUCCESS | 1 ms |
| plan_supplement | SUCCESS | 0 ms |
| dispatch_planning_tasks | SUCCESS | 0 ms |
| plan_research_task | SUCCESS | 5058 ms |
| plan_research_task | SUCCESS | 5666 ms |
| dispatch_research_tasks | SUCCESS | 0 ms |
| run_research_task | SUCCESS | 57428 ms |
| run_research_task | SUCCESS | 62337 ms |
| validate_evidence | SUCCESS | 2 ms |
| review_evidence | SUCCESS | 3 ms |
| validate_synthesis_input | SUCCESS | 1 ms |
| check_final_evidence_sufficiency | SUCCESS | 2 ms |
| synthesize_report | SUCCESS | 13791 ms |
| validate_report_integrity | SUCCESS | 0 ms |
| persist_report | SUCCESS | 2 ms |
| render_report | SUCCESS | 2 ms |

## Tool Calls

| Tool | Calls | Success | Failed | Empty |
|---|---:|---:|---:|---:|
| database_search | 7 | 7 | 0 | 4 |
| reader | 6 | 6 | 0 | 0 |

## Runtime Diagnostics

| Diagnostic | Status | Calls | Failed | Primary finding | Detail |
|---|---|---:|---:|---|---|
| reader | OK | 6 | 0 | - | `diagnostics/reader.json` |
| reference_namespace | ERROR | 6 | 0 | - | `diagnostics/reference_namespace.json` |
| reviewer | WARNING | 0 | 0 | - | `diagnostics/reviewer.json` |

## Run Outcome

- None

## LLM Token Usage and Cost

Calls: 73
Input tokens: 455610
Cached input tokens: 0
Output tokens: 6501
Reasoning tokens: 0
Total tokens: 462111
Estimated API cost (RMB): Unknown (priced subtotal 0.00000000)
Cost completeness: PARTIAL

| Model | Calls | Input | Cached | Output | Total | Cost (RMB) | Unpriced |
|---|---:|---:|---:|---:|---:|---:|---:|
| qwen2.5-7b-instruct | 73 | 455610 | 0 | 6501 | 462111 | Unknown (priced subtotal 0.00000000) | 72 |

## Final Evidence Sufficiency Checks

### Round 1

Configured cut: 1
Stage status: SUCCESS
Check status: INSUFFICIENT
Input final valid Cards: 0
Ignored Cards: 0
Output matches calculation: True
Round limit allows supplement: True
Actual next stage: plan_supplement

| Novelty Point | Valid Cards | Required Cards | Status |
|---|---:|---:|---|
| NP-1 | 0 | 1 | INSUFFICIENT |
| NP-2 | 0 | 1 | INSUFFICIENT |
| NP-3 | 0 | 1 | INSUFFICIENT |
| NP-4 | 0 | 1 | INSUFFICIENT |

### Round 2

Configured cut: 1
Stage status: SUCCESS
Check status: INSUFFICIENT
Input final valid Cards: 0
Ignored Cards: 0
Output matches calculation: True
Round limit allows supplement: False
Actual next stage: synthesize_report

| Novelty Point | Valid Cards | Required Cards | Status |
|---|---:|---:|---|
| NP-1 | 0 | 1 | INSUFFICIENT |
| NP-2 | 0 | 1 | INSUFFICIENT |
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
| NP-2 | insufficient_evidence | - | 0 | True |
| NP-3 | insufficient_evidence | - | 0 | True |
| NP-4 | insufficient_evidence | - | 0 | True |

### Stage stage_0025

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
| NP-2 | insufficient_evidence | - | 0 | True |
| NP-3 | insufficient_evidence | - | 0 | True |
| NP-4 | insufficient_evidence | - | 0 | True |


## Errors

- None

## Integrity Gates

- validate_evidence: PASS
- validate_synthesis_input: PASS
- validate_evidence: PASS
- validate_synthesis_input: PASS
- validate_report_integrity: PASS

## Last Completed Stage

render_report
