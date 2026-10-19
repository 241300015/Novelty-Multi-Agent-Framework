# Runtime Summary

## Run

Paper ID: MF2033k6lC
Run ID: run-20261019T052457976377Z-fbfaa4bc3f
Entrypoint: -
Input Identity: {}
Status: SUCCESS
Start: 2026-10-19T05:24:57.976440+00:00
End: 2026-10-19T05:25:18.632441+00:00
Duration: 21327 ms

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
| reference_namespace | OK | 0 | 0 | - | `diagnostics/reference_namespace.json` |
| reviewer | ERROR | 0 | 0 | - | `diagnostics/reviewer.json` |

## Run Outcome

- None

## LLM Token Usage and Cost

Calls: 3
Input tokens: 17176
Cached input tokens: 0
Output tokens: 725
Reasoning tokens: 0
Total tokens: 17901
Estimated API cost (RMB): Unknown (priced subtotal 0.00000000)
Cost completeness: PARTIAL

| Model | Calls | Input | Cached | Output | Total | Cost (RMB) | Unpriced |
|---|---:|---:|---:|---:|---:|---:|---:|
| qwen2.5-7b-instruct | 3 | 17176 | 0 | 725 | 17901 | Unknown (priced subtotal 0.00000000) | 3 |

## Final Evidence Sufficiency Checks

- None

## Reviewer Information Adjudication

- None

## Errors

- None

## Integrity Gates

- None

## Last Completed Stage

None
