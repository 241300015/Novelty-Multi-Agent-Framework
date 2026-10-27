# Harness runtime path inventory

This inventory records the soft-deprecation boundary after the task-scoped Harness
became the formal runtime. Nothing listed here is physically removed in this phase.

## ACTIVE

- `TaskResearchRequest` and `TaskResearcherWorkflow`: canonical task scope and Harness.
- `SearchPlanner` / `SearchPlannerAgent`: invoked once by `NoveltyWorkflow` before the
  task scope is constructed.
- `DatabaseSearchTool`, `WebSearchTool`, `BrowserTool`, `ReaderTool`: canonical tools.
- `StructuredSourceRetrievalTool`: active database implementation, consuming only
  `StructuredSourceRetrievalRequest.search_plan` at runtime.
- `SearchTool`, `FullTextTool`, `MetadataTool`: active provider-side ports behind
  `RetrievalSource`; they are not Researcher-facing tools.

## COMPATIBILITY

- `StructuredSourceRetrievalTool.search_planner` and matching factory arguments:
  retained for old constructors, ignored by the active execution path.
- `StructuredRetrievalResearcherTool` and `StructuredRetrievalToolArguments`: retained
  for historical experiments and null-catalog regressions. New code must use
  `DatabaseSearchTool`.
- `NoveltyResearchAgent` / `LiteratureResearchAgent`: retained for older tests and
  experiments; the formal workflow uses `TaskResearcherWorkflow`.
- `DemoQueryAdapter`: used by deterministic database tests.

## LEGACY / UNUSED

- `DemoResearchAgent`, `DemoSearchTool`: no formal workflow or current test constructs
  these implementations. They remain for historical demos only.
- `config.loader.legacy_shape`: compatibility projection for unmigrated callers; typed
  `ApplicationConfig` is the formal Composition Root.

## Constraints

- New code must not call Planner from database retrieval.
- Runtime-owned task, paper, run, and plan fields come from `TaskResearchRequest`.
- Compatibility symbols must not acquire new behavior before a separate removal task.

## Reviewer / Coordinator contract follow-up (2026-10-27)

- ACTIVE: `ReviewerEvidenceRegistry` issues evidence choices; model projections omit
  read IDs/coordinates. `review_read_integrity.validate_registered_review_reads`
  serves checkpoint, Gate A and partial-report checks using runtime-owned records.
- ACTIVE: `AsyncNoveltyCoordinator.asynthesize` → `CoordinatorSynthesisHarness`
  partitions per-point inputs, awaits all outcomes and persists a registered batch.
  `synthesize_point` accepts only `PointNarrativeDraft.summary` from the model.
- ACTIVE: Gate B checks inherited relevant-Work Card references as well as legacy
  groups. Its non-blocking policy is unchanged; `report-integrity.json` exposes the
  actual audit status in the workspace and default report.
- COMPATIBILITY: sync-only Coordinator adapters; `ReviewReadCitation` parsing;
  `ReportNarrativeDraft` for internal assembly and historical captured responses.
- COMPATIBILITY: `services/report_resources.py` verifies old captured-response
  publication bundles. It deliberately uses the historical whole-report contract.
- REPLAY: `scripts/replay_report_synthesis.py` uses point drafts in offline/live
  mode. Captured mode prefers a completed `point_synthesis_v1` batch and requires
  identical point materials; old single-response archives use an explicit adapter.
  Neither captured path makes a provider request or reruns upstream research.

## Researcher stage consistency (2026-10-27)

- ACTIVE: `ResearchReadingPolicy` separates discovery results, available material,
  automatic operations, selectable actions and finalization. First discovery and
  authorized zero-return continuation do not consume model decision turns.
- ACTIVE: Harness reconciles proposed actions with registry/budgets before issuing
  choices, clips program-owned read lengths and feeds cached observations back to
  the policy without registering new physical reads.
- ACTIVE: normal and forced finalization share the ResearchFinishDraft schema and
  `research/finalize` prompt. Final responses never re-enter action selection.
- TEMPORARY: Researcher enables repeat_error_limit=2 for stage-local decision errors.
  One feedback opportunity precedes tool-free partial finalization on recurrence.
  This does not classify zero results as errors or validate semantic progress;
  the generic Harness default remains disabled, pending further evaluation.
