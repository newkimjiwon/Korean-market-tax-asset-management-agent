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


def test_2025_ruleset_is_verified():
    """검증을 마친 연도는 출처 기록을 갖는다."""
    rules = load_ruleset(YEAR)
    assert rules["verified"] is True
    assert rules["verification"]["sources"]
    assert len(rules["verification"]["checked"]) >= 10


def test_verified_ruleset_emits_no_warning():
    p = Profile(age=40, earned_income=60_000_000)
    assert estimate_tax(p, YEAR).rationale.warnings() == []


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


def test_isa_saving_is_zero_within_tax_free_limit():
    """수익이 비과세 한도 안이면 ISA 절감액은 원천징수분 전액이다."""
    p = Profile(age=40, earned_income=60_000_000)
    sim = simulate_isa_contribution(p, YEAR, 20_000_000, expected_return_rate=0.05)
    gain = 1_000_000  # 비과세 한도 200만원 이하
    assert sim.annual_saving == round(gain * 0.154)


def test_isa_respects_annual_limit():
    p = Profile(age=40, earned_income=60_000_000, isa_contributed_this_year=20_000_000)
    sim = simulate_isa_contribution(p, YEAR, 10_000_000, expected_return_rate=0.05)
    assert sim.annual_saving == 0
