from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
ISSUE_TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE"
WORKFLOWS = ROOT / ".github" / "workflows"


def _load_yaml(path: Path) -> dict:
    """Load JSON-form YAML with the standard library to avoid parser ambiguity for `on`."""
    return json.loads(path.read_text(encoding="utf-8"))


def _checkboxes(form: dict) -> list[dict]:
    return [field for field in form["body"] if field["type"] == "checkboxes"]


def test_required_community_files_exist():
    required = {
        "LICENSE",
        "NOTICE",
        "CONTENT_POLICY.md",
        "PRIVACY.md",
        "SECURITY.md",
        "SUPPORT.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "THIRD_PARTY_NOTICES.md",
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/ISSUE_TEMPLATE/compatibility.yml",
        ".github/ISSUE_TEMPLATE/feature.yml",
        ".github/ISSUE_TEMPLATE/docs.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/pull_request_template.md",
        ".github/workflows/ci.yml",
        ".github/workflows/release-audit.yml",
    }
    assert all((ROOT / path).is_file() for path in required)


def test_each_submittable_issue_form_requires_a_private_material_confirmation():
    forbidden = ("真实试卷", "学生信息", "课程音视频", "密钥", "内部文件")
    for name in ("bug.yml", "compatibility.yml", "feature.yml", "docs.yml"):
        form = _load_yaml(ISSUE_TEMPLATE / name)
        required_options = [
            option
            for group in _checkboxes(form)
            for option in group["attributes"]["options"]
            if option.get("required") is True
        ]
        assert any(all(item in option["label"] for item in forbidden) for option in required_options)


def test_bug_and_compatibility_forms_collect_reproducible_sanitized_environment_data():
    for name in ("bug.yml", "compatibility.yml"):
        form = _load_yaml(ISSUE_TEMPLATE / name)
        labels = {field["attributes"].get("label", "") for field in form["body"]}
        assert {"操作系统", "Python 版本", "智能体", "脱敏后的 doctor 报告", "原创最小复现"} <= labels
        doctor_field = next(field for field in form["body"] if field["attributes"].get("label") == "脱敏后的 doctor 报告")
        assert "cekit doctor --report" in doctor_field["attributes"]["description"]


def test_issue_template_configuration_disables_unstructured_blank_issues():
    config = _load_yaml(ISSUE_TEMPLATE / "config.yml")
    assert config["blank_issues_enabled"] is False


def test_ci_workflow_runs_the_exact_supported_matrix_and_project_test_command():
    workflow = _load_yaml(WORKFLOWS / "ci.yml")
    assert {"push", "pull_request"} <= set(workflow["on"])
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]["include"]
    assert matrix == [
        {"os": "ubuntu-latest", "python": "3.11"},
        {"os": "ubuntu-latest", "python": "3.13"},
        {"os": "macos-latest", "python": "3.12"},
        {"os": "windows-latest", "python": "3.12"},
    ]
    commands = [step.get("run") for step in workflow["jobs"]["test"]["steps"]]
    assert "python -m pip install -e '.[dev]'" in commands
    assert "python -m pytest -q" in commands


def test_release_audit_workflow_has_release_gate_triggers_and_commands():
    workflow = _load_yaml(WORKFLOWS / "release-audit.yml")
    assert {"push", "pull_request", "workflow_dispatch"} <= set(workflow["on"])
    commands = [step.get("run") for step in workflow["jobs"]["release-audit"]["steps"]]
    assert "python -m pip install -e '.[dev]'" in commands
    assert "cekit release-audit" in commands
    assert "python -m build" in commands
