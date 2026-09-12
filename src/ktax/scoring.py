"""액션 랭킹.

절감액을 노력으로 나눈 단일 ROI 점수는 만들지 않는다. 나눠버리면
사용자가 판단에 필요한 정보가 사라진다 — 지금 5분이 있는 사람과
주말이 통째로 있는 사람은 완전히 다른 목록을 원한다. 그래서 가치와
노력을 각각 독립된 축으로 두고, 합치는 판단은 에이전트에게 넘긴다.

마감도 가치 점수에 섞지 않는다. 섞으면 '급하지만 사소한 일'이
'안 급하지만 중요한 일'을 밀어내는 왜곡이 생긴다. 별도 오버레이로 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ktax.models import Action, EffortTier, Profile, Quadrant, Recurrence, Won
from ktax.value import (
    DEFAULT_DISCOUNT_RATE,
    DEFAULT_HORIZON_CAP_YEARS,
    ValuedBenefit,
    value_benefit,
)

_EFFORT_ORDER = {EffortTier.INSTANT: 0, EffortTier.SHORT: 1, EffortTier.PROJECT: 2}


def classify_quadrant(
    effort_recurrence: Recurrence, benefit_recurrence: Recurrence
) -> Quadrant:
    one_shot_effort = effort_recurrence is Recurrence.ONE_TIME
    recurring_benefit = benefit_recurrence is Recurrence.RECURRING

    if one_shot_effort and recurring_benefit:
        return Quadrant.SET_AND_FORGET
    if one_shot_effort:
        return Quadrant.ONE_OFF
    if recurring_benefit:
        return Quadrant.MAINTENANCE
    return Quadrant.POOR


@dataclass(frozen=True)
class ScoredAction:
    action: Action
    value: ValuedBenefit
    quadrant: Quadrant
    days_until_deadline: int | None

    @property
    def is_set_and_forget(self) -> bool:
        return self.quadrant is Quadrant.SET_AND_FORGET

    @property
    def effort_tier(self) -> EffortTier:
        return self.action.effort_tier

    @property
    def present_value(self) -> Won:
        return self.value.present_value

    def to_dict(self) -> dict:
        """에이전트가 그대로 소비할 수 있는 평평한 형태."""
        r = self.action.rationale
        return {
            "key": self.action.key,
            "title": self.action.title,
            "present_value": self.value.present_value,
            "annual_equivalent": self.value.annual_equivalent,
            "horizon_years": self.value.horizon.years,
            "horizon_reason": self.value.horizon.reason,
            "effort_tier": self.action.effort_tier.value,
            "quadrant": self.quadrant.value,
            "is_set_and_forget": self.is_set_and_forget,
            "deadline": self.action.deadline,
            "days_until_deadline": self.days_until_deadline,
            "rationale": {
                "summary": r.summary,
                "applied_rules": r.applied_rules,
                "assumptions": r.assumptions,
                "warnings": r.warnings(),
            },
        }


def _days_until(deadline: str | None, today: date) -> int | None:
    if not deadline:
        return None
    return (date.fromisoformat(deadline) - today).days


def score_actions(
    actions: list[Action],
    profile: Profile,
    *,
    today: date | None = None,
    discount_rate: float = DEFAULT_DISCOUNT_RATE,
    cap_years: int = DEFAULT_HORIZON_CAP_YEARS,
) -> list[ScoredAction]:
    """각 액션에 가치·노력·사분면·마감을 붙여서 돌려준다.

    정렬은 하지 않는다. 어떤 축으로 볼지는 호출자가 정한다.
    """
    today = today or date.today()
    return [
        ScoredAction(
            action=a,
            value=value_benefit(
                a.benefit, profile, discount_rate=discount_rate, cap_years=cap_years
            ),
            quadrant=classify_quadrant(a.effort_recurrence, a.benefit.recurrence),
            days_until_deadline=_days_until(a.deadline, today),
        )
        for a in actions
    ]


def sort_by_value(scored: list[ScoredAction]) -> list[ScoredAction]:
    """가치 우선. 시간이 충분한 사용자용."""
    return sorted(scored, key=lambda s: -s.present_value)


def sort_by_quick_wins(scored: list[ScoredAction]) -> list[ScoredAction]:
    """노력이 가벼운 순, 같은 등급 안에서는 가치 순. 지금 5분 있는 사용자용."""
    return sorted(scored, key=lambda s: (_EFFORT_ORDER[s.effort_tier], -s.present_value))


def set_and_forget(scored: list[ScoredAction]) -> list[ScoredAction]:
    """한 번 세팅하면 매년 자동으로 돌아오는 것들. 사실상 지배적인 사분면."""
    return sort_by_value([s for s in scored if s.is_set_and_forget])


def urgent(scored: list[ScoredAction], within_days: int = 30) -> list[ScoredAction]:
    """마감 오버레이. 가치 순위와 독립적으로 얹는다."""
    hits = [
        s for s in scored
        if s.days_until_deadline is not None and 0 <= s.days_until_deadline <= within_days
    ]
    return sorted(hits, key=lambda s: s.days_until_deadline or 0)
