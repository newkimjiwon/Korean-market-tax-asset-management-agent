"""출처를 아는 스냅샷 비교."""

from datetime import date

import pytest

from ktax.models import Profile
from ktax.monitor import (
    ChangeKind,
    diff_profile_data,
    diff_snapshots,
    life_events,
    needs_recompute,
)
from ktax.sources import FieldSource, from_source, merge

MAR = date(2026, 3, 1)
JAN = date(2027, 1, 15)


def snapshot(source: FieldSource, as_of: date, **values):
    return merge(from_source(source, as_of, values))


def only(changes, name):
    return next(c for c in changes if c.field == name)


# --------------------------------------------------------------------------
# 실제 변화 vs 데이터 정정 — 이 구분이 핵심
# --------------------------------------------------------------------------

def test_same_authority_change_is_a_life_event():
    """같은 출처가 다른 값을 주면 사용자의 상황이 바뀐 것이다."""
    prev = snapshot(FieldSource.WITHHOLDING_RECEIPT, MAR, age=33, earned_income=50_000_000)
    curr = snapshot(FieldSource.WITHHOLDING_RECEIPT, JAN, age=33, earned_income=70_000_000)

    change = only(diff_profile_data(prev, curr), "earned_income")
    assert change.kind is ChangeKind.VALUE_CHANGED
    assert change.is_life_event


def test_better_source_change_is_a_correction_not_a_life_event():
    """사용자 어림값을 원천징수영수증이 덮은 것은 이직이 아니다."""
    prev = snapshot(FieldSource.USER, MAR, age=33, earned_income=50_000_000)
    curr = snapshot(FieldSource.WITHHOLDING_RECEIPT, JAN, age=33, earned_income=70_000_000)

    change = only(diff_profile_data(prev, curr), "earned_income")
    assert change.kind is ChangeKind.CORRECTED
    assert not change.is_life_event
    assert change.affects_calculation


def test_correction_records_both_sources():
    prev = snapshot(FieldSource.USER, MAR, age=33, donations=300_000)
    curr = snapshot(FieldSource.HOMETAX_SIMPLIFIED, JAN, age=33, donations=900_000)

    change = only(diff_profile_data(prev, curr), "donations")
    assert change.previous_source == "user"
    assert change.current_source == "hometax_simplified"


def test_downgrade_to_weaker_source_is_still_a_life_event():
    """권위가 낮아지면 정정으로 볼 수 없다. 값 변화로 취급한다."""
    prev = snapshot(FieldSource.HOMETAX_SIMPLIFIED, MAR, age=33, donations=300_000)
    curr = snapshot(FieldSource.USER, JAN, age=33, donations=900_000)

    assert only(diff_profile_data(prev, curr), "donations").kind is ChangeKind.VALUE_CHANGED


# --------------------------------------------------------------------------
# 출처만 바뀐 경우
# --------------------------------------------------------------------------

def test_same_value_better_source_is_not_notified():
    prev = snapshot(FieldSource.USER, MAR, age=33, donations=300_000)
    curr = snapshot(FieldSource.HOMETAX_SIMPLIFIED, JAN, age=33, donations=300_000)

    change = only(diff_profile_data(prev, curr), "donations")
    assert change.kind is ChangeKind.SOURCE_UPGRADED
    assert not change.is_life_event


def test_source_upgrade_alone_needs_no_recompute():
    """입력이 그대로면 결과도 그대로다."""
    prev = snapshot(FieldSource.USER, MAR, age=33, donations=300_000)
    curr = snapshot(FieldSource.HOMETAX_SIMPLIFIED, JAN, age=33, donations=300_000)

    assert not needs_recompute(diff_profile_data(prev, curr))


def test_identical_snapshots_produce_nothing():
    prev = snapshot(FieldSource.USER, MAR, age=33, earned_income=50_000_000)
    assert diff_profile_data(prev, prev) == []


# --------------------------------------------------------------------------
# 새로 확보 / 사라짐
# --------------------------------------------------------------------------

def test_newly_known_field_is_enriched_not_a_change():
    """몰랐다가 알게 된 것은 사용자의 인생이 바뀐 게 아니다."""
    prev = snapshot(FieldSource.USER, MAR, age=33)
    curr = snapshot(FieldSource.HOMETAX_SIMPLIFIED, JAN, age=33, medical_expenses=2_000_000)

    change = only(diff_profile_data(prev, curr), "medical_expenses")
    assert change.kind is ChangeKind.ENRICHED
    assert not change.is_life_event
    assert change.affects_calculation


def test_lost_field_is_reported():
    prev = snapshot(FieldSource.HOMETAX_SIMPLIFIED, MAR, age=33, donations=300_000)
    curr = snapshot(FieldSource.USER, JAN, age=33)

    change = only(diff_profile_data(prev, curr), "donations")
    assert change.kind is ChangeKind.LOST
    assert change.current is None


# --------------------------------------------------------------------------
# 노이즈 억제
# --------------------------------------------------------------------------

def test_small_income_wobble_is_suppressed():
    prev = snapshot(FieldSource.WITHHOLDING_RECEIPT, MAR, age=33, earned_income=50_000_000)
    curr = snapshot(FieldSource.WITHHOLDING_RECEIPT, JAN, age=33, earned_income=52_000_000)

    assert diff_profile_data(prev, curr) == []


def test_noise_threshold_is_per_field():
    """금융소득은 근로소득보다 흔들림이 커서 허용치가 넓다."""
    def changed(field, before, after):
        prev = snapshot(FieldSource.MYDATA, MAR, age=33, **{field: before})
        curr = snapshot(FieldSource.MYDATA, JAN, age=33, **{field: after})
        return bool(diff_profile_data(prev, curr))

    assert changed("earned_income", 50_000_000, 53_500_000)      # 7% > 5%
    assert not changed("financial_income", 50_000_000, 53_500_000)  # 7% < 10%


def test_exact_fields_react_to_any_change():
    """자녀 수가 하나 늘어난 것은 노이즈가 아니다."""
    prev = snapshot(FieldSource.USER, MAR, age=33, dependent_children=1)
    curr = snapshot(FieldSource.USER, JAN, age=33, dependent_children=2)

    assert only(diff_profile_data(prev, curr), "dependent_children").is_life_event


def test_boolean_flip_is_detected():
    prev = snapshot(FieldSource.USER, MAR, age=33, is_homeless_household_head=True)
    curr = snapshot(FieldSource.USER, JAN, age=33, is_homeless_household_head=False)

    assert only(diff_profile_data(prev, curr), "is_homeless_household_head").is_life_event


# --------------------------------------------------------------------------
# 알림 선별
# --------------------------------------------------------------------------

def test_only_life_events_reach_the_user():
    prev = merge(from_source(FieldSource.USER, MAR, {
        "age": 33, "earned_income": 50_000_000, "donations": 300_000,
    }))
    curr = merge(
        from_source(FieldSource.WITHHOLDING_RECEIPT, JAN, {"earned_income": 80_000_000}),
        from_source(FieldSource.HOMETAX_SIMPLIFIED, JAN, {
            "donations": 300_000, "medical_expenses": 2_000_000,
        }),
        from_source(FieldSource.USER, JAN, {"age": 34}),
    )
    changes = diff_profile_data(prev, curr)
    notified = {c.field for c in life_events(changes)}

    assert notified == {"age"}                  # 나이만 실제 변화
    assert "earned_income" in {c.field for c in changes}   # 정정은 기록되지만
    assert "earned_income" not in notified                 # 알리지는 않는다
    assert needs_recompute(changes)


def test_to_dict_exposes_the_distinction():
    prev = snapshot(FieldSource.USER, MAR, age=33, earned_income=50_000_000)
    curr = snapshot(FieldSource.WITHHOLDING_RECEIPT, JAN, age=33, earned_income=80_000_000)

    d = only(diff_profile_data(prev, curr), "earned_income").to_dict()
    assert d["kind"] == "corrected"
    assert d["is_life_event"] is False
    assert d["affects_calculation"] is True
    assert d["delta"] == 30_000_000


# --------------------------------------------------------------------------
# 구 인터페이스
# --------------------------------------------------------------------------

def test_profile_diff_still_works_without_sources():
    before = Profile(age=40, earned_income=60_000_000)
    after = Profile(age=40, earned_income=80_000_000)
    assert [c.field for c in diff_snapshots(before, after)] == ["earned_income"]


def test_profile_diff_defaults_to_value_changed():
    before = Profile(age=40, earned_income=60_000_000)
    after = Profile(age=41, earned_income=60_000_000)
    assert diff_snapshots(before, after)[0].kind is ChangeKind.VALUE_CHANGED
