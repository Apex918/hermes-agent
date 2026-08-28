"""Hermes AI Business OS package."""

from . import validator
from .adapters import ReadOnlyAdapter
from .approval_state_machine import ApprovalOutcome, ApprovalPlan, ApprovalStateMachine
from .contracts import (
    ActionRequest,
    ContractValidationError,
    DecisionRecord,
    EvidenceEnvelope,
    WorkItem,
)
from .crm_provider import CRMCommitResult, CRMProvider, CRMWriteProposal
from .cto_agent import CTOAgent, build_cto_agent
from .feishu_fixtures import (
    DEFAULT_FEISHU_FIXTURE_PATH,
    EVENT_KINDS,
    FeishuFixtureScope,
    FeishuFixtureValidationError,
    OfflineFeishuEventStore,
    load_feishu_event_fixture,
    redact_sensitive_fields,
)
from .gate import (
    ApprovalError,
    ApprovalReceipt,
    AuditEntry,
    ConnectorAllowlist,
    ConnectorNotAllowedError,
    FakeConnector,
    OperationNotFoundError,
    ReleaseShadowGate,
    ShadowOnlyResult,
)
from .role_packs import (
    RolePack,
    build_all_role_packs,
    build_p0_role_packs,
    build_p1_sensitive_role_packs,
    build_sensitive_role_packs,
)
from .twenty_sandbox import (
    DEFAULT_TWENTY_FIXTURE_PATH,
    FakeTwentyCRMProvider,
    FakeTwentyProvider,
    TwentySandboxProof,
    TwentySandboxProvider,
    build_twenty_sandbox_proof,
    load_twenty_sandbox_fixture,
)

__all__ = [
    "ActionRequest",
    "ApprovalError",
    "ApprovalOutcome",
    "ApprovalPlan",
    "ApprovalReceipt",
    "ApprovalStateMachine",
    "AuditEntry",
    "CRMCommitResult",
    "CRMProvider",
    "CRMWriteProposal",
    "ConnectorAllowlist",
    "ConnectorNotAllowedError",
    "ContractValidationError",
    "CTOAgent",
    "DEFAULT_FEISHU_FIXTURE_PATH",
    "DEFAULT_TWENTY_FIXTURE_PATH",
    "DecisionRecord",
    "EVENT_KINDS",
    "FeishuFixtureScope",
    "FeishuFixtureValidationError",
    "EvidenceEnvelope",
    "FakeConnector",
    "FakeTwentyCRMProvider",
    "FakeTwentyProvider",
    "OfflineFeishuEventStore",
    "OperationNotFoundError",
    "ReadOnlyAdapter",
    "ReleaseShadowGate",
    "RolePack",
    "ShadowOnlyResult",
    "TwentySandboxProof",
    "TwentySandboxProvider",
    "WorkItem",
    "build_all_role_packs",
    "build_cto_agent",
    "build_p0_role_packs",
    "build_p1_sensitive_role_packs",
    "build_sensitive_role_packs",
    "build_twenty_sandbox_proof",
    "load_feishu_event_fixture",
    "load_twenty_sandbox_fixture",
    "redact_sensitive_fields",
    "validator",
]
