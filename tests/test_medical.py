"""의료비 세액공제 (소득세법 제59조의4 제2항)."""

import pytest

from ktax.catalog import discover_actions
from ktax.models import Profile
from ktax.tax import medical_expense_credit

YEAR = 2025
SALARY = 50_000_000
FLOOR = 1_500_000          # 총급여 5,000만 × 3%


def patient(**kw) -> Profile:
    base = dict(age=45, earned_income=SALARY)
    base.update(kw)
    return Profile(**base)


# --------------------------------------------------------------------------
# 기준금액
# --------------------------------------------------------------------------

def test_threshold_is_three_percent_of_salary():
    assert medical_expense_credit(patient(medical_expenses=5_000_000), YEAR).threshold == FLOOR


def test_no_credit_below_threshold():
    m = medical_expense_credit(patient(medical_expenses=1_000_000), YEAR)
    assert m.credit == 0


def test_general_expense_credit_above_threshold():
    """(300만 - 150만) × 15% = 22.5만"""
    m = medical_expense_credit(patient(medical_expenses=3_000_000), YEAR)
    assert m.credit == 225_000


# --------------------------------------------------------------------------
# 한도는 '그 밖의 의료비'에만
# --------------------------------------------------------------------------

def test_general_expenses_are_capped():
    """일반 의료비는 3% 초과분이 700만원을 넘어도 700만원까지만."""
    m = medical_expense_credit(patient(medical_expenses=20_000_000), YEAR)
    assert m.eligible_general == 7_000_000
    assert m.credit == round(7_000_000 * 0.15)


def test_unlimited_category_is_not_capped():
    """본인·65세 이상·장애인 의료비에 700만원 한도를 씌우면 크게 과소 계산된다."""
    p = patient(medical_expenses=FLOOR, medical_expenses_unlimited=30_000_000)
    m = medical_expense_credit(p, YEAR)
    assert m.credit == round(30_000_000 * 0.15)
    assert m.credit > round(7_000_000 * 0.15)


def test_cap_applies_only_to_the_general_bucket():
    capped = medical_expense_credit(patient(medical_expenses=20_000_000), YEAR).credit
    uncapped = medical_expense_credit(
        patient(medical_expenses=FLOOR, medical_expenses_unlimited=20_000_000), YEAR
    ).credit
    assert uncapped > capped


# --------------------------------------------------------------------------
# 미달분 차감 — 이 구현의 핵심
# --------------------------------------------------------------------------

def test_shortfall_is_deducted_from_unlimited_bucket():
    """일반 의료비 50만원은 기준선 150만원에 100만원 미달.
    그 100만원을 한도 없는 호에서 뺀다: (800만 - 100만) × 15% = 105만"""
    p = patient(medical_expenses=500_000, medical_expenses_unlimited=8_000_000)
    m = medical_expense_credit(p, YEAR)
    assert m.shortfall_applied == 1_000_000
    assert m.credit == 1_050_000


def test_no_shortfall_when_general_exceeds_threshold():
    p = patient(medical_expenses=3_000_000, medical_expenses_unlimited=5_000_000)
    m = medical_expense_credit(p, YEAR)
    assert m.shortfall_applied == 0
    assert m.credit == round(1_500_000 * 0.15) + round(5_000_000 * 0.15)


def test_full_threshold_deducted_when_no_general_expenses():
    p = patient(medical_expenses=0, medical_expenses_unlimited=5_000_000)
    m = medical_expense_credit(p, YEAR)
    assert m.shortfall_applied == FLOOR
    assert m.credit == round((5_000_000 - FLOOR) * 0.15)


def test_shortfall_consumes_the_lowest_rate_bucket_first():
    """미달분은 공제율이 낮은 호부터 소진시켜야 납세자에게 유리하다.
    15% 호를 먼저 깎고 30% 호를 남긴다."""
    p = patient(medical_expenses=0,
                medical_expenses_unlimited=2_000_000,
                medical_expenses_premature=2_000_000)
    m = medical_expense_credit(p, YEAR)
    # 미달 150만을 15% 호에서 소진 → (200만-150만)×15% + 200만×30%
    assert m.credit == round(500_000 * 0.15) + round(2_000_000 * 0.30)


def test_shortfall_is_applied_only_once_across_buckets():
    """조문은 각 호에 차감 단서를 두지만 중복 차감은 취지에 맞지 않는다."""
    p = patient(medical_expenses=0,
                medical_expenses_unlimited=5_000_000,
                medical_expenses_fertility=5_000_000,
                medical_expenses_premature=5_000_000)
    assert medical_expense_credit(p, YEAR).shortfall_applied == FLOOR


def test_shortfall_cannot_exceed_available_expenses():
    p = patient(medical_expenses=0, medical_expenses_unlimited=500_000)
    m = medical_expense_credit(p, YEAR)
    assert m.shortfall_applied == 500_000
    assert m.credit == 0


# --------------------------------------------------------------------------
# 공제율
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field,rate",
    [
        ("medical_expenses_unlimited", 0.15),
        ("medical_expenses_fertility", 0.20),
        ("medical_expenses_premature", 0.30),
    ],
)
def test_rate_by_category(field, rate):
    p = patient(medical_expenses=FLOOR, **{field: 10_000_000})
    assert medical_expense_credit(p, YEAR).credit == round(10_000_000 * rate)


def test_fertility_and_premature_beat_the_general_rate():
    def credit(field):
        return medical_expense_credit(
            patient(medical_expenses=FLOOR, **{field: 5_000_000}), YEAR
        ).credit

    assert credit("medical_expenses_premature") > credit("medical_expenses_fertility")
    assert credit("medical_expenses_fertility") > credit("medical_expenses_unlimited")


# --------------------------------------------------------------------------
# 근거 노출
# --------------------------------------------------------------------------

def test_rationale_states_the_cap_scope():
    m = medical_expense_credit(patient(medical_expenses=3_000_000), YEAR)
    assert any("한도 없음" in r for r in m.rationale.applied_rules)


def test_rationale_discloses_shortfall_interpretation():
    p = patient(medical_expenses=0, medical_expenses_unlimited=8_000_000)
    m = medical_expense_credit(p, YEAR)
    assert any("1회만 차감" in a for a in m.rationale.assumptions)


def test_no_interpretation_note_when_no_shortfall():
    m = medical_expense_credit(patient(medical_expenses=3_000_000), YEAR)
    assert not any("1회만 차감" in a for a in m.rationale.assumptions)


# --------------------------------------------------------------------------
# 카탈로그
# --------------------------------------------------------------------------

def test_catalog_uses_the_statutory_calculation():
    p = patient(medical_expenses=FLOOR, medical_expenses_unlimited=30_000_000,
                income_deductions=5_000_000)
    action = next(
        a for a in discover_actions(p, YEAR).applicable if a.key == "medical_expense_credit"
    )
    from ktax.tax import tax_credit_saving
    expected = tax_credit_saving(p, YEAR, medical_expense_credit(p, YEAR).credit)
    assert action.benefit.amount_per_year == expected


def test_catalog_rejects_below_threshold_with_the_number():
    p = patient(medical_expenses=500_000)
    reason = next(
        i for i in discover_actions(p, YEAR).ineligible if i.key == "medical_expense_credit"
    ).reason
    assert "1,500,000" in reason
