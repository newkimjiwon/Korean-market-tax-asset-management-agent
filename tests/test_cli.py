"""CLI 연기 테스트. 시나리오 파일이 끝까지 흐르는지 확인한다."""

import json
from pathlib import Path

import pytest

from ktax.__main__ import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.mark.parametrize("name", ["salaried_33.json", "collected_partial.json"])
def test_examples_run(name, capsys):
    assert main([str(EXAMPLES / name)]) == 0
    out = capsys.readouterr().out
    assert "세액 계산" in out
    assert "임계값 감시" in out


@pytest.mark.parametrize("view", ["value", "quick_wins", "set_and_forget", "urgent"])
def test_every_view_runs(view, capsys):
    assert main([str(EXAMPLES / "salaried_33.json"), "--view", view]) == 0
    assert f"정렬: {view}" in capsys.readouterr().out


def test_json_output_is_machine_readable(capsys):
    main([str(EXAMPLES / "salaried_33.json"), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["tax"]["total"] > 0
    assert payload["actions"]
    assert "rationale" in payload["actions"][0]


def test_source_scenario_reports_gaps(capsys):
    main([str(EXAMPLES / "collected_partial.json"), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["indeterminate"], "자료 부족 항목이 보고되어야 한다"
    assert all(item["how_to_fill"] for item in payload["indeterminate"])


def test_known_absent_is_ineligible_not_indeterminate(capsys):
    """null 로 답한 값은 '모름'이 아니라 '없음'이다."""
    main([str(EXAMPLES / "collected_partial.json"), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert "sme_employment_reduction" in {i["key"] for i in payload["ineligible"]}
    assert "sme_employment_reduction" not in {i["key"] for i in payload["indeterminate"]}


def test_past_deadline_is_not_shown_as_negative_countdown(capsys):
    main([str(EXAMPLES / "salaried_33.json")])
    out = capsys.readouterr().out
    assert "D--" not in out
