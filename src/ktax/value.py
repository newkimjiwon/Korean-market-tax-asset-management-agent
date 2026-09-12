"""효과를 비교 가능한 하나의 척도로 환산한다.

핵심 결정 두 가지
-----------------
1. 지평(horizon)은 고정값이 아니라 사용자에게서 유도한다.
   같은 연금 상품도 30세와 50세에게 가치가 완전히 다르다. 지평을 5년으로
   박아두면 한 번 계산하고 끝이지만, 나이·인출시점에서 유도하면 매년
   자동으로 답이 달라진다.

2. 불확실성은 상한이 아니라 지속확률로 표현한다.
   하드 컷은 "N년째까지 100% 확실하고 그 다음엔 0%"라는 모델인데, 현실의
   어떤 것도 그렇게 작동하지 않는다. 게다가 상한은 전역이라 항목별로 다른
   불확실성을 담지 못한다 — 연금저축(본인이 계약한 법적 구조)과
   월세(자가 구매로 언제든 끊김)는 확신도가 명백히 다르다.
   지속확률은 할인율에 흡수되어 매끄러운 감쇠가 된다:
       (1 + 실효할인율) = (1 + 할인율) / 지속확률
   상한은 폭주 방지용 바깥 울타리로만 남는다.

3. 순위 기준은 '자기 지평 위의 현재가치(PV)'이고,
   표시용 숫자는 '공통 지평 위로 편 연간 환산액'이다.
   각자 다른 지평에서 구한 연간 환산액끼리는 비교가 성립하지 않는다.
   (1회성 500만원의 1년 환산액은 500만원, 매년 100만원의 10년 환산액은
   100만원 — 이대로 비교하면 반복 효과가 부당하게 저평가된다.)
"""

from __future__ import annotations

from dataclasses import dataclass

from ktax.models import BenefitStream, Profile, Recurrence, Won

# 폭주 방지용 바깥 울타리. 감쇠는 지속확률이 담당하므로 이 값이 실제로
# 구속력을 갖는 경우는 드물다 (지속확률 0.85면 15년 이후 기여분은 미미하다).
DEFAULT_HORIZON_CAP_YEARS = 30

# 연간 환산액을 펼 공통 지평. 모든 액션에 동일하게 적용되는 표시용 자이며,
# 상한과 목적이 다르므로 분리해 둔다.
DEFAULT_COMPARISON_HORIZON_YEARS = 10

# 실질 할인율. 먼 미래의 절감액을 오늘의 절감액과 같게 취급하지 않기 위한 값.
DEFAULT_DISCOUNT_RATE = 0.04


@dataclass(frozen=True)
class HorizonResolution:
    years: int
    reason: str
    capped: bool


@dataclass(frozen=True)
class ValuedBenefit:
    present_value: Won          # 순위 기준
    annual_equivalent: Won      # 표시용 (공통 지평 기준)
    horizon: HorizonResolution
    discount_rate: float
    comparison_horizon_years: int
    persistence: float          # 매년 효과가 이어질 확률
    effective_discount_rate: float  # 지속확률을 흡수한 실효 할인율


def resolve_horizon(
    stream: BenefitStream,
    profile: Profile,
    cap_years: int = DEFAULT_HORIZON_CAP_YEARS,
) -> HorizonResolution:
    """효과가 몇 년이나 지속되는지 사용자 상황에서 유도한다."""
    if stream.recurrence is Recurrence.ONE_TIME:
        return HorizonResolution(years=1, reason="1회성 효과", capped=False)

    candidates: list[tuple[int, str]] = []

    if stream.explicit_years is not None:
        candidates.append((stream.explicit_years, f"잔여 기간 {stream.explicit_years}년"))

    if stream.ends_at_age is not None:
        remaining = stream.ends_at_age - profile.age
        candidates.append(
            (remaining, f"{profile.age}세 → {stream.ends_at_age}세까지 {remaining}년")
        )

    if not candidates:
        return HorizonResolution(
            years=cap_years, reason=f"종료 조건 미지정, 상한 {cap_years}년 적용", capped=True
        )

    years, reason = min(candidates, key=lambda c: c[0])
    if years <= 0:
        return HorizonResolution(years=0, reason=f"{reason} — 이미 지남", capped=False)
    if years > cap_years:
        return HorizonResolution(
            years=cap_years,
            reason=f"{reason}, 단 상한 {cap_years}년으로 절삭",
            capped=True,
        )
    return HorizonResolution(years=years, reason=reason, capped=False)


def effective_rate(discount_rate: float, persistence: float) -> float:
    """지속확률을 할인율에 흡수한다.

    매년 p 의 확률로 이어지는 효과의 t년 후 기댓값은 amount × p^t 이고,
    이를 (1+r)^t 로 할인하면 amount / ((1+r)/p)^t 가 된다. 즉 지속확률은
    할인율 하나로 완전히 표현된다.
    """
    return (1.0 + discount_rate) / persistence - 1.0


def _annuity_factor(years: int, rate: float) -> float:
    """연초 지급 기준 연금현가계수."""
    return sum(1.0 / (1.0 + rate) ** t for t in range(years))


def value_benefit(
    stream: BenefitStream,
    profile: Profile,
    discount_rate: float = DEFAULT_DISCOUNT_RATE,
    cap_years: int = DEFAULT_HORIZON_CAP_YEARS,
    comparison_horizon_years: int | None = None,
) -> ValuedBenefit:
    """효과 스트림 → 현재가치 + 공통 지평 연간 환산액.

    PV 는 항목별 실효할인율(지속확률 반영)로 구하고, 공통 지평 환산은
    기본 할인율로 나눈다. 환산 분모까지 항목별로 달라지면 서로 다른
    액션을 같은 자 위에 놓는다는 목적 자체가 깨진다.
    """
    horizon = resolve_horizon(stream, profile, cap_years)
    comparison = comparison_horizon_years or DEFAULT_COMPARISON_HORIZON_YEARS
    rate = effective_rate(discount_rate, stream.persistence)

    if horizon.years <= 0 or stream.amount_per_year == 0:
        return ValuedBenefit(
            present_value=0,
            annual_equivalent=0,
            horizon=horizon,
            discount_rate=discount_rate,
            comparison_horizon_years=comparison,
            persistence=stream.persistence,
            effective_discount_rate=rate,
        )

    shift = 1.0 / (1.0 + rate) ** stream.starts_in_years
    pv = stream.amount_per_year * _annuity_factor(horizon.years, rate) * shift

    # 서로 다른 지평의 액션을 같은 자 위에 놓기 위해, PV 를 공통 지평으로 편다.
    # 분모는 모든 항목에 동일한 기본 할인율을 쓴다.
    annual_equivalent = pv / _annuity_factor(comparison, discount_rate)

    return ValuedBenefit(
        present_value=round(pv),
        annual_equivalent=round(annual_equivalent),
        horizon=horizon,
        discount_rate=discount_rate,
        comparison_horizon_years=comparison,
        persistence=stream.persistence,
        effective_discount_rate=rate,
    )
