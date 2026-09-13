"""에이전트가 쓰는 표면.

이 도구들의 사용자는 사람이 아니라 에이전트다. 그래서 검증 기준도 다르다 —
계산이 맞는가가 아니라, 사전 지식 없는 에이전트가 이것만 보고 올바른 다음
수를 둘 수 있는가.
"""

import pytest

from ktax.server import (
    check_thresholds,
    describe_profile_fields,
    estimate_tax_liability,
    recommend_actions,
)

YEAR = 2025
MINIMAL = {"age": 33, "earned_income": 52_000_000}


# --------------------------------------------------------------------------
# 위험한 동작이 기본값이면 안 된다
# --------------------------------------------------------------------------

def test_absent_fields_are_unknown_not_zero():
    """예전에는 인자 하나를 깜빡하면 7개 액션이 거짓 '자격 미달'로 나갔다.

    에이전트는 그 사유를 사실로 사용자에게 전달한다. 물어본 적도 없는
    '무주택 세대주가 아니십니다' 같은 말이 나가는 셈이다.
    """
    r = recommend_actions(MINIMAL, YEAR)
    assert r["ineligible"] == []
    assert r["indeterminate"]


def test_explicit_zero_is_a_real_answer():
    """확인해서 0이면 자격 미달이 맞다."""
    profile = {**MINIMAL, "annual_rent_paid": 0, "is_homeless_household_head": True}
    r = recommend_actions(profile, YEAR)

    assert "monthly_rent_credit" in {i["key"] for i in r["ineligible"]}
    assert "monthly_rent_credit" not in {i["key"] for i in r["indeterminate"]}


def test_assume_complete_is_opt_in():
    """모든 값을 안다고 보는 것은 명시적으로만 가능해야 한다."""
    assert recommend_actions(MINIMAL, YEAR, assume_complete=True)["indeterminate"] == []
    assert recommend_actions(MINIMAL, YEAR)["indeterminate"]


def test_explicit_known_fields_still_honoured():
    r = recommend_actions(MINIMAL, YEAR, known_fields=list(MINIMAL))
    assert r["indeterminate"]


def test_thresholds_disclose_what_was_assumed_zero():
    t = check_thresholds(MINIMAL, YEAR)
    assert "financial_income" in t["assumed_zero"]
    assert t["signals"]


def test_thresholds_stop_assuming_once_told():
    t = check_thresholds({**MINIMAL, "financial_income": 0}, YEAR)
    assert "financial_income" not in t["assumed_zero"]


# --------------------------------------------------------------------------
# 에러는 다음 수를 알려줘야 한다
# --------------------------------------------------------------------------

def test_unknown_field_names_the_discovery_tool():
    with pytest.raises(ValueError) as exc:
        recommend_actions({"age": 33, "salary": 50_000_000}, YEAR)
    assert "describe_profile_fields" in str(exc.value)


def test_unknown_field_suggests_a_close_match():
    with pytest.raises(ValueError) as exc:
        recommend_actions({"age": 33, "earned_incom": 50_000_000}, YEAR)
    assert "earned_income" in str(exc.value)


def test_missing_age_says_what_to_ask():
    with pytest.raises(ValueError) as exc:
        recommend_actions({"earned_income": 50_000_000}, YEAR)
    message = str(exc.value)
    assert "나이" in message and "물어" in message


def test_bad_filing_type_lists_the_options():
    with pytest.raises(ValueError) as exc:
        estimate_tax_liability({"age": 33, "filing_type": "freelance"}, YEAR)
    assert "earned" in str(exc.value) and "comprehensive" in str(exc.value)


def test_errors_do_not_leak_python_internals():
    for bad in ({"age": 33, "salary": 1}, {"earned_income": 1}):
        with pytest.raises(ValueError) as exc:
            recommend_actions(bad, YEAR)
        assert "__init__" not in str(exc.value)


# --------------------------------------------------------------------------
# 무엇을 물어야 하는지 스스로 알 수 있어야 한다
# --------------------------------------------------------------------------

def test_field_discovery_covers_every_profile_field():
    from dataclasses import fields

    from ktax.models import Profile

    described = {f["field"] for f in describe_profile_fields()["fields"]}
    assert described == {f.name for f in fields(Profile)}


def test_field_discovery_can_narrow_to_what_is_missing():
    d = describe_profile_fields(only_missing_from=MINIMAL)
    names = {f["field"] for f in d["fields"]}
    assert "age" not in names and "earned_income" not in names


def test_every_field_carries_a_question_and_sources():
    for entry in describe_profile_fields()["fields"]:
        assert entry["question"]
        assert entry["sources"]
        assert "label" in entry


def test_user_only_fields_are_flagged_for_asking():
    entries = {f["field"]: f for f in describe_profile_fields()["fields"]}
    assert entries["is_homeless_household_head"]["user_only"] is True
    assert entries["donations"]["user_only"] is False


def test_indeterminate_carries_the_question_to_ask():
    r = recommend_actions(MINIMAL, YEAR)
    steps = [s for item in r["indeterminate"] for s in item["how_to_fill"]]
    assert steps
    assert all(s["question"] and s["sources"] for s in steps)


def test_missing_fields_is_a_flat_actionable_list():
    r = recommend_actions(MINIMAL, YEAR)
    assert r["missing_fields"]
    assert len(r["missing_fields"]) == len(set(r["missing_fields"]))


# --------------------------------------------------------------------------
# 냉시작 흐름
# --------------------------------------------------------------------------

def test_progressive_disclosure_converges():
    """값을 채울수록 판단불가가 줄고 결론이 늘어야 한다."""
    profile = dict(MINIMAL)
    before = recommend_actions(profile, YEAR)

    profile.update({
        "is_homeless_household_head": True,
        "housing_subscription_contributed": 1_200_000,
        "annual_rent_paid": 0,
    })
    after = recommend_actions(profile, YEAR)

    assert len(after["indeterminate"]) < len(before["indeterminate"])
    assert len(after["actions"]) + len(after["ineligible"]) > 0


def test_server_instructions_warn_about_indeterminate():
    from ktax.server import server

    assert server.instructions
    assert "indeterminate" in server.instructions


def test_isa_response_cannot_be_confused_with_salary_settlement():
    from ktax.server import simulate_isa

    result = simulate_isa({"age": 40}, YEAR, 20_000_000, 0.05,
                          holding_years=4, existing_net_gain=2_000_000)
    assert result["tax_scope"] == "investment_holding_period"
    assert result["holding_years"] == 4
    assert result["projected_gain"] == 4_000_000
    assert result["total_saving"] == 220_000
    assert result["isa_account_tax"] == 396_000
    assert result["benefit"]["starts_in_years"] == 4
    assert not {"annual_saving", "baseline_total", "simulated_total"} & result.keys()


@pytest.mark.parametrize("bad", [
    {"age": True}, {"earned_income": -1}, {"earned_income": "50000000"},
    {"tax_credits": float("nan")}, {"is_homeless_household_head": "false"},
    {"dependent_children": -1}, {"isa_contributed_total": 1.5},
])
def test_mcp_rejects_malformed_profile_scalars(bad):
    with pytest.raises(ValueError):
        recommend_actions({"age": 40, **bad}, YEAR)
