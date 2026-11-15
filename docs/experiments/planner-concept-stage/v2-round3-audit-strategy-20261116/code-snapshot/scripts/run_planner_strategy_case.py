"""Test Q/R independently using a frozen concept catalog, without regenerating it."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from backend.env import OpenAICompatibleChatClient
from novelty_agent_framework.config.factory import build_model_registry, build_prompt_library, build_search_planner
from novelty_agent_framework.config.loader import legacy_shape, load_application_config
from novelty_agent_framework.core.concept_audit import ConceptAudit
from novelty_agent_framework.core.runtime_artifacts import RuntimeArtifactManager, RuntimeDebugConfig
from novelty_agent_framework.schemas import NoveltyPoint, ResearchTask, SearchPlan
from novelty_agent_framework.schemas.search_plan_draft import SearchConceptDraft


async def run(args):
    data=json.loads(args.source.read_text())
    point=NoveltyPoint.model_validate(next(p for p in data['novelty_points'] if p['point_id']==args.point))
    point=point.model_copy(update={'claim_en':'','technical_features_en':[]})
    frozen=SearchPlan.model_validate_json(args.frozen_plan.read_text())
    if frozen.novelty_point_id!=point.point_id:
        raise ValueError('Frozen plan belongs to another novelty point')
    task=ResearchTask(task_id=f'{data["paper_id"]}-{args.point}-strategy-only-zh',novelty_point_id=point.point_id,
        task_type='literature_search',language='zh')
    concepts=[SearchConceptDraft(terms=c.terms,role=c.role or 'object',alias=c.alias,exclude=c.exclude,importance=c.importance).model_dump() for c in frozen.concepts]
    cfg=load_application_config()
    registry=build_model_registry(cfg)
    agent=build_search_planner(legacy_shape(cfg),registry,build_prompt_library())
    profile=registry.client_for(agent._model_alias).profile
    if urlparse(profile.base_url).hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('Only local models are allowed')
    agent.model_client=OpenAICompatibleChatClient(profile)
    args.output.mkdir(parents=True,exist_ok=False)
    manager=RuntimeArtifactManager(data['paper_id'],config=RuntimeDebugConfig(output_root=args.output,
        archive_root=args.output/'runtime-archive',max_model_calls=24),runtime_config={'max_model_calls':24})
    with manager:
        audit=ConceptAudit(args.output,point,task,manager.run_id,protocol='v2-strategy-only',
            publish_root=manager.run_dir/'planner' if manager.run_dir else None)
        audit.write('case.json',dict(source=str(args.source),frozen_plan=str(args.frozen_plan),
            source_sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
            frozen_sha256=hashlib.sha256(args.frozen_plan.read_bytes()).hexdigest(),
            model=cfg.search_planner.model.model_dump(),scope='strategy_only',human_semantic_review='pending'))
        audit.write('frozen-concepts.json',concepts)
        audit.event(0,'concept_stage_finished',dict(accepted=list(range(1,len(concepts)+1)),unresolved={},origin='frozen_fixture'))
        try:
            plan=await agent._strategy_phase(point,task,concepts,True,audit.event)
        except BaseException as exc:
            audit.finish(error=f'{type(exc).__name__}: {exc}',cancelled=isinstance(exc,asyncio.CancelledError))
            manager.finish_run('FAILED',error=exc)
            raise
        else:
            audit.finish(plan=plan)
            manager.finish_run('SUCCESS')
        finally:
            print(audit.directory,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('--frozen-plan',type=Path,required=True)
    parser.add_argument('--point',default='NP-1')
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(run(parser.parse_args()))
