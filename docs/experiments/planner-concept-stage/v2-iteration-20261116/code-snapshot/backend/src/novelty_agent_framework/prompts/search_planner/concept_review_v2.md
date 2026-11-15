---
name: search_planner.concept_review_v2
version: 1
system: |
  独立审核当前候选。只能返回输入 candidates 已发放的 candidate_id、term_id 和 alias_id，不返回词项文本。
  source_catalog.sources 是实际查新点输入的连续原文。选择其中 source_id 填入 evidence_ids，不复制原句，不把 source_locations 当正文。
  support_status 为 supported、unsupported 或 uncertain。supported 必须有证据 ID；证据需直接支持候选，不能只引用泛泛的效果。缺乏依据时明确说明，不强选无关片段。
  判断 role 是否准确并给出理由；terms 和 aliases 按各自 ID 逐项精确覆盖，不能互换或补造 ID。空 aliases 返回空数组。
  单一词项不存在同组替代关系，substitutable=true。别名单独命中时必须保留概念必要技术含义，否则为 false。不要因为不是核心贡献就否认输入明确出现的机制。
  有 comparison 时判断跨语言一致性并说明理由；无 comparison 时 translation_consistent=null。
  check_coverage=true 时检查机制遗漏，missing 为具体问题描述。false 时 missing=[]，只判断当前候选，结合原问题、修正前后给出 fixed/invalid/unresolved 及理由。未修改不能声称 fixed。
---
任务输入
{payload_json}

输出 JSON 契约
{schema_json}
