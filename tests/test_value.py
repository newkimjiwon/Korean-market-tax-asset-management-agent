from ktax.models import BenefitStream, Profile, Recurrence
from ktax.value import DEFAULT_HORIZON_CAP_YEARS, resolve_horizon, value_benefit


def profile(age: int) -> Profile:
    return Profile(age=age, earned_income=60_000_000)


def test_one_time_benefit_has_single_year_horizon():
    stream = BenefitStream(amount_per_year=5_000_000, recurrence=Recurrence.ONE_TIME)
    assert resolve_horizon(stream, profile(40)).years == 1


def test_horizon_derives_from_age_not_a_fixed_constant():
    """같은 상품이라도 나이에 따라 지평이 달라져야 한다."""
    stream = BenefitStream(
        amount_per_year=1_000_000, recurrence=Recurrence.RECURRING, ends_at_age=55
    )
    young = resolve_horizon(stream, profile(30))
    old = resolve_horizon(stream, profile(50))

    assert old.years == 5
    assert young.years == DEFAULT_HORIZON_CAP_YEARS  # 25년이지만 상한에서 절삭
    assert young.capped
    assert not old.capped


def test_horizon_is_capped_to_avoid_false_precision():
    stream = BenefitStream(
        amount_per_year=1_000_000, recurrence=Recurrence.RECURRING, ends_at_age=90
    )
    assert resolve_horizon(stream, profile(30)).years == DEFAULT_HORIZON_CAP_YEARS


def test_horizon_takes_the_tightest_constraint():
    stream = BenefitStream(
        amount_per_year=1_000_000,
        recurrence=Recurrence.RECURRING,
        ends_at_age=55,
        explicit_years=3,
    )
    assert resolve_horizon(stream, profile(50)).years == 3


def test_expired_horizon_yields_no_value():
    stream = BenefitStream(
        amount_per_year=1_000_000, recurrence=Recurrence.RECURRING, ends_at_age=55
    )
    valued = value_benefit(stream, profile(60))
    assert valued.present_value == 0


def test_recurring_benefit_outranks_larger_one_time_benefit():
    """이 프로젝트의 핵심 주장. 매년 100만원이 1회성 500만원을 이겨야 한다."""
    one_time = BenefitStream(amount_per_year=5_000_000, recurrence=Recurrence.ONE_TIME)
    recurring = BenefitStream(
        amount_per_year=1_000_000, recurrence=Recurrence.RECURRING, ends_at_age=55
    )
    p = profile(40)

    assert value_benefit(recurring, p).present_value > value_benefit(one_time, p).present_value


def test_annual_equivalent_uses_common_horizon():
    """각자 다른 지평의 연간 환산액끼리 비교하면 반복 효과가 저평가된다.
    공통 지평으로 펴야 같은 자 위에 놓인다."""
    one_time = BenefitStream(amount_per_year=5_000_000, recurrence=Recurrence.ONE_TIME)
    recurring = BenefitStream(
        amount_per_year=1_000_000, recurrence=Recurrence.RECURRING, ends_at_age=55
    )
    p = profile(40)

    ot = value_benefit(one_time, p)
    rc = value_benefit(recurring, p)

    assert ot.comparison_horizon_years == rc.comparison_horizon_years
    assert rc.annual_equivalent > ot.annual_equivalent
    assert rc.annual_equivalent == 1_000_000  # 지평 전체를 채우면 원래 금액으로 돌아온다


def test_future_benefit_is_discounted():
    now = BenefitStream(amount_per_year=1_000_000, recurrence=Recurrence.ONE_TIME)
    later = BenefitStream(
        amount_per_year=1_000_000, recurrence=Recurrence.ONE_TIME, starts_in_years=5
    )
    p = profile(40)
    assert value_benefit(later, p).present_value < value_benefit(now, p).present_value


def test_value_changes_as_user_ages_so_ranking_is_recomputed_yearly():
    stream = BenefitStream(
        amount_per_year=1_000_000, recurrence=Recurrence.RECURRING, ends_at_age=55
    )
    values = [value_benefit(stream, profile(age)).present_value for age in (48, 49, 50)]
    assert values[0] > values[1] > values[2]
