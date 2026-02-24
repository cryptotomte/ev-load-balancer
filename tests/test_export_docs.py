"""Tests for PR-08 deliverables: README, hacs.json, automation template, charger guides.

Verifies that all documentation and configuration files exist and have
correct format and content.
"""

import json
from pathlib import Path

import pytest
import yaml

# Repo root -- one level up from tests/
REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def readme_content() -> str:
    """Read README.md once and share between all README tests."""
    readme_path = REPO_ROOT / "README.md"
    assert readme_path.exists(), "README.md missing from repo root"
    return readme_path.read_text(encoding="utf-8")


# ── README tests ────────────────────────────────────────────────────────


def test_readme_has_required_sections(readme_content: str) -> None:
    """Verify that README contains all mandatory sections."""
    required_sections = [
        "## Features",
        "## Prerequisites",
        "## Installation",
        "## Configuration",
        "## Troubleshooting",
        "## Who Does What?",
    ]
    for section in required_sections:
        assert section in readme_content, f"README is missing mandatory section: {section}"


def test_readme_has_responsibility_matrix(readme_content: str) -> None:
    """Verify that README contains the responsibility matrix (FR-002)."""
    for column in ("EV Load Balancer", "Charger integration", "EV Charging Manager"):
        assert column in readme_content, f"Responsibility matrix missing column: {column}"


def test_readme_has_calculation_explanation(readme_content: str) -> None:
    """Verify that README contains the calculation explanation (FR-003)."""
    assert "## How It Works" in readme_content, "README missing How It Works section"


def test_readme_has_four_troubleshooting_scenarios(readme_content: str) -> None:
    """Verify that README has at least four troubleshooting scenarios (FR-004)."""
    assert "## Troubleshooting" in readme_content, "README missing Troubleshooting section"

    # Extract the Troubleshooting section (up to the next h2)
    troubleshooting_section = readme_content.split("## Troubleshooting")[1]
    next_h2 = troubleshooting_section.find("\n## ")
    if next_h2 > 0:
        troubleshooting_section = troubleshooting_section[:next_h2]

    scenario_count = troubleshooting_section.count("\n### ")
    assert scenario_count >= 4, (
        f"Troubleshooting has {scenario_count} scenarios — requires at least 4 (FR-004)"
    )


def test_readme_has_evcm_relation(readme_content: str) -> None:
    """Verify that README explains the relation to EV Charging Manager (FR-011)."""
    assert "## Relation to EV Charging Manager" in readme_content, (
        "README missing section about relation to EV Charging Manager"
    )


# ── hacs.json ────────────────────────────────────────────────────────────


def test_hacs_json_valid() -> None:
    """Verify that hacs.json is valid JSON with the correct name (FR-005)."""
    hacs_path = REPO_ROOT / "hacs.json"
    assert hacs_path.exists(), "hacs.json missing from repo root"

    with hacs_path.open(encoding="utf-8") as f:
        data = json.load(f)

    assert data.get("name") == "EV Load Balancer", (
        f"hacs.json name is '{data.get('name')}' — expected 'EV Load Balancer'"
    )
    assert data.get("render_readme") is True, "hacs.json render_readme must be true"


# ── GitHub workflow ──────────────────────────────────────────────────────


def test_github_workflow_exists() -> None:
    """Verify that the HACS validation workflow exists (FR-006)."""
    workflow_path = REPO_ROOT / ".github" / "workflows" / "validate.yaml"
    assert workflow_path.exists(), ".github/workflows/validate.yaml missing"

    with workflow_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert data is not None, "validate.yaml is empty or invalid"
    assert "jobs" in data, "validate.yaml missing 'jobs'"
    assert "validate-hacs" in data["jobs"], "validate.yaml missing 'validate-hacs' job"


# ── InfluxDB automation template ─────────────────────────────────────────


def test_influxdb_automation_yaml_valid() -> None:
    """Verify automation template is valid YAML with correct event types (FR-009, FR-010)."""
    automation_path = REPO_ROOT / "automations" / "ev_load_balancer_influxdb_export.yaml"
    assert automation_path.exists(), "automations/ev_load_balancer_influxdb_export.yaml missing"

    with automation_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert data is not None, "Automation template is empty or invalid"
    assert "automation" in data, "Automation template missing 'automation' key"

    automations = data["automation"]
    assert len(automations) >= 3, (
        f"Automation template has {len(automations)} automations — expected at least 3"
    )

    # Each automation must have trigger and action
    for automation in automations:
        alias = automation.get("alias", "<unknown>")
        assert "trigger" in automation, f"Automation missing 'trigger': {alias}"
        assert "action" in automation, f"Automation missing 'action': {alias}"

    # Collect all event types and verify prefix + completeness
    event_types = {
        trigger["event_type"]
        for automation in automations
        for trigger in automation["trigger"]
        if "event_type" in trigger
    }

    for event_type in event_types:
        assert event_type.startswith("ev_load_balancer_"), (
            f"Event type '{event_type}' does not start with 'ev_load_balancer_'"
        )

    expected_events = {
        "ev_load_balancer_current_adjusted",
        "ev_load_balancer_device_paused",
        "ev_load_balancer_device_resumed",
        "ev_load_balancer_phase_switched",
    }
    missing = expected_events - event_types
    assert not missing, f"Automation template missing event types: {missing}"


# ── Charger profile guides ────────────────────────────────────────────────


def test_goe_guide_has_required_content() -> None:
    """Verify that the go-e guide contains mandatory information."""
    goe_guide = REPO_ROOT / "docs" / "charger-profiles" / "goe-gemini.md"
    assert goe_guide.exists(), "docs/charger-profiles/goe-gemini.md missing"

    content = goe_guide.read_text(encoding="utf-8")

    assert "59.x" in content, "go-e guide missing firmware requirement (≥ 59.x)"
    assert "ama" in content, "go-e guide missing ama information"
    assert "10" in content, "go-e guide missing recommended ama value (10A)"
    assert "L2" in content, "go-e guide missing phase mapping information"
    assert "ha-goecharger-api2" in content, "go-e guide missing reference to ha-goecharger-api2"


def test_community_guide_has_required_content() -> None:
    """Verify that the community contribution template contains mandatory information."""
    community_guide = REPO_ROOT / "docs" / "charger-profiles" / "README.md"
    assert community_guide.exists(), "docs/charger-profiles/README.md missing"

    content = community_guide.read_text(encoding="utf-8")

    assert "amp" in content, "Community guide missing info about amp entity"
    assert "frc" in content, "Community guide missing info about frc entity"
    assert "Pull Request" in content, "Community guide missing PR instructions"
    assert "charger_profiles.py" in content, (
        "Community guide missing reference to charger_profiles.py"
    )
