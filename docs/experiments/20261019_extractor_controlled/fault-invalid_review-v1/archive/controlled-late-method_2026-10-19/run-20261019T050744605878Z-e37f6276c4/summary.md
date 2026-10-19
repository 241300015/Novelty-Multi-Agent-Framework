# Runtime Summary

## Run

Paper ID: controlled-late-method
Run ID: run-20261019T050744605878Z-e37f6276c4
Entrypoint: -
Input Identity: {}
Status: FAILED
Start: 2026-10-19T05:07:44.605910+00:00
End: 2026-10-19T05:08:06.217799+00:00
Duration: 22377 ms

## Environment

Git Branch: experiment/extractor-harness-20261019
Git Commit: ee5ca8b477a597bfcb334f0a6eafb5f0a757114c
Model: openai_compatible / qwen2.5-7b-instruct
Python: 3.11.15

## Stage Status

| Stage | Status | Duration |
|---|---|---:|
| extract_points | NOT_RUN | - ms |

## Tool Calls

| Tool | Calls | Success | Failed | Empty |
|---|---:|---:|---:|---:|

## Runtime Diagnostics

| Diagnostic | Status | Calls | Failed | Primary finding | Detail |
|---|---|---:|---:|---|---|
| reader | INCOMPLETE | 0 | 0 | - | `diagnostics/reader.json` |
| reference_namespace | INCOMPLETE | 0 | 0 | - | `diagnostics/reference_namespace.json` |
| reviewer | INCOMPLETE | 0 | 0 | - | `diagnostics/reviewer.json` |

## Run Outcome

- None

## LLM Token Usage and Cost

Calls: 6
Input tokens: 9943
Cached input tokens: 0
Output tokens: 905
Reasoning tokens: 0
Total tokens: 10848
Estimated API cost (RMB): Unknown (priced subtotal 0.00000000)
Cost completeness: PARTIAL

| Model | Calls | Input | Cached | Output | Total | Cost (RMB) | Unpriced |
|---|---:|---:|---:|---:|---:|---:|---:|
| qwen2.5-7b-instruct | 6 | 9943 | 0 | 905 | 10848 | Unknown (priced subtotal 0.00000000) | 6 |

## Final Evidence Sufficiency Checks

- None

## Reviewer Information Adjudication

- None

## Errors

- - / -: ExtractorHarnessError: model.output.schema_invalid: invalid output persisted after one correction

## Integrity Gates

- None

## Last Completed Stage

None
