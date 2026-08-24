from agent.microsoft_agent_framework_adapter import (
    FakeMicrosoftAgentFrameworkProvider,
    MicrosoftAgentFrameworkAdapter,
)
from agent.orchestrator import Orchestrator


def test_adapter_builds_hitl_subgraph_with_hermes_owned_governance():
    plan = Orchestrator().plan("开发一个系统并测试")
    provider = FakeMicrosoftAgentFrameworkProvider()

    proof = MicrosoftAgentFrameworkAdapter(provider=provider).build_proof(plan)

    assert proof.framework == "microsoft-agent-framework"
    assert proof.network_off is True
    assert proof.provider["name"] == "fake-microsoft-agent-framework"
    assert proof.provider["calls"] == 1
    assert proof.hermes_ownership["routing"]["implementation"] == "Hermes"
    assert proof.hermes_ownership["profile"]["implementation"] == "default"
    assert proof.hermes_ownership["approval"]["owner"] == "Hermes"
    assert proof.hermes_ownership["audit"]["owner"] == "Hermes"

    hitl = proof.hitl_subgraph
    node_ids = {node["id"] for node in hitl["nodes"]}
    assert {"hermes.routing", "hermes.approval", "hermes.audit"} <= node_ids
    assert hitl["nodes_by_id"]["hermes.approval"]["type"] == "human_in_the_loop"
    assert ("hermes.routing", "agent_framework.graph") in hitl["edges"]
    assert ("agent_framework.graph", "hermes.approval") in hitl["edges"]
    assert ("hermes.approval", "hermes.audit") in hitl["edges"]


def test_adapter_accepts_mapping_plans_without_touching_network():
    provider = FakeMicrosoftAgentFrameworkProvider()
    adapter = MicrosoftAgentFrameworkAdapter(provider=provider)

    proof = adapter.build_proof(
        {
            "plan_id": "proof-1",
            "objective": "开发一个系统并测试",
            "execution_mode": "durable",
            "classification": {"domain": ["software"]},
            "tasks": [
                {"id": "research", "title": "研究输入", "role": "researcher"},
                {
                    "id": "implementation",
                    "title": "实现目标功能",
                    "role": "coder",
                    "depends_on": ["research"],
                },
                {
                    "id": "review",
                    "title": "审查实现",
                    "role": "reviewer",
                    "depends_on": ["implementation"],
                },
            ],
        }
    )

    assert provider.calls == 1
    assert proof.plan["plan_id"] == "proof-1"
    assert proof.plan["tasks"][1]["route"]["profile"] == "default"
    assert proof.hitl_subgraph["provider"]["network_off"] is True
    assert proof.audit_trail[0]["event"] == "provider_snapshot"
    assert proof.audit_trail[0]["payload"]["calls"] == 1
