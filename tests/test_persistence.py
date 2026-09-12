import pytest

from ktax.assumptions import PERSISTENCE, persistence_for, persistence_note
from ktax.catalog import discover_actions
from ktax.models import BenefitStream, Profile, Recurrence
from ktax.value import DEFAULT_DISCOUNT_RATE, effective_rate, value_benefit

YEAR = 2025


def profile(age: int = 40) -> Profile:
    return Profile(age=age, earned_income=60_000_000)


# --------------------------------------------------------------------------
# 수학
# --------------------------------------------------------------------------

def test_certain_stream_is_undiscounted_by_persistence():
    assert effective_rate(0.04, 1.0) == pytest.approx(0.04)


def test_lower_persistence_raises_effective_rate():
    assert effective_rate(0.04, 0.85) == pytest.approx(1.04 / 0.85 - 1)
    assert effective_rate(0.04, 0.80) > effective_rate(0.04, 0.90)


def test_persistence_is_equivalent_to_survival_weighting():
    """지속확률 p 로 할인한 PV 는 매년 p^t 를 곱해 기댓값을 낸 것과 같아야 한다."""
    years, amount, p, r = 8, 1_000_000, 0.85, DEFAULT_DISCOUNT_RATE
    expected = sum(amount * (p ** t) / (1 + r) ** t for t in range(years))

    stream = BenefitStream(
        amount_per_year=amount,
        recurrence=Recurrence.RECURRING,
        explicit_years=years,
        persistence=p,
    )
    # 반환값은 원 단위 정수이므로 반올림 오차 1원까지 허용한다.
    assert value_benefit(stream, profile()).present_value == pytest.approx(expected, abs=1)


def test_persistence_must_be_a_probability():
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            BenefitStream(
                amount_per_year=1, recurrence=Recurrence.RECURRING, persistence=bad
            )


# --------------------------------------------------------------------------
# 감쇠는 절벽이 아니라 곡선
# --------------------------------------------------------------------------

def test_decay_is_smooth_not_a_cliff():
    """하드 컷이라면 마지막 해까지 기여가 일정하다가 갑자기 0이 된다.
    지속확률은 매년 매끄럽게 줄어야 한다."""
    stream = BenefitStream(
        amount_per_year=1_000_000,
        recurrence=Recurrence.RECURRING,
        explicit_years=20,
        persistence=0.85,
    )
    pvs = [
        value_benefit(
            BenefitStream(
                amount_per_year=1_000_000,
                recurrence=Recurrence.RECURRING,
                explicit_years=n,
                persistence=0.85,
            ),
            profile(),
        ).present_value
        for n in range(1, 21)
    ]
    marginal = [b - a for a, b in zip(pvs, pvs[1:])]
    assert all(m > 0 for m in marginal), "매 해가 양의 기여를 해야 한다"
    assert all(b < a for a, b in zip(marginal, marginal[1:])), "기여는 단조 감소해야 한다"


def test_far_future_contribution_becomes_negligible():
    """상한이 실제로 구속력을 갖지 않아야 한다 — 감쇠가 일을 한다."""
    def pv(years):
        return value_benefit(
            BenefitStream(
                amount_per_year=1_000_000,
                recurrence=Recurrence.RECURRING,
                explicit_years=years,
                persistence=0.85,
            ),
            profile(),
        ).present_value

    # 15년 이후 15년치를 통째로 더해도 5% 미만. 상한을 30으로 열어둬도 안전하다.
    assert (pv(30) - pv(15)) / pv(30) < 0.05


# --------------------------------------------------------------------------
# 항목별 차등
# --------------------------------------------------------------------------

def test_rent_is_valued_below_pension_at_equal_amounts():
    """같은 금액이라도 월세는 연금저축보다 낮게 평가되어야 한다.
    자가 구매로 언제든 끊기는 효과와 법적 구조를 같은 확신도로 볼 수 없다."""
    def pv(key):
        return value_benefit(
            BenefitStream(
                amount_per_year=1_000_000,
                recurrence=Recurrence.RECURRING,
                explicit_years=10,
                persistence=persistence_for(key),
            ),
            profile(),
        ).present_value

    assert pv("monthly_rent_credit") < pv("pension_account")


def test_every_catalog_action_has_an_explicit_persistence():
    """기본값에 조용히 기대지 않는다. 판단을 내렸는지 구조적으로 확인한다."""
    from ktax.catalog import catalog_keys

    missing = set(catalog_keys()) - set(PERSISTENCE)
    assert not missing, f"지속확률을 정하지 않은 액션: {missing}"


def test_persistence_values_are_probabilities():
    assert all(0.0 < v <= 1.0 for v in PERSISTENCE.values())


# --------------------------------------------------------------------------
# 근거 노출
# --------------------------------------------------------------------------

def test_discovered_actions_carry_their_persistence():
    p = Profile(
        age=29, earned_income=38_000_000, income_deductions=8_000_000,
        is_homeless_household_head=True, annual_rent_paid=7_200_000,
    )
    actions = {a.key: a for a in discover_actions(p, YEAR).applicable}
    assert actions["monthly_rent_credit"].benefit.persistence == 0.85
    assert actions["pension_account"].benefit.persistence == 0.97


def test_persistence_assumption_is_stated_in_rationale():
    """모델 가정임을 사용자가 알 수 있어야 한다."""
    p = Profile(
        age=29, earned_income=38_000_000, income_deductions=8_000_000,
        is_homeless_household_head=True, annual_rent_paid=7_200_000,
    )
    action = next(
        a for a in discover_actions(p, YEAR).applicable if a.key == "monthly_rent_credit"
    )
    note = " ".join(action.rationale.assumptions)
    assert "85%" in note and "세법이 정한 값이 아닙니다" in note


def test_certain_duration_actions_say_so():
    assert "할인을 적용하지 않" in persistence_note("sme_employment_reduction")
