"""소득세법 제62조 — 이자소득 등에 대한 종합과세 시 세액 계산의 특례."""

import pytest

from ktax.models import Profile
from ktax.monitor import Severity, evaluate_thresholds
from ktax.tax import estimate_tax, gross_income_tax_for, taxable_base

YEAR = 2025
THRESHOLD = 20_000_000


# --------------------------------------------------------------------------
# 기준금액 이하 — 분리과세 종결
# --------------------------------------------------------------------------

def test_below_threshold_is_not_added_to_comprehensive_income():
    p = Profile(age=50, earned_income=60_000_000, income_deductions=10_000_000,
                financial_income=15_000_000)
    assert taxable_base(p, YEAR) == 50_000_000
    assert gross_income_tax_for(p, YEAR) == (6_240_000, "separate")


def test_exactly_at_threshold_stays_separate():
    """'초과'가 기준이므로 2,000만원 정확히는 분리과세다."""
    p = Profile(age=50, earned_income=60_000_000, income_deductions=10_000_000,
                financial_income=THRESHOLD)
    assert gross_income_tax_for(p, YEAR)[1] == "separate"


def test_one_won_over_threshold_switches_to_comprehensive():
    p = Profile(age=50, earned_income=60_000_000, income_deductions=10_000_000,
                financial_income=THRESHOLD + 1)
    assert gross_income_tax_for(p, YEAR)[1] != "separate"


def test_no_cliff_in_total_burden_at_the_threshold():
    """경계를 1원 넘었다고 총 세부담이 급등하면 안 된다.

    산출세액만 보면 불연속이다 — 기준금액 이하에서는 금융소득세가
    원천징수로 끝나 산출세액에 잡히지 않다가, 초과하면 그 금액이
    산출세액 안으로 들어오기 때문이다. 총 세부담으로 봐야 연속이다.
    """
    def burden(fin):
        return estimate_tax(
            Profile(age=50, earned_income=60_000_000,
                    income_deductions=10_000_000, financial_income=fin), YEAR
        ).total

    assert abs(burden(THRESHOLD + 1) - burden(THRESHOLD)) < 1_000


def test_separate_financial_tax_is_reported_separately():
    """분리과세분은 세액공제 대상이 아니므로 종합분과 구분해 담는다."""
    p = Profile(age=50, earned_income=60_000_000, income_deductions=10_000_000,
                financial_income=15_000_000)
    est = estimate_tax(p, YEAR)
    assert est.separate_financial_tax == round(15_000_000 * 0.14)
    assert est.comprehensive_tax == 6_240_000
    assert est.income_tax == est.comprehensive_tax + est.separate_financial_tax


def test_separate_financial_tax_is_zero_once_comprehensive():
    """종합과세로 넘어가면 금융소득세는 이미 산출세액 안에 있다. 두 번 더하면 안 된다."""
    p = Profile(age=50, earned_income=60_000_000, income_deductions=10_000_000,
                financial_income=50_000_000)
    assert estimate_tax(p, YEAR).separate_financial_tax == 0


def test_tax_credits_do_not_offset_separately_taxed_financial_income():
    """세액공제는 종합소득 산출세액에서만 차감된다."""
    p = Profile(age=50, earned_income=60_000_000, income_deductions=10_000_000,
                financial_income=15_000_000, tax_credits=99_999_999)
    est = estimate_tax(p, YEAR)
    assert est.comprehensive_tax == 0
    assert est.separate_financial_tax > 0


# --------------------------------------------------------------------------
# 초과 — 일반산출세액과 비교산출세액
# --------------------------------------------------------------------------

def test_general_method_matches_hand_calculation():
    """2,000만×14% + 세율표(다른 과세표준 5,000만 + 초과분 3,000만 = 8,000만)
    = 280만 + (8,000만×24% - 576만) = 1,624만"""
    p = Profile(age=50, earned_income=60_000_000, income_deductions=10_000_000,
                financial_income=50_000_000)
    assert taxable_base(p, YEAR) == 80_000_000
    assert gross_income_tax_for(p, YEAR) == (16_240_000, "general")


def test_comparative_method_wins_when_other_income_is_low():
    """다른 소득이 적으면 종합과세가 원천징수보다 가벼워진다.
    비교산출세액이 그 역전을 막는다: 2,500만 × 14% = 350만"""
    p = Profile(age=65, financial_income=25_000_000)
    assert gross_income_tax_for(p, YEAR) == (3_500_000, "comparative")


def test_result_is_never_below_withholding_equivalent():
    """제62조의 목적 — 최소한 원천징수 수준은 부담해야 한다."""
    for fin in (21_000_000, 40_000_000, 100_000_000, 500_000_000):
        for other in (0, 30_000_000, 120_000_000):
            p = Profile(age=50, earned_income=other, financial_income=fin)
            gross, _ = gross_income_tax_for(p, YEAR)
            floor = round(fin * 0.14)
            assert gross >= floor, f"금융 {fin:,}/근로 {other:,} 에서 원천징수 미만"


def test_picks_the_larger_of_the_two_methods():
    from ktax.tax import gross_income_tax

    p = Profile(age=50, earned_income=60_000_000, income_deductions=10_000_000,
                financial_income=50_000_000)
    other_base = p.non_financial_taxable_base()
    general = round(THRESHOLD * 0.14) + gross_income_tax(
        other_base + (p.financial_income - THRESHOLD), YEAR)
    comparative = round(p.financial_income * 0.14) + gross_income_tax(other_base, YEAR)

    assert gross_income_tax_for(p, YEAR)[0] == max(general, comparative)


def test_more_financial_income_never_lowers_tax():
    prev = 0
    for fin in range(0, 120_000_001, 10_000_000):
        p = Profile(age=50, earned_income=60_000_000,
                    income_deductions=10_000_000, financial_income=fin)
        gross = gross_income_tax_for(p, YEAR)[0]
        assert gross >= prev, f"금융소득 {fin:,}에서 세액이 감소했다"
        prev = gross


# --------------------------------------------------------------------------
# 이전 구현이 놓치던 것
# --------------------------------------------------------------------------

def test_financial_only_taxpayer_is_no_longer_taxed_at_zero():
    """다른 소득이 없으면 이전 구현은 세금을 0원으로 봤다."""
    p = Profile(age=70, financial_income=25_000_000)
    assert estimate_tax(p, YEAR).income_tax > 0


def test_estimate_reports_which_method_applied():
    low = Profile(age=50, earned_income=60_000_000, financial_income=10_000_000)
    high = Profile(age=50, earned_income=60_000_000, financial_income=50_000_000)

    assert estimate_tax(low, YEAR).financial_income_taxation == "separate"
    assert estimate_tax(high, YEAR).financial_income_taxation == "general"


def test_rationale_explains_comprehensive_taxation():
    p = Profile(age=50, earned_income=60_000_000, financial_income=50_000_000)
    r = estimate_tax(p, YEAR).rationale
    assert any("제62조" in rule for rule in r.applied_rules)
    assert any("Gross-up" in a for a in r.assumptions), "미반영 항목을 밝혀야 한다"


def test_rationale_notes_separate_taxation_when_below_threshold():
    p = Profile(age=50, earned_income=60_000_000, financial_income=10_000_000)
    r = estimate_tax(p, YEAR).rationale
    assert any("분리과세 종결" in rule for rule in r.applied_rules)
    assert not any("Gross-up" in a for a in r.assumptions)


# --------------------------------------------------------------------------
# 감시 레이어와의 정합성
# --------------------------------------------------------------------------

def test_bracket_edge_uses_the_combined_base():
    """금융소득 합산 후 과세표준으로 구간 경계를 봐야 한다."""
    p = Profile(age=50, earned_income=40_000_000, income_deductions=10_000_000,
                financial_income=30_000_000)
    edge = next(s for s in evaluate_thresholds(p, YEAR) if s.key == "tax_bracket_edge")
    # 과세표준 = 3,000만 + 초과분 1,000만 = 4,000만 → 5,000만 구간까지 1,000만 남음
    assert taxable_base(p, YEAR) == 40_000_000
    assert edge.headroom == 10_000_000


def test_threshold_signal_and_tax_calculation_agree():
    p = Profile(age=50, earned_income=60_000_000, financial_income=25_000_000)
    signal = next(s for s in evaluate_thresholds(p, YEAR)
                  if s.key == "financial_income_comprehensive")
    assert signal.severity is Severity.ACT
    assert estimate_tax(p, YEAR).financial_income_taxation != "separate"
