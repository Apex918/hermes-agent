from hermes_cli import config as config_module
from tools import delegate_tool


def test_empty_delegation_inherits_explicit_orchestration_worker_route(monkeypatch):
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: {
            "delegation": {"provider": "", "model": ""},
            "orchestration": {
                "worker": {
                    "primary": {
                        "provider": "worker-provider",
                        "model": "worker-model",
                    }
                }
            },
        },
    )

    resolved = delegate_tool._load_config()

    assert resolved["provider"] == "worker-provider"
    assert resolved["model"] == "worker-model"


def test_explicit_delegation_route_wins_over_orchestration_worker(monkeypatch):
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: {
            "delegation": {"provider": "explicit-provider", "model": "explicit-model"},
            "orchestration": {
                "worker": {
                    "primary": {"provider": "worker-provider", "model": "worker-model"}
                }
            },
        },
    )

    resolved = delegate_tool._load_config()

    assert resolved["provider"] == "explicit-provider"
    assert resolved["model"] == "explicit-model"
