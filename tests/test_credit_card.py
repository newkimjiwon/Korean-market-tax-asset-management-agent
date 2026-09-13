"""신용카드 등 사용금액 소득공제 (조세특례제한법 제126조의2)."""

import pytest

from ktax.catalog import discover_actions
from ktax.models import Profile, Quadrant
from ktax.scoring import classify_quadrant
from ktax.tax import credit_card_deduction

YEAR = 2025


def spender(**kw) -> Profile:
    base = dict(
        age=35,
        earned_income=50_000_000,
        credit_card_spending=20_000_000,
        debit_cash_spending=5_000_000,
        traditional_market_spending=1_000_000,
        public_transit_spending=500_000,
    )
    base.update(kw)
    return Profile(**base)


# --------------------------------------------------------------------------
# 최저사용금액
# --------------------------------------------------------------------------

def test_minimum_spending_is_a_quarter_of_salary():
    assert credit_card_deduction(spender(), YEAR).minimum_spending == 12_500_000


def test_no_deduction_below_minimum_spending():
    p = spender(credit_card_spending=1_000_000, debit_cash_spending=0,
                traditional_market_spending=0, public_transit_spending=0)
    d = credit_card_deduction(p, YEAR)
    assert d.deductible == 0
    assert "최저사용금액" in d.rationale.summary


def test_minimum_is_consumed_from_the_lowest_rate_first():
    """법정 차감 순서. 안분해서 빼면 공제액이 과소 계산된다.

    신용카드 2,000만 중 1,250만이 차감되고 750만만 15%로 인정,
    체크 500만·전통시장 100만·대중교통 50만은 그대로 살아남는다.
      750만×15% + 500만×30% + 100만×40% + 50만×40% = 322.5만
    """
    assert credit_card_deduction(spender(), YEAR).gross_deductible == 3_225_000


def test_lowest_rate_first_beats_proportional_allocation():
    d = credit_card_deduction(spender(), YEAR)
    total = 26_500_000
    minimum = d.minimum_spending
    proportional = sum(
        round((amt - minimum * amt / total) * rate)
        for amt, rate in (
            (20_000_000, 0.15), (5_000_000, 0.30),
            (1_000_000, 0.40), (500_000, 0.40),
        )
    )
    assert d.gross_deductible > proportional


def test_minimum_spills_into_the_next_bucket_when_credit_card_is_small():
    """신용카드가 최저사용금액에 못 미치면 다음으로 낮은 구간에서 마저 차감된다."""
    p = spender(credit_card_spending=5_000_000, debit_cash_spending=20_000_000,
                traditional_market_spending=0, public_transit_spending=0)
    # 최저 1,250만 = 신용 500만 전부 + 체크에서 750만
    # 남은 체크 1,250만 × 30% = 375만
    assert credit_card_deduction(p, YEAR).gross_deductible == 3_750_000


# --------------------------------------------------------------------------
# 공제율
# --------------------------------------------------------------------------

def test_cash_receipt_shares_the_debit_card_rate():
    """현금영수증은 신용카드가 아니라 체크카드와 같은 30%다."""
    p = spender(credit_card_spending=12_500_000, debit_cash_spending=10_000_000,
                traditional_market_spending=0, public_transit_spending=0)
    assert credit_card_deduction(p, YEAR).gross_deductible == 3_000_000


def test_culture_spending_ignored_above_income_ceiling():
    low = spender(earned_income=70_000_000, culture_spending=2_000_000,
                  credit_card_spending=17_500_000)
    high = spender(earned_income=70_000_001, culture_spending=2_000_000,
                   credit_card_spending=17_500_001)
    assert credit_card_deduction(low, YEAR).gross_deductible > 0
    # 문화체육이 아예 계산에서 빠지므로 같은 지출이어도 공제대상이 줄어든다
    assert (
        credit_card_deduction(high, YEAR).gross_deductible
        < credit_card_deduction(low, YEAR).gross_deductible
    )


# --------------------------------------------------------------------------
# 한도
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "salary,children,expected",
    [
        (50_000_000, 0, 3_000_000),
        (50_000_000, 1, 3_000_000),
        (50_000_000, 2, 3_000_000),
        (50_000_000, 3, 3_000_000),   # 2명 이상은 동일
        (80_000_000, 0, 2_500_000),
        (80_000_000, 1, 2_500_000),
        (80_000_000, 2, 2_500_000),
    ],
)
def test_2025_base_limit_does_not_apply_2026_child_increase(salary, children, expected):
    p = spender(earned_income=salary, dependent_children=children)
    assert credit_card_deduction(p, YEAR).base_limit == expected


def test_extra_limit_depends_on_income():
    assert credit_card_deduction(spender(earned_income=70_000_000), YEAR).extra_limit == 3_000_000
    assert credit_card_deduction(spender(earned_income=80_000_000), YEAR).extra_limit == 2_000_000


def test_deduction_never_exceeds_combined_limits():
    p = spender(credit_card_spending=100_000_000, debit_cash_spending=50_000_000,
                traditional_market_spending=20_000_000, public_transit_spending=20_000_000)
    d = credit_card_deduction(p, YEAR)
    assert d.deductible <= d.base_limit + d.extra_limit


def test_extra_limit_only_applies_to_market_transit_culture():
    """추가한도는 전통시장·대중교통·문화체육에서 나온 공제액까지만 인정된다."""
    p = spender(credit_card_spending=60_000_000, debit_cash_spending=30_000_000,
                traditional_market_spending=0, public_transit_spending=0)
    d = credit_card_deduction(p, YEAR)
    assert d.gross_deductible > d.base_limit
    assert d.deductible == d.base_limit   # 추가 대상 지출이 없으므로 한도에서 멈춤


# --------------------------------------------------------------------------
# 카탈로그 액션
# --------------------------------------------------------------------------

def found(p, key="payment_method_switch"):
    return next((a for a in discover_actions(p, YEAR).applicable if a.key == key), None)


def blocked(p, key="payment_method_switch"):
    return next((i for i in discover_actions(p, YEAR).ineligible if i.key == key), None)


def test_switch_action_is_set_and_forget():
    a = found(spender())
    assert a is not None
    assert classify_quadrant(a.effort_recurrence, a.benefit.recurrence) is Quadrant.SET_AND_FORGET


def test_switch_action_absent_when_credit_card_already_at_minimum():
    p = spender(credit_card_spending=12_500_000, debit_cash_spending=12_500_000)
    assert found(p) is None
    assert "더 옮길 금액이 없습니다" in blocked(p).reason


def test_switch_action_absent_below_minimum_spending():
    p = spender(credit_card_spending=2_000_000, debit_cash_spending=0,
                traditional_market_spending=0, public_transit_spending=0)
    assert "최저사용금액" in blocked(p).reason


def test_switch_action_absent_when_already_at_limit():
    p = spender(credit_card_spending=80_000_000, debit_cash_spending=40_000_000,
                traditional_market_spending=10_000_000, public_transit_spending=10_000_000)
    assert "한도에 도달" in blocked(p).reason


def test_switch_benefit_matches_the_deduction_delta():
    from dataclasses import replace
    from ktax.tax import income_deduction_saving

    p = spender()
    current = credit_card_deduction(p, YEAR)
    minimum = current.minimum_spending
    movable = p.credit_card_spending - minimum
    optimal = credit_card_deduction(
        replace(p, credit_card_spending=minimum,
                debit_cash_spending=p.debit_cash_spending + movable), YEAR
    )
    expected = income_deduction_saving(p, YEAR, optimal.deductible - current.deductible)
    assert found(p).benefit.amount_per_year == expected


def test_non_earner_is_ineligible():
    p = spender(earned_income=0, business_income=50_000_000)
    assert "근로소득자만" in blocked(p).reason
