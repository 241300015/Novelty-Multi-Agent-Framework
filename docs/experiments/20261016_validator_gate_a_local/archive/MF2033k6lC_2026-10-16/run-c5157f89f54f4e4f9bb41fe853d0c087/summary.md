# Runtime Summary

## Run

Paper ID: MF2033k6lC
Run ID: run-c5157f89f54f4e4f9bb41fe853d0c087
Entrypoint: paper_input
Input Identity: {"paper_json": "outputs/MF2033k6lC/paper-input/others/paper.json", "paper_sha256": "89fcc578efcc840daa9c4e0698943314f794e096e176c067dbbd6e603d42ea66", "novelty_point_id": null, "task_id": null, "search_plan_id": null}
Status: SUCCESS
Start: 2026-10-16T09:39:58.148744+00:00
End: 2026-10-16T09:46:13.849565+00:00
Duration: 375732 ms

## Environment

Git Branch: main
Git Commit: d7948cc455ceb235a23e448f056859d993b76ee5
Model: openai_compatible / qwen2.5-7b-instruct
Python: 3.11.15

## Stage Status

| Stage | Status | Duration |
|---|---|---:|
| extract_points | SUCCESS | 39114 ms |
| plan | SUCCESS | 0 ms |
| dispatch_planning_tasks | SUCCESS | 0 ms |
| plan_research_task | SUCCESS | 6572 ms |
| plan_research_task | SUCCESS | 6837 ms |
| dispatch_research_tasks | SUCCESS | 0 ms |
| run_research_task | SUCCESS | 135541 ms |
| run_research_task | SUCCESS | 197107 ms |
| validate_evidence | SUCCESS | 1 ms |
| review_evidence | SUCCESS | 2 ms |
| validate_synthesis_input | SUCCESS | 1 ms |
| check_final_evidence_sufficiency | SUCCESS | 1 ms |
| plan_supplement | SUCCESS | 0 ms |
| dispatch_planning_tasks | SUCCESS | 0 ms |
| plan_research_task | SUCCESS | 23897 ms |
| dispatch_research_tasks | SUCCESS | 0 ms |
| run_research_task | SUCCESS | 97588 ms |
| validate_evidence | SUCCESS | 2 ms |
| review_evidence | SUCCESS | 3 ms |
| validate_synthesis_input | SUCCESS | 1 ms |
| check_final_evidence_sufficiency | SUCCESS | 1 ms |
| synthesize_report | SUCCESS | 10555 ms |
| validate_report_integrity | SUCCESS | 0 ms |
| persist_report | SUCCESS | 2 ms |
| render_report | SUCCESS | 2 ms |

## Tool Calls

| Tool | Calls | Success | Failed | Empty |
|---|---:|---:|---:|---:|
| database_search | 6 | 4 | 2 | 0 |
| reader | 41 | 39 | 2 | 0 |

## Runtime Diagnostics

| Diagnostic | Status | Calls | Failed | Primary finding | Detail |
|---|---|---:|---:|---|---|
| reader | WARNING | 41 | 2 | NEVER_PERSISTED | `diagnostics/reader.json` |
| reference_namespace | ERROR | 41 | 0 | - | `diagnostics/reference_namespace.json` |
| reviewer | WARNING | 0 | 0 | - | `diagnostics/reviewer.json` |

reader classifications: NEVER_PERSISTED=1, OTHER=1

## Run Outcome

- None

## LLM Token Usage and Cost

Calls: 62
Input tokens: 585147
Cached input tokens: 0
Output tokens: 9500
Reasoning tokens: 0
Total tokens: 594647
Estimated API cost (RMB): Unknown (priced subtotal 0.00000000)
Cost completeness: PARTIAL

| Model | Calls | Input | Cached | Output | Total | Cost (RMB) | Unpriced |
|---|---:|---:|---:|---:|---:|---:|---:|
| qwen2.5-7b-instruct | 62 | 585147 | 0 | 9500 | 594647 | Unknown (priced subtotal 0.00000000) | 62 |

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


## Reviewer Information Adjudication

### Stage stage_0010

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

### Stage stage_0019

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


## Errors

- run_research_task / tool_0018: ValidationError: 1 validation error for DatabaseSearchArguments
full_text_source_record_ids
  List should have at most 4 items after validation, not 8 [type=too_long, input_value=['src_39e0d195edbcbf801f2...bb387aeacfde4a1866df75'], input_type=list]
    For further information visit https://errors.pydantic.dev/2.13/v/too_long
- run_research_task / tool_0029: HarnessPolicyError: reader tool-call budget exhausted
- run_research_task / tool_0043: ValidationError: 1 validation error for DatabaseSearchArguments
full_text_source_record_ids
  List should have at most 4 items after validation, not 8 [type=too_long, input_value=['src_39e0d195edbcbf801f2...bb387aeacfde4a1866df75'], input_type=list]
    For further information visit https://errors.pydantic.dev/2.13/v/too_long
- run_research_task / tool_0047: ValueError: unknown artifact_id 'art_649f84754254b648f4199871' in the research or subject reference manifest

## Integrity Gates

- validate_evidence: PASS
- validate_synthesis_input: PASS
- validate_evidence: PASS
- validate_synthesis_input: PASS
- validate_report_integrity: PASS

## Last Completed Stage

render_report
