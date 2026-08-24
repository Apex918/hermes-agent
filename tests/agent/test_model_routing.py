from types import SimpleNamespace

import pytest

from agent.model_routing import ModelPlanner, resolve_model_routes


class _FakeCall:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response))]
        )


def test_default_routes_match_requested_strong_and_fast_priority():
    routes = resolve_model_routes({})

    assert routes.planner.primary.provider == "xai-oauth"
    assert routes.planner.primary.model == "grok-4.6"
    assert [(item.provider, item.model) for item in routes.planner.fallbacks] == [
        ("hp-fenno", "gpt-5.6-sol")
    ]
    assert routes.worker.primary.provider == "hp-fenno"
    assert routes.worker.primary.model == "gpt-5.6-luna"


def test_configured_routes_override_defaults_without_changing_roles():
    routes = resolve_model_routes(
        {
            "orchestration": {
                "planner": {
                    "primary": {"provider": "custom", "model": "planner-x"},
                    "fallback": [{"provider": "custom", "model": "planner-y"}],
                },
                "worker": {
                    "primary": {"provider": "custom", "model": "worker-x"}
                },
            }
        }
    )

    assert routes.planner.primary.model == "planner-x"
    assert routes.planner.fallbacks[0].model == "planner-y"
    assert routes.worker.primary.model == "worker-x"


def test_planner_uses_grok_then_sol_fallback_and_reports_actual_model():
    fake_call = _FakeCall(
        [
            RuntimeError("primary unavailable"),
            '{"plan_id":"p1","objective":"do x","execution_mode":"direct",'
            '"tasks":[{"id":"t1","title":"x","role":"coordinator",'
            '"backend":"direct","depends_on":[]}]}',
        ]
    )
    planner = ModelPlanner(resolve_model_routes({}), call_llm=fake_call)

    result = planner.plan("do x")

    assert result.actual_model == "gpt-5.6-sol"
    assert result.actual_provider == "hp-fenno"
    assert result.fallback_used is True
    assert [call["model"] for call in fake_call.calls] == ["grok-4.6", "gpt-5.6-sol"]


def test_planner_prompt_is_plan_only_and_never_receives_tools():
    fake_call = _FakeCall(
        ['{"plan_id":"p1","objective":"do x","execution_mode":"direct",'
         '"tasks":[{"id":"t1","title":"x","role":"coordinator",'
         '"backend":"direct","depends_on":[]}]}']
    )
    planner = ModelPlanner(resolve_model_routes({}), call_llm=fake_call)

    planner.plan("do x")

    call = fake_call.calls[0]
    assert call["tools"] == []
    assert "must not execute" in call["messages"][0]["content"].lower()
    assert "json" in call["messages"][0]["content"].lower()


def test_planner_rejects_non_object_or_invalid_plan_response():
    fake_call = _FakeCall(["not json", "not json"])
    planner = ModelPlanner(resolve_model_routes({}), call_llm=fake_call)

    with pytest.raises(ValueError, match="all planner models failed"):
        planner.plan("do x")
