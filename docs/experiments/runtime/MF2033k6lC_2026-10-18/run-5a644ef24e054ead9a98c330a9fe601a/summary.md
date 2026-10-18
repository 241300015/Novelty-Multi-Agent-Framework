# Runtime Summary

## Run

Paper ID: MF2033k6lC
Run ID: run-5a644ef24e054ead9a98c330a9fe601a
Entrypoint: paper_input
Input Identity: {"paper_json": "outputs/MF2033k6lC/paper-input/others/paper.json", "paper_sha256": "89fcc578efcc840daa9c4e0698943314f794e096e176c067dbbd6e603d42ea66", "novelty_point_id": null, "task_id": null, "search_plan_id": null}
Status: INTERRUPTED
Start: 2026-10-18T16:52:08.904668+00:00
End: 2026-10-18T16:54:45.927591+00:00
Duration: 157053 ms

## Environment

Git Branch: feat/p0-06-renderer-default
Git Commit: 35e7a0b55b50814d7f4ad20ce951c1679401e9ab
Model: openai_compatible / qwen2.5-7b-instruct
Python: 3.11.15

## Stage Status

| Stage | Status | Duration |
|---|---|---:|
| extract_points | SUCCESS | 18239 ms |
| plan | SUCCESS | 0 ms |
| dispatch_planning_tasks | SUCCESS | 0 ms |
| plan_research_task | SUCCESS | 8784 ms |
| plan_research_task | SUCCESS | 6929 ms |
| plan_research_task | SUCCESS | 6479 ms |
| dispatch_research_tasks | SUCCESS | 0 ms |
| run_research_task | SUCCESS | 73951 ms |
| run_research_task | FAILED | 125265 ms |
| run_research_task | SUCCESS | 29200 ms |
| validate_evidence | NOT_RUN | - ms |
| review_evidence | NOT_RUN | - ms |
| validate_synthesis_input | NOT_RUN | - ms |
| check_final_evidence_sufficiency | NOT_RUN | - ms |
| plan_supplement | NOT_RUN | - ms |
| synthesize_report | NOT_RUN | - ms |
| validate_report_integrity | NOT_RUN | - ms |
| persist_report | NOT_RUN | - ms |
| render_report | NOT_RUN | - ms |

## Tool Calls

| Tool | Calls | Success | Failed | Empty |
|---|---:|---:|---:|---:|
| database_search | 5 | 4 | 1 | 3 |
| reader | 10 | 7 | 3 | 0 |
| reference_search | 2 | 2 | 0 | 2 |

## Runtime Diagnostics

| Diagnostic | Status | Calls | Failed | Primary finding | Detail |
|---|---|---:|---:|---|---|
| reader | WARNING | 10 | 3 | NEVER_PERSISTED | `diagnostics/reader.json` |
| reference_namespace | ERROR | 10 | 0 | - | `diagnostics/reference_namespace.json` |
| reviewer | ERROR | 0 | 0 | - | `diagnostics/reviewer.json` |

reader classifications: NEVER_PERSISTED=3

## Run Outcome

- None

## LLM Token Usage and Cost

Calls: 25
Input tokens: 137979
Cached input tokens: 0
Output tokens: 4396
Reasoning tokens: 0
Total tokens: 142375
Estimated API cost (RMB): Unknown (priced subtotal 0.00000000)
Cost completeness: PARTIAL

| Model | Calls | Input | Cached | Output | Total | Cost (RMB) | Unpriced |
|---|---:|---:|---:|---:|---:|---:|---:|
| qwen2.5-7b-instruct | 25 | 137979 | 0 | 4396 | 142375 | Unknown (priced subtotal 0.00000000) | 24 |

## Final Evidence Sufficiency Checks

- None

## Reviewer Information Adjudication

- None

## Errors

- run_research_task / tool_0009: ValidationError: 1 validation error for DatabaseSearchArguments
full_text_source_record_ids.0
  Input should be a valid string [type=string_type, input_value=123456, input_type=int]
    For further information visit https://errors.pydantic.dev/2.13/v/string_type
- run_research_task / tool_0015: ValueError: unknown artifact_id 'art_3f89aec978a4eed8f221cf1b' in the research or subject reference manifest
- run_research_task / tool_0016: ValueError: unknown artifact_id 'art_701480b32642b7486ed51ac6' in the research or subject reference manifest
- run_research_task / tool_0017: ValueError: unknown artifact_id 'art_09214cbbc59fe401d9d390c5' in the research or subject reference manifest
- run_research_task / -: CancelledError: <object object at 0x788febd876e0>
- - / -: CancelledError: 

## Integrity Gates

- None

## Last Completed Stage

run_research_task
