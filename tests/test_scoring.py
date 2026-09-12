from datetime import date

from ktax.models import (
    Action,
    BenefitStream,
    EffortTier,
    Profile,
    Quadrant,
    Rationale,
    Recurrence,
)
from ktax.scoring import (
    classify_quadrant,
    score_actions,
    set_and_forget,
    sort_by_quick_wins,
    sort_by_value,
    urgent,
)

TODAY = date(2026, 9, 13)
P = Profile(age=40, earned_income=60_000_000)


def action(key, amount, recurrence, effort, effort_rec, deadline=None, ends_at_age=None):
    return Action(
        key=key,
        title=key,
        benefit=BenefitStream(
            amount_per_year=amount, recurrence=recurrence, ends_at_age=ends_at_age
        ),
        effort_tier=effort,
        effort_recurrence=effort_rec,
        rationale=Rationale(summary=key),
        deadline=deadline,
    )


def test_quadrant_classification():
    assert classify_quadrant(Recurrence.ONE_TIME, Recurrence.RECURRING) is Quadrant.SET_AND_FORGET
    assert classify_quadrant(Recurrence.ONE_TIME, Recurrence.ONE_TIME) is Quadrant.ONE_OFF
    assert classify_quadrant(Recurrence.RECURRING, Recurrence.RECURRING) is Quadrant.MAINTENANCE
    assert classify_quadrant(Recurrence.RECURRING, Recurrence.ONE_TIME) is Quadrant.POOR


def test_score_actions_does_not_reorder():
    """정렬은 호출자의 몫이다. 도구는 축만 붙여서 돌려준다."""
    actions = [
        action("small", 100_000, Recurrence.ONE_TIME, EffortTier.INSTANT, Recurrence.ONE_TIME),
        action("big", 9_000_000, Recurrence.ONE_TIME, EffortTier.PROJECT, Recurrence.ONE_TIME),
    ]
    scored = score_actions(actions, P, today=TODAY)
    assert [s.action.key for s in scored] == ["small", "big"]


def test_quick_wins_and_value_give_different_orders():
    """단일 ROI 점수로 합치지 않는 이유. 사용자 상황에 따라 답이 달라야 한다."""
    actions = [
        action("heavy", 3_000_000, Recurrence.ONE_TIME, EffortTier.PROJECT, Recurrence.ONE_TIME),
        action("light", 300_000, Recurrence.ONE_TIME, EffortTier.INSTANT, Recurrence.ONE_TIME),
    ]
    scored = score_actions(actions, P, today=TODAY)

    assert [s.action.key for s in sort_by_value(scored)] == ["heavy", "light"]
    assert [s.action.key for s in sort_by_quick_wins(scored)] == ["light", "heavy"]


def test_set_and_forget_filter():
    actions = [
        action(
            "auto_transfer", 800_000, Recurrence.RECURRING,
            EffortTier.INSTANT, Recurrence.ONE_TIME, ends_at_age=55,
        ),
        action(
            "yearly_receipts", 800_000, Recurrence.RECURRING,
            EffortTier.SHORT, Recurrence.RECURRING, ends_at_age=55,
        ),
    ]
    scored = score_actions(actions, P, today=TODAY)
    assert [s.action.key for s in set_and_forget(scored)] == ["auto_transfer"]


def test_deadline_is_not_folded_into_value():
    """급하지만 사소한 일이 가치 순위를 밀어내면 안 된다."""
    actions = [
        action(
            "urgent_small", 100_000, Recurrence.ONE_TIME,
            EffortTier.INSTANT, Recurrence.ONE_TIME, deadline="2026-09-20",
        ),
        action(
            "calm_large", 5_000_000, Recurrence.ONE_TIME,
            EffortTier.SHORT, Recurrence.ONE_TIME,
        ),
    ]
    scored = score_actions(actions, P, today=TODAY)

    assert [s.action.key for s in sort_by_value(scored)] == ["calm_large", "urgent_small"]
    assert [s.action.key for s in urgent(scored)] == ["urgent_small"]


def test_expired_deadline_is_not_urgent():
    actions = [
        action(
            "past", 100_000, Recurrence.ONE_TIME, EffortTier.INSTANT,
            Recurrence.ONE_TIME, deadline="2026-09-01",
        )
    ]
    assert urgent(score_actions(actions, P, today=TODAY)) == []


def test_to_dict_carries_rationale_for_the_agent():
    a = action("x", 100_000, Recurrence.ONE_TIME, EffortTier.INSTANT, Recurrence.ONE_TIME)
    d = score_actions([a], P, today=TODAY)[0].to_dict()
    assert d["rationale"]["summary"] == "x"
    assert "horizon_reason" in d
    assert d["is_set_and_forget"] is False
