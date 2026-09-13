import pytest

from ktax.catalog import Discovery, Ineligible, catalog_keys, discover_actions
from ktax.models import Profile, Quadrant, Recurrence
from ktax.scoring import classify_quadrant, score_actions, set_and_forget, sort_by_value

YEAR = 2025


def young_renter(**overrides) -> Profile:
    base = dict(
        age=29,
        earned_income=38_000_000,
        income_deductions=8_000_000,
        is_homeless_household_head=True,
        annual_rent_paid=7_200_000,
    )
    base.update(overrides)
    return Profile(**base)


def found(discovery: Discovery, key: str):
    return next((a for a in discovery.applicable if a.key == key), None)


def blocked(discovery: Discovery, key: str) -> Ineligible | None:
    return next((i for i in discovery.ineligible if i.key == key), None)


# --------------------------------------------------------------------------
# 레지스트리
# --------------------------------------------------------------------------

def test_catalog_is_populated():
    assert len(catalog_keys()) >= 7


def test_every_entry_is_classified_as_applicable_or_ineligible():
    """조용히 사라지는 액션이 없어야 한다."""
    d = discover_actions(young_renter(), YEAR)
    assert len(d.applicable) + len(d.ineligible) == len(catalog_keys())


def test_ineligible_entries_always_carry_a_reason():
    d = discover_actions(Profile(age=45, earned_income=200_000_000), YEAR)
    assert d.ineligible
    assert all(i.reason.strip() for i in d.ineligible)


# --------------------------------------------------------------------------
# 자격 판정
# --------------------------------------------------------------------------

def test_housing_subscription_requires_homeless_household_head():
    d = discover_actions(young_renter(is_homeless_household_head=False), YEAR)
    assert found(d, "housing_subscription") is None
    assert "무주택" in blocked(d, "housing_subscription").reason


def test_housing_subscription_blocked_over_income_ceiling():
    d = discover_actions(young_renter(earned_income=80_000_000), YEAR)
    assert "총급여" in blocked(d, "housing_subscription").reason


def test_monthly_rent_requires_actual_rent():
    d = discover_actions(young_renter(annual_rent_paid=0), YEAR)
    assert blocked(d, "monthly_rent_credit") is not None


def test_medical_expense_below_floor_is_rejected_with_the_number():
    """총급여 3% 기준선에 못 미치면 사유에 기준선 금액이 담겨야 한다."""
    p = young_renter(medical_expenses=500_000)   # 3,800만 × 3% = 114만
    d = discover_actions(p, YEAR)
    reason = blocked(d, "medical_expense_credit").reason
    assert "1,140,000" in reason


def test_medical_expense_above_floor_is_applicable():
    d = discover_actions(young_renter(medical_expenses=3_000_000), YEAR)
    assert found(d, "medical_expense_credit") is not None


def test_isa_needs_financial_income():
    d = discover_actions(young_renter(), YEAR)
    assert "금융소득" in blocked(d, "isa_account").reason

    d2 = discover_actions(young_renter(financial_income=3_000_000), YEAR)
    assert found(d2, "isa_account") is not None


def test_pension_blocked_when_limit_exhausted():
    d = discover_actions(young_renter(irp_contributed=9_000_000), YEAR)
    assert "한도" in blocked(d, "pension_account").reason


# --------------------------------------------------------------------------
# 중소기업 감면 — 동적 지평이 가장 잘 드러나는 항목
# --------------------------------------------------------------------------

def test_sme_reduction_requires_employment_record():
    d = discover_actions(young_renter(), YEAR)
    assert blocked(d, "sme_employment_reduction") is not None


def test_sme_remaining_years_shrink_each_year():
    """같은 사람도 해가 갈수록 남은 감면 기간이 줄어 가치가 떨어진다."""
    p = young_renter(sme_employment_start_year=2023)
    first = found(discover_actions(p, 2025), "sme_employment_reduction")
    assert first.benefit.explicit_years == 3   # 청년 5년 중 2년 경과


def test_sme_reduction_expires():
    p = young_renter(age=32, sme_employment_start_year=2019)
    d = discover_actions(p, YEAR)
    assert "종료" in blocked(d, "sme_employment_reduction").reason


def test_sme_age_gate_excludes_non_youth_without_special_category():
    p = young_renter(age=45, sme_employment_start_year=2024)
    assert blocked(discover_actions(p, YEAR), "sme_employment_reduction") is not None

    p2 = young_renter(age=45, sme_employment_start_year=2024, sme_special_category=True)
    assert found(discover_actions(p2, YEAR), "sme_employment_reduction") is not None


def test_sme_saving_is_capped():
    """고소득이어도 연 감면 한도를 넘지 않는다."""
    p = young_renter(
        age=30, earned_income=90_000_000, sme_employment_start_year=2024
    )
    a = found(discover_actions(p, YEAR), "sme_employment_reduction")
    assert a.benefit.amount_per_year <= round(2_000_000 * 1.1)


# --------------------------------------------------------------------------
# 사분면
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "key,expected",
    [
        ("pension_account", Quadrant.SET_AND_FORGET),
        ("housing_subscription", Quadrant.SET_AND_FORGET),
        ("sme_employment_reduction", Quadrant.SET_AND_FORGET),
        ("monthly_rent_credit", Quadrant.MAINTENANCE),
        ("medical_expense_credit", Quadrant.MAINTENANCE),
        ("donation_credit", Quadrant.MAINTENANCE),
    ],
)
def test_quadrant_assignment(key, expected):
    p = young_renter(
        medical_expenses=3_000_000,
        donations=600_000,
        sme_employment_start_year=2023,
        financial_income=3_000_000,
    )
    action = found(discover_actions(p, YEAR), key)
    assert action is not None, f"{key} 가 적용 가능해야 한다"
    assert classify_quadrant(action.effort_recurrence, action.benefit.recurrence) is expected


def test_set_and_forget_view_excludes_recurring_effort():
    p = young_renter(medical_expenses=3_000_000, sme_employment_start_year=2023)
    d = discover_actions(p, YEAR)
    keys = {s.action.key for s in set_and_forget(score_actions(d.applicable, p))}
    assert "medical_expense_credit" not in keys
    assert "pension_account" in keys


# --------------------------------------------------------------------------
# 공제 유형
# --------------------------------------------------------------------------

def test_income_deduction_and_tax_credit_are_not_conflated():
    """같은 금액이라도 소득공제와 세액공제의 절감액은 크게 다르다."""
    from ktax.tax import income_deduction_saving, tax_credit_saving

    p = young_renter()
    amount = 1_200_000
    assert tax_credit_saving(p, YEAR, amount) > income_deduction_saving(p, YEAR, amount) * 3


def test_year_end_deadlines_are_set_on_contribution_actions():
    """연내 납입해야 효력이 생기는 것들은 마감이 있어야 한다."""
    p = young_renter(donations=600_000)
    d = discover_actions(p, YEAR)
    for key in ("pension_account", "housing_subscription", "donation_credit"):
        assert found(d, key).deadline == "2025-12-31"


def test_credit_saving_capped_by_gross_tax():
    """산출세액보다 큰 세액공제는 버려진다."""
    from ktax.tax import tax_credit_saving

    p = Profile(age=30, earned_income=15_000_000, income_deductions=14_000_000)
    assert tax_credit_saving(p, YEAR, 50_000_000) < 50_000_000


def test_housing_subscription_includes_homeless_heads_spouse():
    p = young_renter(is_homeless_household_head=False,
                     is_homeless_household_head_spouse=True)
    assert found(discover_actions(p, YEAR), "housing_subscription")


def test_housing_subscription_asks_spouse_status_before_rejecting():
    p = young_renter(is_homeless_household_head=False)
    known = frozenset({"earned_income", "is_homeless_household_head",
                       "housing_subscription_contributed"})
    d = discover_actions(p, YEAR, known=known)
    item = next(i for i in d.indeterminate if i.key == "housing_subscription")
    assert item.missing == ["is_homeless_household_head_spouse"]
    assert blocked(d, "housing_subscription") is None


def test_card_recommendation_asks_for_special_spending():
    known = frozenset({"earned_income", "credit_card_spending", "debit_cash_spending"})
    d = discover_actions(young_renter(), YEAR, known=known)
    item = next(i for i in d.indeterminate if i.key == "payment_method_switch")
    assert set(item.missing) == {"culture_spending", "traditional_market_spending", "public_transit_spending"}
