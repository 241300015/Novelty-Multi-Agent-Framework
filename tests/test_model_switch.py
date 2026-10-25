import asyncio
import sys

import pytest

from backend.env import (
    ChatMessage, ModelCallBudgetExceeded, ModelClientError, ModelResponse,
    OpenAICompatibleChatClient, set_model_call_observer, reset_model_call_observer,
)
from backend.env.model_switch import (
    MODEL_ORDER, ModelSwitchAborted, ModelSwitchSession, model_switch_session,
)
from novelty_agent_framework.config import build_model_registry, load_application_config


MESSAGES = [ChatMessage(role="user", content="synthetic test")]


def client():
    return build_model_registry(load_application_config(environ={})).client_for(MODEL_ORDER[0])


@pytest.mark.parametrize("deny_at,expected", [(None, 3), (1, 1), (2, 2)])
def test_order_requires_separate_consent_and_rejection_stops(monkeypatch, deny_at, expected):
    calls, prompts, events = [], [], []

    def transport(self, messages, *, options=None):
        calls.append(self.profile.alias)
        if self.profile.alias != MODEL_ORDER[-1]:
            raise ModelClientError("sensitive detail must not appear in prompt", failure_reason="model.transport")
        return ModelResponse(content="ok")

    def approve(request, cancelled):
        prompts.append(request)
        assert len(calls) == len(prompts)
        assert "sensitive" not in request.message
        return len(prompts) != deny_at

    monkeypatch.setattr(OpenAICompatibleChatClient, "_complete", transport)
    session = ModelSwitchSession(approve)
    token = model_switch_session.set(session)
    observer = set_model_call_observer(events.append)
    try:
        if deny_at:
            with pytest.raises(ModelSwitchAborted):
                client().complete(MESSAGES)
            with pytest.raises(ModelSwitchAborted):
                client().complete(MESSAGES)
        else:
            active = client()
            assert asyncio.run(active.acomplete(MESSAGES)).content == "ok"
            assert [e.alias for e in events if e.phase == "START"] == list(MODEL_ORDER)
            assert len({e.call_id for e in events if e.phase == "START"}) == 3
            assert active.complete(MESSAGES).content == "ok"
            assert len(prompts) == 2  # consent stays within this run
            calls.pop()  # additional call went directly to the approved tier
        assert calls == list(MODEL_ORDER[:expected])
        assert [p.target for p in prompts] == list(MODEL_ORDER[1:len(prompts) + 1])
    finally:
        reset_model_call_observer(observer)
        model_switch_session.reset(token)


@pytest.mark.parametrize("error", [
    ModelClientError("bad JSON", failure_reason="model.response.envelope_invalid"),
    ModelCallBudgetExceeded("budget exhausted"),
])
def test_non_transport_errors_never_trigger_paid_fallback(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(OpenAICompatibleChatClient, "_complete", fail)
    token = model_switch_session.set(ModelSwitchSession(lambda *args: pytest.fail("unexpected prompt")))
    try:
        with pytest.raises(type(error)):
            client().complete(MESSAGES)
    finally:
        model_switch_session.reset(token)


def test_unattended_call_stops_without_cloud_dispatch(monkeypatch):
    calls = []
    def fail(self, *args, **kwargs):
        calls.append(self.profile.alias)
        raise ModelClientError("offline", failure_reason="model.transport")
    monkeypatch.setattr(OpenAICompatibleChatClient, "_complete", fail)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(ModelSwitchAborted, match="无法交互确认"):
        client().complete(MESSAGES)
    assert calls == [MODEL_ORDER[0]]


def test_model_credentials_do_not_fall_back_to_private_key(monkeypatch):
    for name in ("LOCAL_VLLM_API_KEY", "SILICONFLOW_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NOVELTY_API_KEY", "private-secret")
    monkeypatch.setenv("LLM_API_KEY", "private-secret")
    registry = build_model_registry(load_application_config(environ={}))
    assert all(registry.client_for(alias).profile.api_key is None for alias in MODEL_ORDER)


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("synchronous", [True, False])
def test_web_approval_waits_then_resumes_or_aborts(monkeypatch, tmp_path, allowed, synchronous):
    import httpx
    from novelty_agent_framework.config import NoveltyWebSettings
    from novelty_agent_framework.main import create_app
    from novelty_agent_framework.schemas import PaperInput
    from novelty_agent_framework.services import NoveltyWorkflowService

    calls = []
    def transport(self, *args, **kwargs):
        calls.append(self.profile.alias)
        if self.profile.alias == MODEL_ORDER[0]:
            raise ModelClientError("offline", failure_reason="model.transport")
        return ModelResponse(content="ok")
    monkeypatch.setattr(OpenAICompatibleChatClient, "_complete", transport)

    class Workflow:
        last_rendered_report_path = None
        async def arun(self, *args, **kwargs):
            if synchronous:
                client().complete(MESSAGES)
            else:
                await client().acomplete(MESSAGES)
            # End the synthetic run after the model boundary; no real workflow.
            raise RuntimeError("synthetic run finished")

    service = NoveltyWorkflowService(workflow_factory=lambda _: Workflow(), runs_root=tmp_path)
    app = create_app(NoveltyWebSettings(workflow_mode="demo"), service=service)

    async def scenario():
        task_id = service.create_run().task_id
        run = asyncio.create_task(service.execute(task_id, PaperInput(paper_id="test", title="test", abstract="test", full_text="synthetic text")))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
                for _ in range(200):
                    snapshot = (await api.get(f"/api/novelty/runs/{task_id}")).json()
                    if snapshot["model_approval"]:
                        break
                    await asyncio.sleep(0.01)
                request = snapshot["model_approval"]
                assert request and request["target"] == MODEL_ORDER[1]
                assert calls == [MODEL_ORDER[0]]
                url = f"/api/novelty/runs/{task_id}/model-switch"
                assert (await api.post(url, json={"request_id": "stale", "allowed": True})).status_code == 409
                assert (await api.post(url, json={"request_id": request["request_id"], "allowed": "true"})).status_code == 422
                assert (await api.post(url, json={"request_id": request["request_id"], "allowed": allowed})).status_code == 200
                await asyncio.wait_for(run, 3)
                assert calls == list(MODEL_ORDER[:2 if allowed else 1])
                final = service.get_run(task_id)
                assert final.model_approval is None
                assert final.error.code == ("workflow_failed" if allowed else "model_switch_aborted")
                assert (await api.post(url, json={"request_id": request["request_id"], "allowed": True})).status_code == 409
        finally:
            if not run.done():
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())


def test_concurrent_failures_share_consent_and_runs_are_isolated(monkeypatch):
    prompts = []
    def transport(self, *args, **kwargs):
        if self.profile.alias == MODEL_ORDER[0]:
            raise ModelClientError("offline", failure_reason="model.transport")
        return ModelResponse(content="ok")
    monkeypatch.setattr(OpenAICompatibleChatClient, "_complete", transport)

    async def scenario():
        active = client()
        for _ in range(2):
            token = model_switch_session.set(ModelSwitchSession(lambda request, _: prompts.append(request) or True))
            try:
                results = await asyncio.gather(*(active.acomplete(MESSAGES) for _ in range(4)))
                assert all(result.content == "ok" for result in results)
            finally:
                model_switch_session.reset(token)
        assert [request.target for request in prompts] == [MODEL_ORDER[1], MODEL_ORDER[1]]
    asyncio.run(scenario())


def test_cancelled_approval_never_dispatches_cloud(monkeypatch):
    from novelty_agent_framework.services.model_approval import RunModelApproval
    from novelty_agent_framework.services.jobs import InMemoryRunStore

    calls = []
    def fail(self, *args, **kwargs):
        calls.append(self.profile.alias)
        raise ModelClientError("offline", failure_reason="model.transport")
    monkeypatch.setattr(OpenAICompatibleChatClient, "_complete", fail)

    async def scenario():
        store = InMemoryRunStore()
        task_id = store.create().task_id
        approval = RunModelApproval(store, task_id)
        token = model_switch_session.set(approval.session)
        task = asyncio.create_task(client().acomplete(MESSAGES))
        try:
            for _ in range(200):
                if store.get(task_id).model_approval:
                    break
                await asyncio.sleep(0.01)
            assert store.get(task_id).model_approval
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(200):
                if store.get(task_id).model_approval is None:
                    break
                await asyncio.sleep(0.01)
            assert store.get(task_id).model_approval is None
            assert calls == [MODEL_ORDER[0]]
        finally:
            approval.close()
            model_switch_session.reset(token)
    asyncio.run(scenario())
