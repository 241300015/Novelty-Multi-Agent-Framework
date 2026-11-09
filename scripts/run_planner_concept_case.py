"""Run a single stage-one case; keep the reviewer blind to a controlled edit."""
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
from novelty_agent_framework.schemas import NoveltyPoint, ResearchTask


async def run(args):
    data = json.loads(args.source.read_text())
    cfg = load_application_config()
    registry = build_model_registry(cfg)
    agent = build_search_planner(legacy_shape(cfg), registry, build_prompt_library())
    # The local acceptance experiment must not fall back to another provider.
    configured_client = registry.client_for(agent._model_alias)
    profile = configured_client.profile
    if urlparse(profile.base_url).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("本实验只允许连接本机模型服务")
    agent.model_client = OpenAICompatibleChatClient(profile)
    debug = RuntimeDebugConfig(output_root=args.output, archive_root=args.output / "runtime-archive",
                               max_model_calls=args.max_model_calls)
    manager = RuntimeArtifactManager(data["paper_id"], config=debug,
                                     runtime_config={"max_model_calls": args.max_model_calls})
    with manager:
        try:
            await run_cases(args, cfg, agent, manager)
        except BaseException as exc:
            manager.finish_run("FAILED", error=exc)
            raise
        else:
            manager.finish_run("SUCCESS")


async def run_cases(args, cfg, agent, manager):
    data = json.loads(args.source.read_text())
    point = NoveltyPoint.model_validate(next(p for p in data["novelty_points"] if p["point_id"] == args.point))
    if args.scenario in ("A", "B"):
        point = point.model_copy(update={"claim_en":"", "technical_features_en":[]})
    elif args.scenario == "C":
        point = point.model_copy(update={"claim":"", "technical_features":[]})
    languages = {"A":["zh"], "B":["en"], "C":["zh"], "D":["zh","en"]}[args.scenario]
    original = agent.generate_concepts
    for lang in languages:
        task = ResearchTask(task_id=f"{data['paper_id']}-{args.scenario}-{args.repeat}-{lang}",
                            novelty_point_id=point.point_id, task_type="literature_search", language=lang)
        audit = ConceptAudit(args.output, point, task, manager.run_id)
        audit.write("case.json", {"source":str(args.source), "source_sha256":hashlib.sha256(args.source.read_bytes()).hexdigest(),
                                 "model":cfg.search_planner.model.model_dump(), "injection":args.inject,
                                 "target":args.target,"alias":args.alias,"destination":args.destination})
        async def generate(call, payload):
            batch = await original(call, payload)
            if not args.inject:
                return batch
            before = batch.model_dump()
            matches = [c for c in batch.concepts if args.target in c.terms]
            if len(matches) != 1:
                raise ValueError("未满足注入条件：目标词项无法唯一定位")
            if args.inject == "omit":
                batch.concepts.remove(matches[0])
                if not batch.concepts:
                    raise ValueError("未满足注入条件：删除后没有候选")
            else:
                dest = [c for c in batch.concepts if args.destination in c.terms]
                if len(dest) != 1 or dest[0] is matches[0] or args.alias not in matches[0].alias:
                    raise ValueError("未满足注入条件：缺少指定别名或目标概念")
                matches[0].alias.remove(args.alias)
                dest[0].alias.append(args.alias)
            audit.write("injection.json", {"before":before, "after":batch.model_dump()})
            return batch
        agent.generate_concepts = generate
        try:
            concepts = await agent._concept_phase(point, task, True, audit.event)
            audit.write("accepted-concepts.json", concepts)
            audit.write("semantic-review.json", {"status":"pending_human_review"})
            audit.finish()
        except BaseException as exc:
            audit.finish(error=str(exc))
            raise
        finally:
            print(audit.directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--point", required=True)
    parser.add_argument("--scenario", choices=list("ABCD"), required=True)
    parser.add_argument("--repeat", type=int, choices=(1,2,3), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-model-calls", type=int, default=24, help="每次命令的最大模型调用次数")
    parser.add_argument("--inject", choices=("omit", "alias"))
    parser.add_argument("--target")
    parser.add_argument("--alias")
    parser.add_argument("--destination")
    args = parser.parse_args()
    if args.inject and not args.target:
        parser.error("注入需要预先指定 --target")
    if args.inject == "alias" and not (args.alias and args.destination):
        parser.error("错配注入需要 --alias 和 --destination")
    asyncio.run(run(args))
