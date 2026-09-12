"""효과를 비교 가능한 하나의 척도로 환산한다.

핵심 결정 두 가지
-----------------
1. 지평(horizon)은 고정값이 아니라 사용자에게서 유도한다.
   같은 연금 상품도 30세와 50세에게 가치가 완전히 다르다. 지평을 5년으로
   박아두면 한 번 계산하고 끝이지만, 나이·인출시점에서 유도하면 매년
   자동으로 답이 달라진다.

2. 순위 기준은 '자기 지평 위의 현재가치(PV)'이고,
   표시용 숫자는 '공통 지평 위로 편 연간 환산액'이다.
   각자 다른 지평에서 구한 연간 환산액끼리는 비교가 성립하지 않는다.
   (1회성 500만원의 1년 환산액은 500만원, 매년 100만원의 10년 환산액은
   100만원 — 이대로 비교하면 반복 효과가 부당하게 저평가된다.)
"""

from __future__ import annotations

from dataclasses import dataclass

from ktax.models import BenefitStream, Profile, Recurrence, Won

# 반복 효과를 무한정 크레딧하지 않기 위한 상한.
# 세법은 개정되고 소득은 변한다. 30년치를 곱하는 건 거짓 정밀도다.
DEFAULT_HORIZON_CAP_YEARS = 10

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
    """효과 스트림 → 현재가치 + 공통 지평 연간 환산액."""
    horizon = resolve_horizon(stream, profile, cap_years)
    comparison = comparison_horizon_years or cap_years

    if horizon.years <= 0 or stream.amount_per_year == 0:
        return ValuedBenefit(
            present_value=0,
            annual_equivalent=0,
            horizon=horizon,
            discount_rate=discount_rate,
            comparison_horizon_years=comparison,
        )

    shift = 1.0 / (1.0 + discount_rate) ** stream.starts_in_years
    pv = stream.amount_per_year * _annuity_factor(horizon.years, discount_rate) * shift

    # 서로 다른 지평의 액션을 같은 자 위에 놓기 위해, PV 를 공통 지평으로 편다.
    annual_equivalent = pv / _annuity_factor(comparison, discount_rate)

    return ValuedBenefit(
        present_value=round(pv),
        annual_equivalent=round(annual_equivalent),
        horizon=horizon,
        discount_rate=discount_rate,
        comparison_horizon_years=comparison,
    )
