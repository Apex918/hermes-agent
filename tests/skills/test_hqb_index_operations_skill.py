"""Offline contract tests for the HQB index operations skill."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "optional-skills" / "productivity" / "hqb-index-operations"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts" / "hqb_index_operations.py"
FIXTURE = SKILL_DIR / "fixtures" / "hqb-index-operations.v1.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("hqb_index_operations", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_frontmatter_and_required_sections():
    content = SKILL_MD.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    _, raw_frontmatter, body = content.split("---", 2)
    frontmatter = yaml.safe_load(raw_frontmatter)
    assert frontmatter["name"] == "hqb-index-operations"
    assert len(frontmatter["description"]) <= 60
    assert frontmatter["description"].endswith(".")
    for key in ("version", "author", "license", "platforms", "metadata"):
        assert key in frontmatter
    for heading in ("## When to Use", "## Prerequisites", "## How to Run", "## Quick Reference", "## Procedure", "## Pitfalls", "## Verification"):
        assert heading in body


def test_default_fixture_is_safe_and_valid():
    module = _load_module()
    fixture = module.load_fixture(FIXTURE)
    assert fixture["integration_status"] == "not_integrated"
    assert fixture["network_access"] is False
    assert fixture["external_effects"] == 0
    assert len(fixture["candidates"]) == 2


def test_packet_summary_readback_and_report_are_offline():
    module = _load_module()
    fixture = module.load_fixture(FIXTURE)
    packet = module.build_evidence_packet(fixture)
    summary = module.read_ruoyi_readonly_summary(fixture)
    batch = module.readback_batch(fixture, "batch-001")
    report = module.build_daily_report(fixture)

    assert len(packet["packet_hash"]) == 64
    assert all(len(candidate["candidateHash"]) == 64 for candidate in packet["candidates"])
    assert packet["submission"] == "not_submitted"
    assert summary["mode"] == "readonly"
    assert summary["method"] == "GET"
    assert batch["verified"] is True
    assert batch["candidate_count"] == 2
    assert report["counts"] == {"candidate_count": 2, "batch_count": 1, "verified_batch_count": 1}
    assert report["integration_status"] == "not_integrated"
    assert report["network_access"] is False
    assert report["external_effects"] == 0


def test_ima_archive_is_only_a_dry_run():
    module = _load_module()
    preview = module.ima_archive_preview("daily report")
    assert preview["operation"] == "archive"
    assert preview["mode"] == "dry_run"
    assert preview["production_write"] is False
    assert preview["integration_status"] == "not_integrated"


def test_network_credentials_and_mutating_summary_fail_closed(tmp_path: Path):
    module = _load_module()
    with pytest.raises(module.HqbOperationsError, match="network"):
        module.load_fixture("https://example.invalid/fixture.json")

    unsafe = json.loads(FIXTURE.read_text(encoding="utf-8"))
    unsafe["candidates"][0]["payload"]["api_key"] = "must-not-be-read"
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(module.HqbOperationsError, match="credential"):
        module.load_fixture(path)

    mutation = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mutation["readonly_summary"]["method"] = "POST"
    path = tmp_path / "mutation.json"
    path.write_text(json.dumps(mutation), encoding="utf-8")
    with pytest.raises(module.HqbOperationsError, match="GET"):
        module.load_fixture(path)


def test_exact_readback_and_tenant_scope_fail_closed():
    module = _load_module()
    fixture = module.load_fixture(FIXTURE)
    with pytest.raises(module.HqbOperationsError, match="unavailable"):
        module.readback_batch(fixture, "batch-does-not-exist")
    wrong_tenant = dict(fixture)
    wrong_tenant["readonly_summary"] = dict(fixture["readonly_summary"])
    wrong_tenant["readonly_summary"]["tenant_id"] = "2002"
    with pytest.raises(module.HqbOperationsError, match="tenant"):
        module.validate_fixture(wrong_tenant)


def test_cli_all_writes_only_local_output(tmp_path: Path, capsys):
    module = _load_module()
    output = tmp_path / "result.json"
    rc = module.main(["--fixture", str(FIXTURE), "--output", str(output), "all"])
    assert rc == 0
    assert output.is_file()
    parsed = json.loads(output.read_text(encoding="utf-8"))
    assert "packet" in parsed and "ruoyi_readonly_summary" in parsed
    assert parsed["integration_status"] == "not_integrated"
    assert "https://" not in capsys.readouterr().out


def test_cli_accepts_documented_options_after_subcommand(tmp_path: Path, capsys):
    module = _load_module()
    output = tmp_path / "report.json"
    rc = module.main(["--fixture", str(FIXTURE), "report", "--output", str(output)])
    assert rc == 0
    assert output.is_file()
    parsed = json.loads(output.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == module.REPORT_SCHEMA
    assert "https://" not in capsys.readouterr().out
