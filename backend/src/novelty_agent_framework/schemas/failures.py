"""Versioned business failures and bounded recovery advice, independent of agents.

Codes describe observed facts. Advice never grants tool/network permission and
never turns missing coverage into a negative scientific finding.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FailureCode(StrEnum):
    PROVIDER_AUTHENTICATION = "provider.authentication"
    PROVIDER_AUTHORIZATION = "provider.authorization"
    PROVIDER_RATE_LIMIT = "provider.rate_limit"
    PROVIDER_NETWORK = "provider.network"
    PROVIDER_SERVICE = "provider.service"
    PROVIDER_PROTOCOL = "provider.protocol"
    PROVIDER_RESOURCE_MISSING = "provider.resource_missing"
    PROVIDER_UNKNOWN = "provider.unknown"
    MATERIAL_UNAVAILABLE = "material.unavailable"
    MATERIAL_INTEGRITY = "material.integrity"
    TOOL_SCOPE = "tool.scope"
    TOOL_ARGUMENTS = "tool.arguments"
    TOOL_UNAVAILABLE = "tool.unavailable"
    MODEL_TIMEOUT = "model.timeout"
    MODEL_TRANSPORT = "model.transport"
    MODEL_CONTEXT_LIMIT = "model.context_limit"
    MODEL_CONTEXT_UNAVAILABLE = "model.context_unavailable"
    MODEL_OUTPUT_SCHEMA = "model.output_schema"
    MODEL_OUTPUT_REFERENCE = "model.output_reference"
    MODEL_USAGE_UNAVAILABLE = "model.usage_unavailable"
    BUDGET_EXHAUSTED = "harness.budget_exhausted"
    COVERAGE_NOT_EXECUTED = "coverage.not_executed"
    COVERAGE_NO_MATCH = "coverage.no_match"
    EVIDENCE_MISSING = "evidence.missing"
    REVIEW_MISSING_FEATURES = "review.missing_features"
    REVIEW_SUMMARY_FAILED = "review.summary_failed"
    UNKNOWN = "execution.unknown"

    # Specific observed causes; legacy family codes remain readable.
    PROVIDER_CREDENTIAL_MISSING = "provider.credentials.missing"
    PROVIDER_TIMEOUT = "provider.request.timeout"
    PROVIDER_CONNECTION = "provider.request.connection_failed"
    PROVIDER_RESULT_INVALID = "provider.response.contract_invalid"
    READER_FILE_MISSING = "reader.file.missing"
    READER_ACCESS_DENIED = "reader.file.access_denied"
    READER_MANIFEST_INVALID = "reader.manifest.invalid"
    READER_DECODE_FAILED = "reader.content.decode_failed"
    READER_ARTIFACT_UNREGISTERED = "reader.artifact.unregistered"
    READER_VERIFICATION_FAILED = "reader.artifact.verification_failed"
    TOOL_NOT_REGISTERED = "tool.dispatch.not_registered"
    TOOL_ARGUMENT_SCHEMA = "tool.arguments.schema_invalid"
    TOOL_SCOPE_DENIED = "tool.scope.denied"
    RECOVERY_SCOPE_CHANGED = "recovery.scope.changed"
    HARNESS_NATIVE_CALL = "harness.decision.native_call_forbidden"
    HARNESS_DECISION_SCHEMA = "harness.decision.schema_invalid"
    HARNESS_OPTION_LABEL = "harness.decision.label_instead_of_id"
    HARNESS_OPTION_STALE = "harness.decision.stale_option"
    HARNESS_OPTION_UNKNOWN = "harness.decision.unknown_option"
    HARNESS_EARLY_FINISH = "harness.stage.premature_finish"
    HARNESS_READ_REQUIRED = "harness.stage.read_required"
    BUDGET_TURNS = "harness.budget.turns_exhausted"
    BUDGET_TOOLS = "harness.budget.tool_calls_exhausted"
    BUDGET_PER_TOOL = "harness.budget.per_tool_exhausted"
    BUDGET_READ_CHARS = "harness.budget.read_chars_exhausted"
    RETRIEVAL_BUDGET = "retrieval.budget.requests_exhausted"
    RETRIEVAL_SKIPPED = "retrieval.query.skipped_after_failure"
    BUILDER_QUOTE_TARGET = "builder.quote.from_target_input"
    BUILDER_QUOTE_TEXT_MISMATCH = "builder.quote.near_match"
    BUILDER_QUOTE_UNGROUNDED = "builder.quote.unmatched"
    BUILDER_QUOTE_GENERATED = "builder.quote.generated_summary"
    BUILDER_WEB_SUPPLEMENT = "builder.source.web_supplement"
    BUILDER_CROSS_WORK = "builder.source.cross_work"
    BUILDER_AMBIGUOUS = "builder.source.ambiguous"
    BUILDER_DUPLICATE = "builder.card.duplicate_work"
    BUILDER_WORK_MISSING = "builder.work.missing"
    BUILDER_ARTIFACT_MISSING = "builder.artifact.missing"
    BUILDER_WORK_MISMATCH = "builder.artifact.work_mismatch"
    BUILDER_RECORD_MISSING = "builder.source_record.missing"
    REVIEW_POINT_SCOPE = "reviewer.reference.point_out_of_scope"
    REVIEW_EVIDENCE_DUPLICATE = "reviewer.reference.duplicate_evidence"
    REVIEW_EVIDENCE_COLLISION = "reviewer.reference.evidence_id_collision"
    REVIEW_EVIDENCE_SCOPE = "reviewer.reference.evidence_out_of_scope"
    REVIEW_CARD_UNKNOWN = "reviewer.reference.unknown_card"
    REVIEW_EVIDENCE_UNKNOWN = "reviewer.reference.unknown_evidence"
    REVIEW_WORK_MISMATCH = "reviewer.reference.work_mismatch"
    REVIEW_CARD_UNLINKED = "reviewer.reference.card_evidence_unlinked"
    REVIEW_FEATURE_UNKNOWN = "reviewer.comparison.unknown_feature"
    REVIEW_WORK_SCOPE = "reviewer.comparison.work_out_of_scope"
    REVIEW_SUPPORT_MISSING = "reviewer.comparison.evidence_missing"
    REVIEW_SUPPORT_WRONG_WORK = "reviewer.comparison.evidence_binding_invalid"
    GATE_REVIEW_MISSING = "gate_a.review.missing"
    GATE_REVIEW_DUPLICATE = "gate_a.review.duplicate"
    GATE_CARDS_MISSING = "gate_a.review.accepted_evidence_missing"
    GATE_INCOMPLETE_CONFLICT = "gate_a.review.status_conflict"
    GATE_BLOCKING_FAILURE = "gate_a.review.unresolved_failure"
    GATE_COVERAGE_INCOMPLETE = "gate_a.verdict.coverage_incomplete"
    GATE_READ_UNVERIFIED = "gate_a.read.unverified"
    GATE_INCREMENT_MISSING = "gate_a.read.evidence_unregistered"

    READER_HASH_MISMATCH = "reader.artifact.hash_mismatch"
    READER_PATH_ESCAPE = "reader.artifact.path_escape"
    FULLTEXT_UNSUPPORTED = "retrieval.fulltext.unsupported"
    FULLTEXT_EMPTY = "retrieval.fulltext.empty"
    FULLTEXT_FAILED = "retrieval.fulltext.acquisition_failed"
    MODEL_JSON_INVALID = "model.output.json_invalid"
    MODEL_SCHEMA_INVALID = "model.output.schema_invalid"
    MODEL_CALL_BUDGET = "model.budget.calls_exhausted"
    SUMMARY_CHECKPOINT_WRITE = "reviewer.checkpoint.write_failed"
    SUMMARY_CHECKPOINT_INVALID = "reviewer.checkpoint.invalid"
    SUMMARY_RESULT_WRITE = "reviewer.summary.result_write_failed"
    CORRECTION_NATIVE_CALL = "builder.correction.native_call_forbidden"
    CORRECTION_CARD_COUNT = "builder.correction.too_many_cards"
    HARNESS_MULTIPLE_CALLS = "harness.decision.multiple_tool_calls"

    MODEL_CREDENTIAL_MISSING = "model.config.credential_missing"
    MODEL_ALIAS_UNKNOWN = "model.config.alias_unknown"
    MODEL_PROVIDER_UNSUPPORTED = "model.config.provider_unsupported"
    MODEL_RESPONSE_SHAPE = "model.response.envelope_invalid"
    MODEL_CONTENT_TYPE = "model.response.content_type_invalid"
    MODEL_TOOL_LIST = "model.response.tool_calls_not_array"
    MODEL_TOOL_ITEM = "model.response.tool_call_not_object"
    MODEL_TOOL_ID = "model.response.tool_call_id_missing"
    MODEL_TOOL_FUNCTION = "model.response.function_missing"
    MODEL_TOOL_NAME = "model.response.function_name_missing"
    MODEL_TOOL_ARGS_TYPE = "model.response.arguments_not_string"
    MODEL_TOOL_ARGS_JSON = "model.response.arguments_json_invalid"
    MODEL_TOOL_ARGS_OBJECT = "model.response.arguments_not_object"
    MODEL_HTTP_AUTH = "model.request.authentication_failed"
    MODEL_HTTP_DENIED = "model.request.authorization_failed"
    MODEL_HTTP_RATE = "model.request.rate_limited"
    MODEL_HTTP_SERVER = "model.request.server_error"
    MODEL_HTTP_REJECTED = "model.request.http_rejected"


class RecoveryAction(StrEnum):
    RETRY_REQUEST = "retry_request"
    REPAIR_ARGUMENTS = "repair_arguments"
    REPAIR_OUTPUT = "repair_output"
    CHANGE_PROVIDER = "change_provider"
    FETCH_FULLTEXT = "fetch_fulltext"
    READ_ARTIFACT = "read_artifact"
    REPLAN_QUERY = "replan_query"
    RESEARCH_GAP = "research_gap"
    RESUME_REVIEW = "resume_review"
    RESTORE_CHECKPOINT = "restore_checkpoint"
    MANUAL = "manual"
    STOP = "stop"
    NONE = "none"


class FailureContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)


class FailureScope(FailureContract):
    node: str | None = None
    paper_id: str | None = None
    run_id: str | None = None
    point_id: str | None = None
    task_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    card_id: str | None = None
    artifact_id: str | None = None
    provider: str | None = None
    feature_ids: list[str] = Field(default_factory=list)


class RetryAdvice(FailureContract):
    allowed: bool = False
    max_additional_attempts: int = Field(default=0, ge=0, le=2)
    requires_input_change: bool = False
    retry_after_seconds: float | None = Field(default=None, ge=0, le=3600)

    @model_validator(mode="after")
    def bounded(self):
        if self.allowed != (self.max_additional_attempts > 0):
            raise ValueError("retry allowance and additional attempt limit must agree")
        return self


@dataclass(frozen=True)
class FailureDefinition:
    layer: str
    category: str
    actions: tuple[RecoveryAction, ...]
    retry_limit: int = 0
    input_change: bool = False
    effect: str = "limits_coverage"


A = RecoveryAction
D = FailureDefinition
FAILURE_CATALOG = MappingProxyType({
    FailureCode.PROVIDER_AUTHENTICATION: D("provider", "authentication", (A.CHANGE_PROVIDER, A.MANUAL)),
    FailureCode.PROVIDER_AUTHORIZATION: D("provider", "authorization", (A.CHANGE_PROVIDER, A.MANUAL)),
    FailureCode.PROVIDER_RATE_LIMIT: D("provider", "rate_limit", (A.RETRY_REQUEST, A.CHANGE_PROVIDER, A.STOP), 1),
    FailureCode.PROVIDER_NETWORK: D("provider", "network", (A.RETRY_REQUEST, A.CHANGE_PROVIDER, A.STOP), 1),
    FailureCode.PROVIDER_SERVICE: D("provider", "service", (A.RETRY_REQUEST, A.CHANGE_PROVIDER, A.STOP), 1),
    FailureCode.PROVIDER_PROTOCOL: D("provider", "protocol", (A.CHANGE_PROVIDER, A.MANUAL)),
    FailureCode.PROVIDER_RESOURCE_MISSING: D("provider", "resource", (A.CHANGE_PROVIDER, A.MANUAL)),
    FailureCode.PROVIDER_UNKNOWN: D("provider", "unknown", (A.MANUAL, A.STOP)),
    FailureCode.MATERIAL_UNAVAILABLE: D("reader", "resource", (A.FETCH_FULLTEXT, A.MANUAL)),
    FailureCode.MATERIAL_INTEGRITY: D("reader", "integrity", (A.MANUAL, A.STOP), effect="blocks_card"),
    FailureCode.TOOL_SCOPE: D("harness", "policy", (A.REPAIR_ARGUMENTS, A.STOP), 1, True, "blocks_card"),
    FailureCode.TOOL_ARGUMENTS: D("harness", "arguments", (A.REPAIR_ARGUMENTS, A.STOP), 1, True),
    FailureCode.TOOL_UNAVAILABLE: D("harness", "policy", (A.MANUAL, A.STOP)),
    FailureCode.MODEL_TIMEOUT: D("model", "network", (A.RESTORE_CHECKPOINT, A.RETRY_REQUEST, A.STOP), 1, effect="blocks_point"),
    FailureCode.MODEL_TRANSPORT: D("model", "network", (A.RESTORE_CHECKPOINT, A.RETRY_REQUEST, A.STOP), 1, effect="blocks_point"),
    FailureCode.MODEL_CONTEXT_LIMIT: D("model", "context", (A.RESTORE_CHECKPOINT, A.STOP), effect="blocks_point"),
    FailureCode.MODEL_CONTEXT_UNAVAILABLE: D("model", "context", (A.MANUAL, A.STOP), effect="blocks_point"),
    FailureCode.MODEL_OUTPUT_SCHEMA: D("model", "contract", (A.REPAIR_OUTPUT, A.STOP), 1, True, "blocks_point"),
    FailureCode.MODEL_OUTPUT_REFERENCE: D("reviewer", "integrity", (A.REPAIR_OUTPUT, A.STOP), 1, True, "blocks_point"),
    FailureCode.MODEL_USAGE_UNAVAILABLE: D("runtime", "unknown", (A.NONE,), effect="accounting_unknown"),
    FailureCode.BUDGET_EXHAUSTED: D("harness", "budget", (A.STOP,), effect="blocks_point"),
    FailureCode.COVERAGE_NOT_EXECUTED: D("researcher", "coverage", (A.RESEARCH_GAP, A.STOP)),
    FailureCode.COVERAGE_NO_MATCH: D("provider", "coverage", (A.REPLAN_QUERY, A.STOP)),
    FailureCode.EVIDENCE_MISSING: D("researcher", "coverage", (A.RESEARCH_GAP, A.STOP)),
    FailureCode.REVIEW_MISSING_FEATURES: D("reviewer", "semantic", (A.READ_ARTIFACT, A.FETCH_FULLTEXT, A.RESEARCH_GAP, A.STOP), effect="blocks_point"),
    FailureCode.REVIEW_SUMMARY_FAILED: D("reviewer", "contract", (A.RESUME_REVIEW, A.STOP), 1, effect="blocks_point"),
    FailureCode.UNKNOWN: D("runtime", "unknown", (A.MANUAL, A.STOP), effect="unknown"),
})


# Leaf advice inherits the existing family policy; classification is independent
# of retry policy. Unknown causes must remain explicit family fallbacks.
FAILURE_FAMILIES = MappingProxyType({
    FailureCode.MODEL_CREDENTIAL_MISSING: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_ALIAS_UNKNOWN: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_PROVIDER_UNSUPPORTED: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_RESPONSE_SHAPE: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_CONTENT_TYPE: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_TOOL_LIST: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_TOOL_ITEM: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_TOOL_ID: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_TOOL_FUNCTION: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_TOOL_NAME: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_TOOL_ARGS_TYPE: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_TOOL_ARGS_JSON: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_TOOL_ARGS_OBJECT: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_HTTP_AUTH: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_HTTP_DENIED: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_HTTP_RATE: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_HTTP_SERVER: FailureCode.MODEL_TRANSPORT,
    FailureCode.MODEL_HTTP_REJECTED: FailureCode.MODEL_TRANSPORT,

    FailureCode.READER_HASH_MISMATCH: FailureCode.MATERIAL_INTEGRITY,
    FailureCode.READER_PATH_ESCAPE: FailureCode.MATERIAL_INTEGRITY,
    FailureCode.FULLTEXT_UNSUPPORTED: FailureCode.MATERIAL_UNAVAILABLE,
    FailureCode.FULLTEXT_EMPTY: FailureCode.MATERIAL_UNAVAILABLE,
    FailureCode.FULLTEXT_FAILED: FailureCode.MATERIAL_UNAVAILABLE,
    FailureCode.MODEL_JSON_INVALID: FailureCode.MODEL_OUTPUT_SCHEMA,
    FailureCode.MODEL_SCHEMA_INVALID: FailureCode.MODEL_OUTPUT_SCHEMA,
    FailureCode.MODEL_CALL_BUDGET: FailureCode.BUDGET_EXHAUSTED,
    FailureCode.SUMMARY_CHECKPOINT_WRITE: FailureCode.REVIEW_SUMMARY_FAILED,
    FailureCode.SUMMARY_CHECKPOINT_INVALID: FailureCode.REVIEW_SUMMARY_FAILED,
    FailureCode.SUMMARY_RESULT_WRITE: FailureCode.REVIEW_SUMMARY_FAILED,
    FailureCode.CORRECTION_NATIVE_CALL: FailureCode.MODEL_OUTPUT_SCHEMA,
    FailureCode.CORRECTION_CARD_COUNT: FailureCode.MODEL_OUTPUT_SCHEMA,
    FailureCode.HARNESS_MULTIPLE_CALLS: FailureCode.TOOL_ARGUMENTS,

    FailureCode.PROVIDER_CREDENTIAL_MISSING: FailureCode.PROVIDER_AUTHENTICATION,
    FailureCode.PROVIDER_TIMEOUT: FailureCode.PROVIDER_NETWORK,
    FailureCode.PROVIDER_CONNECTION: FailureCode.PROVIDER_NETWORK,
    FailureCode.PROVIDER_RESULT_INVALID: FailureCode.PROVIDER_PROTOCOL,
    FailureCode.READER_FILE_MISSING: FailureCode.MATERIAL_UNAVAILABLE,
    FailureCode.READER_ACCESS_DENIED: FailureCode.MATERIAL_UNAVAILABLE,
    FailureCode.READER_MANIFEST_INVALID: FailureCode.MATERIAL_INTEGRITY,
    FailureCode.READER_DECODE_FAILED: FailureCode.MATERIAL_INTEGRITY,
    FailureCode.READER_ARTIFACT_UNREGISTERED: FailureCode.MATERIAL_UNAVAILABLE,
    FailureCode.READER_VERIFICATION_FAILED: FailureCode.MATERIAL_INTEGRITY,
    FailureCode.TOOL_NOT_REGISTERED: FailureCode.TOOL_UNAVAILABLE,
    FailureCode.TOOL_ARGUMENT_SCHEMA: FailureCode.TOOL_ARGUMENTS,
    FailureCode.TOOL_SCOPE_DENIED: FailureCode.TOOL_SCOPE,
    FailureCode.RECOVERY_SCOPE_CHANGED: FailureCode.TOOL_SCOPE,
    FailureCode.HARNESS_NATIVE_CALL: FailureCode.TOOL_ARGUMENTS,
    FailureCode.HARNESS_DECISION_SCHEMA: FailureCode.TOOL_ARGUMENTS,
    FailureCode.HARNESS_OPTION_LABEL: FailureCode.TOOL_ARGUMENTS,
    FailureCode.HARNESS_OPTION_STALE: FailureCode.TOOL_ARGUMENTS,
    FailureCode.HARNESS_OPTION_UNKNOWN: FailureCode.TOOL_ARGUMENTS,
    FailureCode.HARNESS_EARLY_FINISH: FailureCode.TOOL_SCOPE,
    FailureCode.HARNESS_READ_REQUIRED: FailureCode.TOOL_SCOPE,
    FailureCode.BUDGET_TURNS: FailureCode.BUDGET_EXHAUSTED,
    FailureCode.BUDGET_TOOLS: FailureCode.BUDGET_EXHAUSTED,
    FailureCode.BUDGET_PER_TOOL: FailureCode.BUDGET_EXHAUSTED,
    FailureCode.BUDGET_READ_CHARS: FailureCode.BUDGET_EXHAUSTED,
    FailureCode.RETRIEVAL_BUDGET: FailureCode.BUDGET_EXHAUSTED,
    FailureCode.RETRIEVAL_SKIPPED: FailureCode.COVERAGE_NOT_EXECUTED,
    FailureCode.BUILDER_QUOTE_TARGET: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_QUOTE_TEXT_MISMATCH: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_QUOTE_UNGROUNDED: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_QUOTE_GENERATED: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_WEB_SUPPLEMENT: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_CROSS_WORK: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_AMBIGUOUS: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_DUPLICATE: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_WORK_MISSING: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_ARTIFACT_MISSING: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_WORK_MISMATCH: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.BUILDER_RECORD_MISSING: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_POINT_SCOPE: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_EVIDENCE_DUPLICATE: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_EVIDENCE_COLLISION: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_EVIDENCE_SCOPE: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_CARD_UNKNOWN: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_EVIDENCE_UNKNOWN: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_WORK_MISMATCH: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_CARD_UNLINKED: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_FEATURE_UNKNOWN: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_WORK_SCOPE: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_SUPPORT_MISSING: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.REVIEW_SUPPORT_WRONG_WORK: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.GATE_REVIEW_MISSING: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.GATE_REVIEW_DUPLICATE: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.GATE_CARDS_MISSING: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.GATE_INCOMPLETE_CONFLICT: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.GATE_BLOCKING_FAILURE: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.GATE_COVERAGE_INCOMPLETE: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.GATE_READ_UNVERIFIED: FailureCode.MODEL_OUTPUT_REFERENCE,
    FailureCode.GATE_INCREMENT_MISSING: FailureCode.MODEL_OUTPUT_REFERENCE,
})
FAILURE_DESCRIPTIONS = MappingProxyType({
    FailureCode.MODEL_CREDENTIAL_MISSING: '模型客户端缺少配置凭据',
    FailureCode.MODEL_ALIAS_UNKNOWN: '模型别名未注册',
    FailureCode.MODEL_PROVIDER_UNSUPPORTED: '客户端不支持配置的供应商',
    FailureCode.MODEL_RESPONSE_SHAPE: '响应不符合 chat completions 的 choices/message 契约',
    FailureCode.MODEL_CONTENT_TYPE: '响应 content 不是字符串或 null',
    FailureCode.MODEL_TOOL_LIST: '响应 tool_calls 不是数组',
    FailureCode.MODEL_TOOL_ITEM: '单项 tool_call 不是对象',
    FailureCode.MODEL_TOOL_ID: 'tool_call 缺少有效 id',
    FailureCode.MODEL_TOOL_FUNCTION: 'tool_call 缺少 function 对象',
    FailureCode.MODEL_TOOL_NAME: 'function 缺少有效 name',
    FailureCode.MODEL_TOOL_ARGS_TYPE: 'function.arguments 不是字符串',
    FailureCode.MODEL_TOOL_ARGS_JSON: 'function.arguments 字符串不是合法 JSON',
    FailureCode.MODEL_TOOL_ARGS_OBJECT: 'function.arguments 解码后不是对象',
    FailureCode.MODEL_HTTP_AUTH: '模型接口返回 HTTP 401',
    FailureCode.MODEL_HTTP_DENIED: '模型接口返回 HTTP 403',
    FailureCode.MODEL_HTTP_RATE: '模型接口返回 HTTP 429',
    FailureCode.MODEL_HTTP_SERVER: '模型接口返回 HTTP 5xx',
    FailureCode.MODEL_HTTP_REJECTED: '模型接口返回其他非成功 HTTP 状态',

    FailureCode.READER_HASH_MISMATCH: '落盘文件的 SHA-256 与索引不一致',
    FailureCode.READER_PATH_ESCAPE: '材料路径越过允许的目录',
    FailureCode.FULLTEXT_UNSUPPORTED: '来源没有全文获取能力',
    FailureCode.FULLTEXT_EMPTY: '全文调用成功返回但没有可读内容',
    FailureCode.FULLTEXT_FAILED: '全文获取抛出异常，具体原因见关联事件',
    FailureCode.MODEL_JSON_INVALID: '模型输出不是合法 JSON',
    FailureCode.MODEL_SCHEMA_INVALID: 'JSON 已解析，但字段或类型违反数据契约',
    FailureCode.MODEL_CALL_BUDGET: '模型调用预算耗尽',
    FailureCode.SUMMARY_CHECKPOINT_WRITE: '保存汇总检查点失败',
    FailureCode.SUMMARY_CHECKPOINT_INVALID: '汇总检查点输入无法核验',
    FailureCode.SUMMARY_RESULT_WRITE: '保存汇总尝试结果失败',
    FailureCode.CORRECTION_NATIVE_CALL: '纠错返回了禁用的工具调用',
    FailureCode.CORRECTION_CARD_COUNT: '纠错输出卡数多于已读文献数',
    FailureCode.HARNESS_MULTIPLE_CALLS: '串行模式返回多个工具调用，只保留首个',

    FailureCode.PROVIDER_CREDENTIAL_MISSING: '配置中缺少凭据',
    FailureCode.PROVIDER_TIMEOUT: '请求明确抛出超时异常',
    FailureCode.PROVIDER_CONNECTION: '请求明确抛出连接异常',
    FailureCode.PROVIDER_RESULT_INVALID: '返回数据不能满足解析契约',
    FailureCode.READER_FILE_MISSING: '文件不存在',
    FailureCode.READER_ACCESS_DENIED: '文件访问被拒绝',
    FailureCode.READER_MANIFEST_INVALID: '索引文件无法解析或不符合契约',
    FailureCode.READER_DECODE_FAILED: '内容无法按 UTF-8 解码',
    FailureCode.READER_ARTIFACT_UNREGISTERED: '绑定的 artifact 不在索引中',
    FailureCode.READER_VERIFICATION_FAILED: '文件校验失败，尚未细分校验器原因',
    FailureCode.TOOL_NOT_REGISTERED: '工具未注册',
    FailureCode.TOOL_ARGUMENT_SCHEMA: '调用参数未通过数据契约',
    FailureCode.TOOL_SCOPE_DENIED: '工具拒绝当前任务范围',
    FailureCode.RECOVERY_SCOPE_CHANGED: '恢复调用改变了冻结的原始范围',
    FailureCode.HARNESS_NATIVE_CALL: '选项模式下模型仍返回原生工具调用',
    FailureCode.HARNESS_DECISION_SCHEMA: '决策 JSON 或字段结构不合法',
    FailureCode.HARNESS_OPTION_LABEL: '返回了当前菜单的标签而非编号',
    FailureCode.HARNESS_OPTION_STALE: '返回了曾经发放但当前已经失效的编号',
    FailureCode.HARNESS_OPTION_UNKNOWN: '返回了未发放的编号',
    FailureCode.HARNESS_EARLY_FINISH: '仍有必需阶段时提前提交草稿',
    FailureCode.HARNESS_READ_REQUIRED: '检索后未读取必需材料',
    FailureCode.BUDGET_TURNS: '模型探索轮次耗尽',
    FailureCode.BUDGET_TOOLS: '总工具调用次数耗尽',
    FailureCode.BUDGET_PER_TOOL: '单工具调用次数耗尽',
    FailureCode.BUDGET_READ_CHARS: '读取字符预算不足或耗尽',
    FailureCode.RETRIEVAL_BUDGET: '检索请求预算耗尽',
    FailureCode.RETRIEVAL_SKIPPED: '前置检索失败后本查询未执行',
    FailureCode.BUILDER_QUOTE_TARGET: '无法匹配参考文献，但匹配了输入查新点内容',
    FailureCode.BUILDER_QUOTE_TEXT_MISMATCH: '引文仅与已读材料近似匹配，字符存在差异；不推断抄写动机',
    FailureCode.BUILDER_QUOTE_UNGROUNDED: '引文不在已读的合法参考材料中，具体来源未知',
    FailureCode.BUILDER_QUOTE_GENERATED: '引文仅命中了模型生成的摘要',
    FailureCode.BUILDER_WEB_SUPPLEMENT: '补充网页不能用作论文证据',
    FailureCode.BUILDER_CROSS_WORK: '引文不能共同绑定到一篇文献，且无法安全拆卡',
    FailureCode.BUILDER_AMBIGUOUS: '引文可同时绑定多个文献，无法唯一溯源',
    FailureCode.BUILDER_DUPLICATE: '同一文献重复产卡',
    FailureCode.BUILDER_WORK_MISSING: '索引缺少 Work',
    FailureCode.BUILDER_ARTIFACT_MISSING: '索引缺少 Artifact',
    FailureCode.BUILDER_WORK_MISMATCH: 'Artifact 与读取结果的 Work 不一致',
    FailureCode.BUILDER_RECORD_MISSING: 'Artifact 引用了不存在的 SourceRecord',
    FailureCode.REVIEW_POINT_SCOPE: 'Review 指向其他查新点',
    FailureCode.REVIEW_EVIDENCE_DUPLICATE: '增量证据编号重复',
    FailureCode.REVIEW_EVIDENCE_COLLISION: '增量证据编号与原始证据冲突',
    FailureCode.REVIEW_EVIDENCE_SCOPE: '增量证据越过 Card 或 Work 范围',
    FailureCode.REVIEW_CARD_UNKNOWN: '引用未知 Card',
    FailureCode.REVIEW_EVIDENCE_UNKNOWN: '引用未知 Evidence',
    FailureCode.REVIEW_WORK_MISMATCH: 'Work 与引用证据不一致',
    FailureCode.REVIEW_CARD_UNLINKED: 'Card 与引用证据没有关联',
    FailureCode.REVIEW_FEATURE_UNKNOWN: '比较了未定义的特征',
    FailureCode.REVIEW_WORK_SCOPE: '比较了范围外的 Work',
    FailureCode.REVIEW_SUPPORT_MISSING: '明确比较结论没有证据',
    FailureCode.REVIEW_SUPPORT_WRONG_WORK: '比较证据缺失或属于其他 Work',
    FailureCode.GATE_REVIEW_MISSING: '查新点没有 Review',
    FailureCode.GATE_REVIEW_DUPLICATE: '查新点有重复 Review',
    FailureCode.GATE_CARDS_MISSING: '已完成的审阅没有合法 Card 或 Evidence',
    FailureCode.GATE_INCOMPLETE_CONFLICT: '已完成状态仍带未完成原因',
    FailureCode.GATE_BLOCKING_FAILURE: '已完成状态仍带阻断执行错误',
    FailureCode.GATE_COVERAGE_INCOMPLETE: '不新颖结论缺少单篇文献的完整特征支持',
    FailureCode.GATE_READ_UNVERIFIED: '阅读引用缺少核验通过的 Reader 记录',
    FailureCode.GATE_INCREMENT_MISSING: '阅读引用缺少已登记的增量证据',
})
FAILURE_CATALOG = MappingProxyType({**FAILURE_CATALOG, **{
    code: FailureDefinition(code.value.split(".")[0], code.value.split(".")[1],
        definition.actions, definition.retry_limit, definition.input_change, definition.effect)
    for code, parent in FAILURE_FAMILIES.items()
    for definition in [FAILURE_CATALOG[parent]]
}})

def failure_family(code: FailureCode | str) -> FailureCode:
    code = FailureCode(code)
    return FAILURE_FAMILIES.get(code, code)


class CodedFailure(ValueError):
    """A deterministic boundary assertion carrying a stable machine code."""
    def __init__(self, code: FailureCode, message: str):
        super().__init__(message)
        self.code = code


class FailureEvent(FailureContract):
    schema_version: Literal[1] = 1
    event_id: str = Field(pattern=r"^failure_[0-9a-f]{24}$")
    code: FailureCode
    layer: str
    category: str
    scope: FailureScope
    execution_status: Literal["incomplete", "partial"] = "incomplete"
    semantic_status: Literal["not_adjudicated"] = "not_adjudicated"
    retry: RetryAdvice
    recovery: RecoveryAction
    conclusion_effect: Literal["limits_coverage", "blocks_card", "blocks_point", "blocks_run", "accounting_unknown", "unknown"]
    evidence_refs: list[str] = Field(default_factory=list)
    cause_event_ids: list[str] = Field(default_factory=list)
    message: str = ""

    @model_validator(mode="after")
    def catalog_consistent(self):
        definition = FAILURE_CATALOG[self.code]
        if (self.layer, self.category) != (definition.layer, definition.category):
            raise ValueError("failure classification must match the code catalog")
        if self.recovery not in definition.actions:
            raise ValueError("recovery action is not allowed for this failure code")
        if self.retry.max_additional_attempts > definition.retry_limit:
            raise ValueError("retry exceeds the code's bounded policy")
        if self.retry.allowed and definition.input_change and not self.retry.requires_input_change:
            raise ValueError("this recovery cannot repeat the same invalid input")
        if self.event_id in self.cause_event_ids or len(set(self.cause_event_ids)) != len(self.cause_event_ids):
            raise ValueError("failure causes cannot contain self or duplicate references")
        return self


def make_failure(code: FailureCode | str, *, scope: FailureScope | dict,
                 message: str = "", evidence_refs: list[str] | None = None,
                 cause_event_ids: list[str] | None = None, occurrence_id: str = "") -> FailureEvent:
    code = FailureCode(code)
    scope = FailureScope.model_validate(scope)
    definition = FAILURE_CATALOG[code]
    identity_scope = scope.model_dump(mode="json")
    if scope.node is None:
        identity_scope.pop("node")  # Preserve historical event IDs.
    identity = json.dumps([code.value, identity_scope, occurrence_id], sort_keys=True, ensure_ascii=False)
    # Callers supply sanitized constant descriptions, never raw credential-bearing errors.
    return FailureEvent(event_id="failure_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
        code=code, layer=definition.layer, category=definition.category, scope=scope,
        retry=RetryAdvice(allowed=definition.retry_limit > 0,
            max_additional_attempts=definition.retry_limit, requires_input_change=definition.input_change),
        recovery=definition.actions[0], conclusion_effect=definition.effect,
        message=message, evidence_refs=evidence_refs or [], cause_event_ids=cause_event_ids or [])


class RecoveryArtifact(FailureContract):
    namespace: Literal["research_reference", "subject_reference"] = "research_reference"
    artifact_id: str = Field(min_length=1)


class RecoveryDecision(FailureContract):
    """Runtime trigger facts plus model-authored planning; STOP remains runtime-owned."""
    point_id: str = Field(min_length=1)
    action: RecoveryAction
    reason: str = Field(min_length=1)
    cause_event_ids: list[str] = Field(default_factory=list)
    source_id: str | None = None
    artifacts: list[RecoveryArtifact] = Field(default_factory=list)
    source_record_ids: list[str] = Field(default_factory=list)
    missing_feature_ids: list[str] = Field(default_factory=list)
    missing_aspects: list[str] = Field(default_factory=list)
    review_reason: str | None = None
    suggested_focus: list[str] = Field(default_factory=list)
    max_additional_attempts: int = Field(default=1, ge=0, le=1)
    trigger_codes: list[Literal["evidence.missing", "review.missing_features", "coverage.no_match"]] = Field(default_factory=list)
    round_number: int | None = Field(default=None, ge=1)
    coordinator_status: Literal["not_requested", "planned", "failed"] = "not_requested"
    planned_task_ids: list[str] = Field(default_factory=list)
    planning_origin: Literal["runtime", "coordinator_llm", "demo"] = "runtime"
    planning_basis_read_ids: list[str] = Field(default_factory=list)
    coordinator_guidance_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def bound_targets(self):
        if self.coordinator_status == "planned":
            if self.action not in {RecoveryAction.RESEARCH_GAP, RecoveryAction.REPLAN_QUERY} or not self.planned_task_ids:
                raise ValueError("planned supplementation needs a task-level action and task IDs")
        elif self.planned_task_ids:
            raise ValueError("task IDs require completed Coordinator planning")
        if self.coordinator_status == "failed" and self.action != RecoveryAction.STOP:
            raise ValueError("failed Coordinator planning must stop supplementation")
        if len(set(self.planned_task_ids)) != len(self.planned_task_ids):
            raise ValueError("planned task IDs must be unique within the point")
        if self.planning_origin != "runtime" and self.coordinator_status != "planned":
            raise ValueError("model/demo origin requires validated planning")
        if self.planning_origin == "coordinator_llm" and not self.coordinator_guidance_ids:
            raise ValueError("LLM planning requires loaded Coordinator guidance")
        if len(set(self.planning_basis_read_ids)) != len(self.planning_basis_read_ids):
            raise ValueError("planning read references must be unique")
        if self.action == RecoveryAction.READ_ARTIFACT and not self.artifacts:
            raise ValueError("read recovery needs explicit Artifact handles")
        if self.action == RecoveryAction.FETCH_FULLTEXT and (not self.source_id or not self.source_record_ids):
            raise ValueError("acquisition recovery needs a Provider and bound record handles")
        if self.action in {RecoveryAction.CHANGE_PROVIDER, RecoveryAction.RETRY_REQUEST} and not self.source_id:
            raise ValueError("Provider recovery needs an explicit enabled source")
        if self.action in {RecoveryAction.STOP, RecoveryAction.NONE, RecoveryAction.MANUAL} and self.max_additional_attempts:
            raise ValueError("terminal recovery decisions cannot grant attempts")
        return self


class RecoverySearchFact(FailureContract):
    execution_id: str
    source_id: str
    query: str
    status: Literal["not_run", "succeeded", "partial", "failed", "requires_human"]
    candidate_count: int = Field(ge=0)


class RecoveryAttempt(FailureContract):
    """Compact, observed history supplied to Coordinator and the next Planner."""
    point_id: str
    task_id: str
    attempt: int = Field(ge=1)
    language: str
    task_type: str = "unknown"  # Old audit records may lack the task classification.
    description: str
    status: Literal["completed", "partial", "failed", "result_missing"]
    produced_card_count: int = Field(ge=0)
    failure_codes: list[FailureCode] = Field(default_factory=list)
    searches: list[RecoverySearchFact] = Field(default_factory=list)
