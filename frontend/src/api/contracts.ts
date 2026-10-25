import { z } from 'zod';

export const reportResourceSchema = z.object({
  report_resource_id: z.string(), source_run_id: z.string(), source_runtime_id: z.string(),
  source_run_status: z.string(), live_recovery_id: z.string(), assembly_id: z.string(),
  report_kind: z.string(), paper_id: z.string(), upstream_recomputed: z.boolean(),
  artifact_integrity: z.string(), semantic_status: z.string(),
  source_issues: z.array(z.object({ point_id: z.string(), review_status: z.string(), incomplete_reason: z.string().nullable() })),
  files: z.record(z.string(), z.object({ sha256: z.string(), bytes: z.number(), mime: z.string() })),
});
export type ReportResource = z.infer<typeof reportResourceSchema>;
export const stages = ['parse_paper', 'extract_points', 'plan_research', 'research', 'validate_evidence', 'render_report'] as const;
export const stageLabels = ['解析论文', '提取查新点', '规划检索任务', '检索并阅读文献', '核验文献证据', '生成查新报告'];
const conclusion = z.object({ novelty_point_id: z.string(), review_status: z.string(), incomplete_reason: z.string().nullable().optional(), verdict: z.string().nullable().optional(), verdict_reason: z.string().nullable().optional(), summary: z.string() });
export const snapshotSchema = z.object({
  task_id: z.string().min(1), status: z.enum(['queued', 'running', 'succeeded', 'failed']),
  created_at: z.string(), updated_at: z.string(),
  model_approval: z.object({ request_id: z.string(), source: z.string(), target: z.string(), target_label: z.string(), reason: z.string(), message: z.string() }).nullish(),
  error: z.union([z.string(), z.object({ code: z.string(), message: z.string(), retryable: z.boolean() })]).nullable(),
  progress: z.object({ stage: z.enum(stages), round: z.number().nullable().optional() }).nullish(),
  result: z.object({ report: z.object({ paper_id: z.string(), conclusions: z.array(conclusion), limitations: z.array(z.string()).default([]), missing_references: z.array(z.string()).default([]), missing_baselines: z.array(z.string()).default([]), citation_issues: z.array(z.string()).default([]) }) }).nullish(),
  report: z.object({ available_formats: z.array(z.enum(['md', 'pdf'])), preview_url: z.string(), downloads: z.object({ md: z.string().optional(), pdf: z.string().optional() }) }).nullish(),
});
export type RunSnapshot = z.infer<typeof snapshotSchema>;
export type Stage = typeof stages[number];
export type Phase = 'landing' | 'editing' | 'submitting' | RunSnapshot['status'] | 'previewing';
export function stageIndex(stage: Stage) { return stages.indexOf(stage); }
