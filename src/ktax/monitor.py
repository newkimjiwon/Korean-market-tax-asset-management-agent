"""상시 감시 레이어.

이 도구들이 '연 1회 계산기'와 '매년 돌아오는 에이전트'를 가른다.
사람은 금융소득이 2천만원 경계에 다가가는 걸 상시 추적하지 못한다.

월 1회 배치로 스냅샷을 찍고 직전 스냅샷과 비교하는 모델을 전제로 한다.
따라서 도구는 상태를 저장하지 않고, 스냅샷 두 개를 인자로 받는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ktax.models import Profile, Won
from ktax.sources import ProfileData, label_of
from ktax.rules import load_ruleset
from ktax.tax import marginal_rate, taxable_base


class Severity(str, Enum):
    INFO = "info"
    WATCH = "watch"       # 여유가 줄고 있음
    ACT = "act"           # 지금 행동하면 결과가 달라짐


@dataclass(frozen=True)
class ThresholdSignal:
    key: str
    title: str
    severity: Severity
    headroom: Won             # 경계까지 남은 금액 (음수면 이미 초과)
    detail: str

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "severity": self.severity.value,
            "headroom": self.headroom,
            "detail": self.detail,
        }


def _severity(used_ratio: float, exceeded: bool) -> Severity:
    if exceeded:
        return Severity.ACT
    if used_ratio >= 0.8:
        return Severity.WATCH
    return Severity.INFO


def evaluate_thresholds(profile: Profile, year: int) -> list[ThresholdSignal]:
    """경계선까지의 여유를 계산한다. 알림을 쏠지 말지는 호출자가 정한다."""
    rules = load_ruleset(year)
    signals: list[ThresholdSignal] = []

    # 1. 금융소득 종합과세 경계
    fin = rules["financial_income"]
    threshold = fin["comprehensive_taxation_threshold"]
    headroom = threshold - profile.financial_income
    exceeded = headroom < 0
    ratio = profile.financial_income / threshold if threshold else 0.0
    if exceeded:
        detail = (
            f"금융소득 {profile.financial_income:,}원으로 종합과세 대상입니다. "
            f"한계세율 {marginal_rate(taxable_base(profile, year), year):.0%}가 적용됩니다."
        )
    else:
        detail = (
            f"금융소득 {profile.financial_income:,}원, 종합과세 경계까지 "
            f"{headroom:,}원 남았습니다. 추가 이자·배당 실현 시점을 조절하면 "
            f"경계를 넘기지 않을 수 있습니다."
        )
    signals.append(
        ThresholdSignal(
            key="financial_income_comprehensive",
            title="금융소득 종합과세 경계",
            severity=_severity(ratio, exceeded),
            headroom=headroom,
            detail=detail,
        )
    )

    # 2. 연금계좌 세액공제 한도 소진
    pension = rules["pension_account"]
    limit = pension["combined_limit_with_irp"]
    used = profile.pension_savings_contributed + profile.irp_contributed
    room = limit - used
    signals.append(
        ThresholdSignal(
            key="pension_credit_room",
            title="연금계좌 세액공제 한도",
            severity=Severity.INFO if room <= 0 else _severity(used / limit, False),
            headroom=max(0, room),
            detail=(
                f"올해 {used:,}원 납입, 한도 {limit:,}원 중 {max(0, room):,}원 남았습니다. "
                f"연내 미납입분은 이월되지 않습니다."
            ),
        )
    )

    # 3. 세율 구간 경계
    base = taxable_base(profile, year)
    for bracket in rules["income_tax_brackets"]:
        upper = bracket["upper"]
        if upper is None or base > upper:
            continue
        distance = upper - base
        signals.append(
            ThresholdSignal(
                key="tax_bracket_edge",
                title="종합소득세 구간 경계",
                severity=Severity.WATCH if distance <= upper * 0.05 else Severity.INFO,
                headroom=distance,
                detail=(
                    f"과세표준 {base:,}원, 현재 구간 상한 {upper:,}원까지 "
                    f"{distance:,}원 남았습니다. 초과하면 한계세율이 오릅니다."
                ),
            )
        )
        break

    # 4. ISA 연간 납입 한도
    isa = rules["isa"]
    annual_limit = isa["annual_contribution_limit"]
    isa_room = annual_limit - profile.isa_contributed_this_year
    signals.append(
        ThresholdSignal(
            key="isa_annual_room",
            title="ISA 연간 납입 한도",
            severity=_severity(profile.isa_contributed_this_year / annual_limit, False),
            headroom=max(0, isa_room),
            detail=(
                f"올해 {profile.isa_contributed_this_year:,}원 납입, "
                f"{max(0, isa_room):,}원 남았습니다."
            ),
        )
    )

    return signals


# --------------------------------------------------------------------------
# 스냅샷 비교 — 생활 사건 감지
# --------------------------------------------------------------------------
#
# 세금 상황은 날짜가 아니라 인생이 바뀔 때 변한다. 다만 자동 수집을 쓰면
# 값이 달라지는 이유가 둘로 갈린다.
#
#   1. 사용자의 상황이 실제로 바뀌었다 (이직, 연봉 인상, 출산)
#   2. 더 정확한 자료를 확보했다 (사용자 어림값 → 간소화자료)
#
# 이 둘을 합치면 "소득이 늘었네요"라고 알림이 나가는데 사실은 우리가
# 제대로 된 자료를 처음 본 것뿐일 수 있다. 알림 한 번 잘못 나가면
# 사용자는 알림을 끄고, 그러면 정작 중요한 순간에 닿지 못한다.

# 금액 필드의 노이즈 허용치. 이보다 작은 변동은 무시한다.
_TOLERANCE: dict[str, float] = {
    "earned_income": 0.05,
    "business_income": 0.05,
    "financial_income": 0.10,
    "other_income": 0.10,
}
_DEFAULT_TOLERANCE = 0.10

# 금액이 아니어서 조금만 달라져도 의미가 있는 필드.
_EXACT_FIELDS = frozenset({
    "age", "filing_type", "dependent_children", "isa_preferential",
    "is_homeless_household_head", "sme_employment_start_year",
    "sme_special_category", "planned_withdrawal_age",
})


class ChangeKind(str, Enum):
    VALUE_CHANGED = "value_changed"      # 상황이 실제로 바뀜 — 생활 사건
    CORRECTED = "corrected"              # 더 나은 출처가 다른 값을 줌 — 데이터 정정
    ENRICHED = "enriched"                # 몰랐던 값을 새로 확보
    SOURCE_UPGRADED = "source_upgraded"  # 값은 같고 출처만 개선 — 알릴 일 아님
    LOST = "lost"                        # 있던 값이 사라짐


@dataclass(frozen=True)
class ProfileChange:
    field: str
    label: str
    previous: object
    current: object
    kind: ChangeKind = ChangeKind.VALUE_CHANGED
    previous_source: str | None = None
    current_source: str | None = None

    @property
    def delta(self) -> object:
        if isinstance(self.previous, (int, float)) and isinstance(self.current, (int, float)):
            return self.current - self.previous
        return None

    @property
    def is_life_event(self) -> bool:
        """사용자에게 알릴 만한 변화인가.

        데이터 정정과 출처 개선은 우리 쪽 사정이지 사용자의 인생이
        바뀐 것이 아니다.
        """
        return self.kind is ChangeKind.VALUE_CHANGED

    @property
    def affects_calculation(self) -> bool:
        """재계산이 필요한가. 값이 그대로면 결과도 그대로다."""
        return self.kind is not ChangeKind.SOURCE_UPGRADED

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "label": self.label,
            "kind": self.kind.value,
            "previous": self.previous,
            "current": self.current,
            "delta": self.delta,
            "previous_source": self.previous_source,
            "current_source": self.current_source,
            "is_life_event": self.is_life_event,
            "affects_calculation": self.affects_calculation,
        }


def _is_noise(name: str, before: object, after: object) -> bool:
    """허용치 안의 변동인가. 사소한 변동마다 알리면 사용자는 알림을 끈다."""
    if name in _EXACT_FIELDS or not isinstance(before, (int, float)):
        return False
    if not isinstance(after, (int, float)):
        return False
    tolerance = _TOLERANCE.get(name, _DEFAULT_TOLERANCE)
    baseline = max(abs(before), 1)
    return abs(after - before) / baseline < tolerance


def diff_snapshots(previous: Profile, current: Profile) -> list[ProfileChange]:
    """Profile 두 개를 비교한다. 출처 정보가 없으므로 값 변화만 본다.

    자동 수집을 쓴다면 `diff_profile_data` 를 써야 한다. 출처를 모르면
    실제 변화와 데이터 정정을 구분할 수 없다.
    """
    changes: list[ProfileChange] = []
    for name in _TOLERANCE:
        before, after = getattr(previous, name), getattr(current, name)
        if before == after or _is_noise(name, before, after):
            continue
        changes.append(
            ProfileChange(field=name, label=label_of(name), previous=before, current=after)
        )

    if previous.age != current.age:
        changes.append(
            ProfileChange(
                field="age", label=label_of("age"),
                previous=previous.age, current=current.age,
            )
        )
    return changes


def diff_profile_data(previous: ProfileData, current: ProfileData) -> list[ProfileChange]:
    """출처를 아는 스냅샷 비교.

    값이 달라진 이유를 구분한다. 더 권위 있는 출처가 다른 값을 주었다면
    그것은 사용자의 상황이 바뀐 것이 아니라 우리가 이제야 제대로 된 자료를
    본 것이다 — 재계산은 해야 하지만 "소득이 바뀌셨네요"라고 말하면 안 된다.
    """
    changes: list[ProfileChange] = []
    names = sorted(previous.known() | current.known())

    for name in names:
        before = previous.fields.get(name)
        after = current.fields.get(name)
        label = label_of(name)

        if before is None and after is not None:
            changes.append(ProfileChange(
                field=name, label=label, previous=None, current=after.value,
                kind=ChangeKind.ENRICHED, current_source=after.source.value,
            ))
            continue

        if after is None and before is not None:
            changes.append(ProfileChange(
                field=name, label=label, previous=before.value, current=None,
                kind=ChangeKind.LOST, previous_source=before.source.value,
            ))
            continue

        if before is None or after is None:
            continue

        same_value = before.value == after.value
        better_source = after.rank > before.rank

        if same_value:
            if before.source is not after.source and better_source:
                changes.append(ProfileChange(
                    field=name, label=label, previous=before.value, current=after.value,
                    kind=ChangeKind.SOURCE_UPGRADED,
                    previous_source=before.source.value,
                    current_source=after.source.value,
                ))
            continue

        if _is_noise(name, before.value, after.value):
            continue

        kind = ChangeKind.CORRECTED if better_source else ChangeKind.VALUE_CHANGED
        changes.append(ProfileChange(
            field=name, label=label, previous=before.value, current=after.value,
            kind=kind,
            previous_source=before.source.value,
            current_source=after.source.value,
        ))

    return changes


def life_events(changes: list[ProfileChange]) -> list[ProfileChange]:
    """사용자에게 알릴 만한 변화만. 나머지는 조용히 재계산만 한다."""
    return [c for c in changes if c.is_life_event]


def needs_recompute(changes: list[ProfileChange]) -> bool:
    return any(c.affects_calculation for c in changes)
