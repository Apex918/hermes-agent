"""Tests for the decision-grade HQB daily quote report."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "optional-skills" / "productivity" / "hqb-index-operations"
SCRIPT = SKILL_DIR / "scripts" / "hqb_index_operations.py"
FIXTURE = SKILL_DIR / "fixtures" / "hqb-index-operations.v1.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("hqb_index_operations_report", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _business_fixture():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["report_mode"] = "business_fact"
    fixture["as_of"] = "2026-09-01T09:05:00+00:00"
    fixture["expected_scope"] = {
        "sku_profile_ids": [101],
        "market_ids": [11],
        "channel_ids": [21],
        "supplier_channel_ids": [201, 202],
    }
    fixture["quote_rows"] = [
        {
            "sku_profile_id": 101,
            "sku_code": "USB-C-1M",
            "sku_name": "USB-C cable",
            "market_id": 11,
            "market_name": "CN",
            "channel_id": 21,
            "channel_name": "HQB",
            "supplier_id": 301,
            "supplier_name": "Supplier A",
            "supplier_channel_id": 201,
            "supplier_channel_name": "HQB counter A",
            "offer_id": 401,
            "observation_id": 501,
            "unit_price": "12.50",
            "currency_code": "CNY",
            "unit": "piece",
            "min_order_quantity": "10",
            "available_quantity": "80",
            "observed_at": "2026-09-01T08:00:00+00:00",
            "valid_until": "2026-09-02T08:00:00+00:00",
            "approval_status": 1,
            "source": "manual",
            "evidence_ref": "candidate-001",
            "batch_id": "batch-001",
        },
        {
            "sku_profile_id": 101,
            "sku_code": "USB-C-1M",
            "sku_name": "USB-C cable",
            "market_id": 11,
            "market_name": "CN",
            "channel_id": 21,
            "channel_name": "HQB",
            "supplier_id": 302,
            "supplier_name": "Supplier B",
            "supplier_channel_id": 202,
            "supplier_channel_name": "HQB counter B",
            "offer_id": 402,
            "observation_id": 502,
            "unit_price": "13.00",
            "currency_code": "CNY",
            "unit": "piece",
            "min_order_quantity": "20",
            "available_quantity": "50",
            "observed_at": "2026-09-01T08:30:00+00:00",
            "valid_until": "2026-09-02T08:30:00+00:00",
            "approval_status": 1,
            "source": "manual",
            "evidence_ref": "candidate-002",
            "batch_id": "batch-001",
        },
    ]
    return fixture


def test_business_fact_report_is_traceable_and_dimension_safe():
    module = _load_module()
    report = module.build_daily_report(_business_fixture())

    assert report["schema_version"] == "hqb.daily_quote_report.v1"
    assert report["report_mode"] == "business_fact"
    assert report["decision_status"] in {"READY_FOR_DECISION", "LIMITED_SCOPE"}
    assert report["counts"]["accepted_current_count"] == 2
    assert len(report["quote_rows"]) == 2
    assert {row["supplier_channel_id"] for row in report["quote_rows"]} == {201, 202}
    assert {row["offer_id"] for row in report["quote_rows"]} == {401, 402}
    assert {row["observation_id"] for row in report["quote_rows"]} == {501, 502}
    assert len(report["aggregates"]) == 2
    assert all(item["sample_count"] == 1 for item in report["aggregates"])
    assert report["safety"] == {
        "integration_status": "not_integrated",
        "network_access": False,
        "external_effects": 0,
    }


def test_offline_fixture_is_demo_only_and_cannot_make_decision_conclusion():
    module = _load_module()
    report = module.build_daily_report(module.load_fixture(FIXTURE))
    markdown = module.render_daily_report_markdown(report)

    assert report["report_mode"] == "demo_fixture"
    assert report["decision_status"] == "DEMO_ONLY"
    assert report["aggregates"] == []
    assert "DEMO_ONLY" in markdown
    assert "不可用于采购或决策" in markdown


def test_markdown_is_deterministic_projection_of_canonical_report():
    module = _load_module()
    fixture = _business_fixture()
    report_a = module.build_daily_report(copy.deepcopy(fixture))
    report_b = module.build_daily_report(copy.deepcopy(fixture))

    assert report_a == report_b
    assert module.render_daily_report_markdown(report_a) == module.render_daily_report_markdown(report_b)
    assert "packet_hash" in report_a["evidence_index"][0]
    assert "hqb.daily_quote_report.v1" in module.render_daily_report_markdown(report_a)
