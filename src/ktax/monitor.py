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

# 세금 상황은 날짜가 아니라 인생이 바뀔 때 변한다. 아래 필드의 유의미한
# 변화는 재계산을 촉발해야 한다.
_WATCHED_FIELDS: dict[str, tuple[str, float]] = {
    # 필드명: (사람이 읽을 이름, 유의미하다고 볼 상대 변화율)
    "earned_income": ("근로소득", 0.05),
    "business_income": ("사업소득", 0.05),
    "financial_income": ("금융소득", 0.10),
    "other_income": ("기타소득", 0.10),
}


@dataclass(frozen=True)
class ProfileChange:
    field: str
    label: str
    previous: Won
    current: Won

    @property
    def delta(self) -> Won:
        return self.current - self.previous

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "label": self.label,
            "previous": self.previous,
            "current": self.current,
            "delta": self.delta,
        }


def diff_snapshots(previous: Profile, current: Profile) -> list[ProfileChange]:
    """직전 스냅샷 대비 유의미한 변화만 뽑는다.

    노이즈를 걸러내는 게 핵심이다. 사소한 변동마다 알림을 쏘면
    사용자는 알림을 끄고, 그러면 정작 중요한 순간에 닿지 못한다.
    """
    changes: list[ProfileChange] = []
    for field, (label, tolerance) in _WATCHED_FIELDS.items():
        before = getattr(previous, field)
        after = getattr(current, field)
        if before == after:
            continue
        baseline = max(abs(before), 1)
        if abs(after - before) / baseline < tolerance:
            continue
        changes.append(
            ProfileChange(field=field, label=label, previous=before, current=after)
        )

    if previous.age != current.age:
        changes.append(
            ProfileChange(
                field="age", label="나이", previous=previous.age, current=current.age
            )
        )

    return changes
