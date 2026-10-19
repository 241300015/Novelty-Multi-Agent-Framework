"""Restrict a recovery task to its runtime-selected operation and material handles."""
from __future__ import annotations

from ..schemas import ResearcherToolObservation
from ..schemas.failures import RecoveryAction as A, FailureCode, FailureScope, make_failure
from .researcher_registry import ResearcherToolRegistry


class RecoveryToolRegistry(ResearcherToolRegistry):
    def __init__(self, original, request):
        self.original, self.request = original, request.model_copy(deep=True)
        self.directive = request.recovery
        if self.directive is None:
            raise ValueError('recovery registry requires a runtime directive')
        action=self.directive.action
        names=set(original.names)
        if action in {A.STOP,A.MANUAL,A.NONE,A.RESUME_REVIEW,A.RESTORE_CHECKPOINT}:
            names.clear()
        elif action == A.READ_ARTIFACT:
            names &= {'reader','submit_evidence'}
        elif action in {A.FETCH_FULLTEXT,A.CHANGE_PROVIDER,A.RETRY_REQUEST}:
            names &= {'database_search','reader','submit_evidence'}
        self.allowed_artifacts={(a.namespace,a.artifact_id) for a in self.directive.artifacts}
        super().__init__([original.get(name) for name in original.names if name in names])

    async def execute_validated(self, tool_name, arguments, *, scope, started=None):
        try:
            if scope != self.request:
                raise PermissionError('recovery task scope differs from its frozen request')
            if tool_name not in self.names:
                raise PermissionError('tool is not part of the selected recovery action')
            action=self.directive.action
            if tool_name=='database_search':
                if self.directive.source_id and arguments.source_id!=self.directive.source_id:
                    raise PermissionError('database source differs from the configured recovery target')
                if action in {A.CHANGE_PROVIDER,A.RETRY_REQUEST} and arguments.full_text_source_record_ids:
                    raise PermissionError('search recovery cannot switch to an unrelated full-text acquisition')
                if action==A.FETCH_FULLTEXT:
                    ids=set(arguments.full_text_source_record_ids)
                    if not ids or not ids.issubset(self.directive.source_record_ids):
                        raise PermissionError('full-text recovery must use the bound records and cannot repeat search')
            if tool_name=='reader' and action in {A.READ_ARTIFACT,A.FETCH_FULLTEXT}:
                for item in getattr(arguments,'reads',None) or [arguments]:
                    namespace=getattr(item,'namespace',None)
                    resolver=getattr(self.original.get('reader'), '_namespace_for', None)
                    if callable(resolver):
                        namespace=resolver(scope.subject_paper_id, item)
                    matches=[handle for handle in self.allowed_artifacts if handle[1]==item.artifact_id
                             and (namespace is None or handle[0]==namespace)]
                    if len(matches)!=1:
                        raise PermissionError('Reader artifact is not an unambiguous recovery material handle')
        except PermissionError as exc:
            failure=make_failure(FailureCode.RECOVERY_SCOPE_CHANGED,scope=FailureScope(
                paper_id=scope.subject_paper_id,run_id=scope.run_id,
                point_id=scope.novelty_point.point_id,task_id=scope.research_task.task_id),
                message=str(exc),occurrence_id=tool_name)
            return ResearcherToolObservation(tool_name=tool_name,arguments=arguments.model_dump(mode='json'),
                succeeded=False,error=str(exc),payload={'error_type':'PermissionError','failure':failure.model_dump(mode='json')})
        observation=await self.original.execute_validated(tool_name,arguments,scope=scope,started=started)
        if tool_name=='database_search' and self.directive.action==A.FETCH_FULLTEXT:
            # Only artifact handles produced by the constrained, registered tool
            # extend access; the model cannot grant itself a new handle.
            bundle=observation.payload.get('research_bundle') or {}
            artifacts=[*observation.payload.get('artifacts',[]), *bundle.get('artifacts',[])]
            for artifact in artifacts:
                if artifact.get('source_record_id') in self.directive.source_record_ids:
                    self.allowed_artifacts.add(('research_reference',artifact['artifact_id']))
        return observation
