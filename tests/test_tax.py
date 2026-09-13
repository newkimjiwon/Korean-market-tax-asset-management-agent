import pytest

from ktax.models import FilingType, Profile
from ktax.rules import load_ruleset
from ktax.tax import (
    estimate_tax,
    gross_income_tax,
    marginal_rate,
    simulate_isa_contribution,
    simulate_pension_contribution,
)

YEAR = 2025


def test_bracket_boundaries_are_continuous():
    """누진공제 값이 맞다면 구간 경계에서 세액이 튀지 않아야 한다."""
    rules = load_ruleset(YEAR)
    for bracket in rules["income_tax_brackets"]:
        upper = bracket["upper"]
        if upper is None:
            continue
        at = gross_income_tax(upper, YEAR)
        just_after = gross_income_tax(upper + 1, YEAR)
        assert abs(just_after - at) < 10, f"{upper:,}원 경계에서 세액 불연속"


def test_gross_income_tax_lowest_bracket():
    assert gross_income_tax(10_000_000, YEAR) == 600_000


def test_gross_income_tax_second_bracket():
    # 5천만원 × 15% - 126만원
    assert gross_income_tax(50_000_000, YEAR) == 6_240_000


def test_marginal_rate_moves_with_base():
    assert marginal_rate(10_000_000, YEAR) == 0.06
    assert marginal_rate(50_000_000, YEAR) == 0.15
    assert marginal_rate(90_000_000, YEAR) == 0.35


def test_estimate_tax_includes_local_tax():
    p = Profile(age=40, earned_income=60_000_000, income_deductions=10_000_000)
    est = estimate_tax(p, YEAR)
    assert est.taxable_base == 50_000_000
    assert est.local_income_tax == round(est.income_tax * 0.1)
    assert est.total == est.income_tax + est.local_income_tax


def test_estimate_tax_never_negative():
    p = Profile(age=30, earned_income=20_000_000, tax_credits=99_999_999)
    assert estimate_tax(p, YEAR).income_tax == 0


def test_2025_ruleset_requires_recheck_after_discovered_data_error():
    """일부 항목 재검증을 전체 연도의 검증 완료로 확대하지 않는다."""
    rules = load_ruleset(YEAR)
    assert rules["verified"] is False
    assert rules["verification"]["status"] == "partial_recheck"
    assert rules["verification"]["rechecked"]["medical_expense"]["sources"]
    assert rules["verification"]["sources"]
    assert len(rules["verification"]["checked"]) >= 10


def test_ruleset_under_recheck_warns_in_actual_calculation():
    p = Profile(age=40, earned_income=60_000_000)
    assert estimate_tax(p, YEAR).rationale.warnings()


def test_unverified_ruleset_still_warns():
    """경고 메커니즘은 아직 검증하지 않은 연도를 위해 계속 살아 있어야 한다."""
    from ktax.models import Rationale

    r = Rationale(summary="x", ruleset_year=2099, ruleset_verified=False)
    assert r.warnings()
    assert "2099" in r.warnings()[0]


@pytest.mark.parametrize(
    "taxable_base,statutory_tax",
    [
        # 소득세법 제55조의 누적액 형태를 그대로 옮긴 값.
        # 검증을 마친 수치이므로 부주의한 수정이 여기서 걸리게 고정한다.
        (14_000_000, 840_000),
        (50_000_000, 6_240_000),
        (88_000_000, 15_360_000),
        (150_000_000, 37_060_000),
        (300_000_000, 94_060_000),
        (500_000_000, 174_060_000),
        (1_000_000_000, 384_060_000),
    ],
)
def test_brackets_match_statute(taxable_base, statutory_tax):
    assert gross_income_tax(taxable_base, YEAR) == statutory_tax


def test_pension_credit_rate_switches_on_income():
    low = Profile(age=35, earned_income=50_000_000)
    high = Profile(age=35, earned_income=80_000_000)

    low_sim = simulate_pension_contribution(low, YEAR, 6_000_000)
    high_sim = simulate_pension_contribution(high, YEAR, 6_000_000)

    assert low_sim.annual_saving > high_sim.annual_saving
    assert low_sim.annual_saving == round(6_000_000 * 0.15 * 1.1)
    assert high_sim.annual_saving == round(6_000_000 * 0.12 * 1.1)


def test_pension_contribution_capped_at_limit():
    p = Profile(age=35, earned_income=50_000_000, irp_contributed=9_000_000)
    sim = simulate_pension_contribution(p, YEAR, 6_000_000)
    assert sim.annual_saving == 0
    assert any("한도 여유" in a for a in sim.rationale.assumptions)


def test_pension_benefit_ends_at_withdrawal_age():
    p = Profile(age=35, earned_income=50_000_000)
    sim = simulate_pension_contribution(p, YEAR, 6_000_000)
    assert sim.benefit.ends_at_age == 55


def test_isa_applies_exemption_once_over_holding_period():
    sim = simulate_isa_contribution(Profile(age=40), YEAR, 20_000_000, 0.05)
    assert sim.projected_gain == 3_000_000
    assert sim.normal_account_tax == 462_000
    assert sim.isa_account_tax == 99_000
    assert sim.total_saving == 363_000
    assert sim.benefit.recurrence.value == "one_time"
    assert sim.benefit.starts_in_years == 3
    assert sim.benefit.explicit_years is None


def test_isa_is_independent_of_current_income_tax():
    empty = simulate_isa_contribution(Profile(age=40), YEAR, 20_000_000, 0.05)
    salaried = simulate_isa_contribution(Profile(age=40, earned_income=80_000_000), YEAR, 20_000_000, 0.05)
    assert empty == salaried
    assert empty.isa_account_tax >= 0


def test_isa_existing_profit_uses_exemption():
    sim = simulate_isa_contribution(Profile(age=40), YEAR, 20_000_000, 0.05, existing_net_gain=2_000_000)
    assert sim.isa_account_tax == 297_000
    assert sim.total_saving == 165_000


def test_isa_existing_loss_offsets_incremental_gain():
    sim = simulate_isa_contribution(Profile(age=40), YEAR, 20_000_000, 0.05, existing_net_gain=-1_000_000)
    assert sim.isa_account_tax == 0
    assert sim.total_saving == 462_000


def test_isa_respects_annual_limit():
    p = Profile(age=40, isa_contributed_this_year=20_000_000)
    sim = simulate_isa_contribution(p, YEAR, 10_000_000, 0.05)
    assert sim.total_saving == 0


@pytest.mark.parametrize("kwargs", [
    {"additional_contribution": -1}, {"additional_contribution": True},
    {"expected_return_rate": float("nan")}, {"expected_return_rate": float("inf")},
    {"expected_return_rate": -0.1}, {"expected_return_rate": True},
    {"holding_years": 2}, {"holding_years": True}, {"existing_net_gain": 1.5},
])
def test_isa_rejects_invalid_forecast_inputs(kwargs):
    inputs = dict(additional_contribution=1_000_000, expected_return_rate=0.05)
    inputs.update(kwargs)
    with pytest.raises(ValueError):
        simulate_isa_contribution(Profile(age=40), YEAR, **inputs)


def test_pension_cannot_refund_more_than_remaining_tax():
    p=Profile(age=40, earned_income=1_000_000)
    sim=simulate_pension_contribution(p,YEAR,9_000_000)
    assert sim.baseline_total==66_000
    assert sim.annual_saving==66_000
    assert sim.simulated_total==0


def test_pension_does_not_offset_separately_taxed_financial_income():
    p=Profile(age=40,earned_income=0,financial_income=10_000_000)
    sim=simulate_pension_contribution(p,YEAR,9_000_000)
    assert sim.annual_saving==0
    assert sim.simulated_total==1_540_000


def test_excess_pension_savings_does_not_consume_irp_credit_room():
    p=Profile(age=40,earned_income=60_000_000,pension_savings_contributed=8_000_000)
    sim=simulate_pension_contribution(p,YEAR,3_000_000)
    assert sim.annual_saving==396_000  # IRP 300만원 × 12% × 1.1
    assert any('IRP 3,000,000원' in x for x in sim.rationale.assumptions)


@pytest.mark.parametrize('amount',[-1,True,1.5])
def test_invalid_additional_pension_contribution_rejected(amount):
    with pytest.raises(ValueError):
        simulate_pension_contribution(Profile(age=40),YEAR,amount)


def test_income_deduction_does_not_save_tax_already_eliminated_by_credits():
    from ktax.tax import income_deduction_saving
    p=Profile(age=40,earned_income=10_000_000,tax_credits=600_000)
    assert income_deduction_saving(p,YEAR,1_000_000)==0


def test_income_deduction_respects_financial_comparative_tax_floor():
    from ktax.tax import income_deduction_saving
    p=Profile(age=40,financial_income=25_000_000)
    assert income_deduction_saving(p,YEAR,1_000_000)==0
