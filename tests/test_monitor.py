from ktax.models import Profile
from ktax.monitor import Severity, diff_snapshots, evaluate_thresholds

YEAR = 2025


def signal(signals, key):
    return next(s for s in signals if s.key == key)


def test_financial_income_headroom_is_reported():
    p = Profile(age=45, earned_income=60_000_000, financial_income=18_000_000)
    s = signal(evaluate_thresholds(p, YEAR), "financial_income_comprehensive")
    assert s.headroom == 2_000_000
    assert s.severity is Severity.WATCH


def test_financial_income_over_threshold_escalates():
    p = Profile(age=45, earned_income=60_000_000, financial_income=25_000_000)
    s = signal(evaluate_thresholds(p, YEAR), "financial_income_comprehensive")
    assert s.headroom < 0
    assert s.severity is Severity.ACT


def test_quiet_when_far_from_every_threshold():
    """할 말을 만들려고 알림을 쏘지 않는다. 조용할 수 있어야 한다."""
    p = Profile(age=45, earned_income=40_000_000, financial_income=1_000_000)
    signals = evaluate_thresholds(p, YEAR)
    assert all(s.severity is Severity.INFO for s in signals)


def test_pension_room_reported():
    p = Profile(age=45, earned_income=60_000_000, irp_contributed=3_000_000)
    s = signal(evaluate_thresholds(p, YEAR), "pension_credit_room")
    assert s.headroom == 6_000_000


def test_bracket_edge_distance():
    p = Profile(age=45, earned_income=49_000_000)
    s = signal(evaluate_thresholds(p, YEAR), "tax_bracket_edge")
    assert s.headroom == 1_000_000
    assert s.severity is Severity.WATCH


def test_diff_ignores_noise():
    """사소한 변동마다 알림을 쏘면 사용자는 알림을 끈다."""
    before = Profile(age=40, earned_income=60_000_000)
    after = Profile(age=40, earned_income=60_500_000)  # 0.8% 변동
    assert diff_snapshots(before, after) == []


def test_diff_detects_meaningful_income_change():
    before = Profile(age=40, earned_income=60_000_000)
    after = Profile(age=40, earned_income=75_000_000)
    changes = diff_snapshots(before, after)
    assert len(changes) == 1
    assert changes[0].field == "earned_income"
    assert changes[0].delta == 15_000_000


def test_diff_detects_new_financial_income_from_zero():
    before = Profile(age=40, earned_income=60_000_000, financial_income=0)
    after = Profile(age=40, earned_income=60_000_000, financial_income=5_000_000)
    assert [c.field for c in diff_snapshots(before, after)] == ["financial_income"]


def test_diff_detects_birthday_because_horizon_depends_on_it():
    before = Profile(age=40, earned_income=60_000_000)
    after = Profile(age=41, earned_income=60_000_000)
    assert [c.field for c in diff_snapshots(before, after)] == ["age"]
