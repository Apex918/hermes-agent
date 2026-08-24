import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from agent.orchestrator import Orchestrator


_PLUGIN_PATH = Path.home() / ".hermes" / "plugins" / "orchestrator" / "__init__.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("hermes_orchestrator_plugin_test", _PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_complex_gateway_request_uses_model_planner_before_rewrite(monkeypatch):
    plugin = _load_plugin()
    objective = "研究并开发一个金融晨报系统，最后测试和审查"
    calls = []
    plan = Orchestrator().plan(objective)

    class _Planner:
        def plan(self, value):
            calls.append(value)
            return SimpleNamespace(
                plan=plan,
                actual_provider="xai-oauth",
                actual_model="grok-4.6",
                fallback_used=False,
            )

    monkeypatch.setattr(plugin, "_get_model_planner", lambda: _Planner(), raising=False)
    monkeypatch.setattr(plugin, "_get_orchestrator", lambda: Orchestrator())

    result = plugin._handle_pre_dispatch(
        event=SimpleNamespace(text=objective), gateway=object()
    )

    assert calls == [objective]
    assert result["action"] == "rewrite"
    assert "grok-4.6" in result["text"]
    assert "Plan JSON" in result["text"]


def test_planner_failure_falls_back_to_local_orchestrator(monkeypatch):
    plugin = _load_plugin()
    objective = "研究并开发一个金融晨报系统，最后测试和审查"
    monkeypatch.setattr(
        plugin,
        "_get_model_planner",
        lambda: SimpleNamespace(plan=lambda value: (_ for _ in ()).throw(RuntimeError("down"))),
        raising=False,
    )
    monkeypatch.setattr(plugin, "_get_orchestrator", lambda: Orchestrator())

    result = plugin._handle_pre_dispatch(
        event=SimpleNamespace(text=objective), gateway=object()
    )

    assert result["action"] == "rewrite"
    assert "Plan JSON" in result["text"]


class _State:
    def __init__(self):
        self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _Ctx:
    def __init__(self):
        self.state = _State()
        self.dispatch_calls = []

    def dispatch_tool(self, name, args):
        self.dispatch_calls.append((name, args))
        raise AssertionError("dispatch_tool should not be called")


def test_fresh_module_load_rebuilds_the_orchestrator_cache():
    plugin1 = _load_plugin()
    marker = object()
    plugin1._local.orchestrator_instance = marker
    assert plugin1._get_orchestrator() is marker

    plugin2 = _load_plugin()
    first = plugin2._get_orchestrator()
    second = plugin2._get_orchestrator()
    assert first is second
    assert first is not marker


def test_explicit_orchestrate_task_returns_plan_ready_or_queued_envelope_without_side_effects():
    plugin = _load_plugin()
    ctx = _Ctx()

    direct = json.loads(plugin._handle_orchestrate_tool(ctx, {"objective": "解释这个函数的作用"}))
    assert direct["status"] == "plan_ready"
    assert direct["execution_request"]["backend"] == "direct"
    assert direct["plan"]["execution_mode"] == "direct"
    assert ctx.dispatch_calls == []

    durable = json.loads(plugin._handle_orchestrate_tool(ctx, {"objective": "研究并开发一个金融晨报系统，最后测试和审查"}))
    assert durable["status"] == "queued"
    assert durable["execution_request"]["backend"] == "kanban"
    assert durable["plan"]["execution_mode"] == "durable"
    assert "tasks" in durable["execution_request"]["graph"]
    assert ctx.dispatch_calls == []

    plans = ctx.state.get("orchestration_plans") or {}
    assert plans
    assert len(plans) == 2
    assert all(record["status"] == "pending" for record in plans.values())


def test_subagent_stop_ignores_unknown_correlation_and_partial_completion():
    plugin = _load_plugin()
    ctx = _Ctx()
    plan_result = json.loads(plugin._handle_orchestrate_tool(ctx, {"objective": "研究并开发一个金融晨报系统，最后测试和审查"}))
    plan_id = plan_result["plan"]["plan_id"]
    plans = ctx.state.get("orchestration_plans") or {}
    assert plans
    record = plans[plan_id]
    assert record["expected_task_ids"]

    plugin._on_subagent_stop(
        ctx,
        parent_session_id="session-1",
        child_summary="no orchestration id here",
        child_status="completed",
    )
    assert ctx.dispatch_calls == []
    assert ctx.state.get("orchestration_plans")[plan_id]["status"] == "pending"

    plugin._on_subagent_stop(
        ctx,
        parent_session_id="session-1",
        child_summary=f"orchestration_task_id: {record['expected_task_ids'][0]}",
        child_status="queued",
    )
    assert ctx.dispatch_calls == []
    assert ctx.state.get("orchestration_plans")[plan_id]["status"] == "pending"
