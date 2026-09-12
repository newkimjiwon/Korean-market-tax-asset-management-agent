"""절세 액션 카탈로그.

액션마다 도구를 하나씩 늘리면 에이전트가 매번 '뭘 불러야 하지'를 고르게 된다.
대신 평가 함수를 레지스트리에 모아두고 진입점 하나(`discover_actions`)만
노출한다. 에이전트는 "이 사람이 할 수 있는 게 뭐야?"만 물으면 된다.

평가 함수는 `Action`(적용 가능) 또는 `Ineligible`(사유 포함)을 돌려준다.
자격 미달을 조용히 빼지 않고 사유를 남기는 이유는, 사용자가 "왜 나는 이게
안 뜨죠?"라고 물었을 때 에이전트가 답할 수 있어야 하기 때문이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ktax.models import (
    Action,
    BenefitStream,
    EffortTier,
    Profile,
    Rationale,
    Recurrence,
    Won,
)
from ktax.rules import load_ruleset
from ktax.tax import (
    income_deduction_saving,
    simulate_isa_contribution,
    simulate_pension_contribution,
    tax_credit_saving,
)


@dataclass(frozen=True)
class Ineligible:
    key: str
    title: str
    reason: str

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "reason": self.reason}


Evaluation = Action | Ineligible
Evaluator = Callable[[Profile, int], Evaluation]

_CATALOG: list[Evaluator] = []


def entry(fn: Evaluator) -> Evaluator:
    _CATALOG.append(fn)
    return fn


def _year_end(year: int) -> str:
    """연내 납입해야 효력이 생기는 액션의 마감. 이월되지 않는다."""
    return f"{year}-12-31"


def _rationale(year: int, summary: str, rules_used: list[str], assumptions: list[str]):
    ruleset = load_ruleset(year)
    return Rationale(
        summary=summary,
        applied_rules=rules_used,
        assumptions=assumptions,
        ruleset_year=year,
        ruleset_verified=ruleset.get("verified", False),
    )


# --------------------------------------------------------------------------
# 1회 노력 → 반복 효과 (set_and_forget)
# --------------------------------------------------------------------------

@entry
def pension_account(profile: Profile, year: int) -> Evaluation:
    """연금저축/IRP 납입 — 자동이체 한 번에 매년 세액공제."""
    rules = load_ruleset(year)["pension_account"]
    used = profile.pension_savings_contributed + profile.irp_contributed
    room = rules["combined_limit_with_irp"] - used
    if room <= 0:
        return Ineligible(
            "pension_account", "연금계좌 추가 납입",
            f"올해 한도 {rules['combined_limit_with_irp']:,}원을 이미 모두 사용했습니다.",
        )
    if profile.age >= rules["withdrawal_start_age"]:
        return Ineligible(
            "pension_account", "연금계좌 추가 납입",
            f"{rules['withdrawal_start_age']}세 이상은 납입보다 수령 전략이 우선입니다.",
        )

    sim = simulate_pension_contribution(profile, year, room)
    return Action(
        key="pension_account",
        title="연금저축·IRP 한도까지 납입",
        benefit=sim.benefit,
        effort_tier=EffortTier.INSTANT,
        effort_recurrence=Recurrence.ONE_TIME,   # 자동이체 한 번
        rationale=sim.rationale,
        deadline=_year_end(year),
    )


@entry
def housing_subscription(profile: Profile, year: int) -> Evaluation:
    """주택청약종합저축 — 무주택 세대주의 소득공제."""
    key, title = "housing_subscription", "주택청약종합저축 납입"
    rules = load_ruleset(year)["housing_subscription"]

    if not profile.is_homeless_household_head:
        return Ineligible(key, title, "무주택 세대주만 공제 대상입니다.")
    if profile.earned_income > rules["earned_income_ceiling"]:
        return Ineligible(
            key, title,
            f"총급여 {rules['earned_income_ceiling']:,}원 이하만 대상입니다 "
            f"(현재 {profile.earned_income:,}원).",
        )

    room = rules["annual_contribution_limit"] - profile.housing_subscription_contributed
    if room <= 0:
        return Ineligible(key, title, "올해 공제 대상 납입 한도를 모두 사용했습니다.")

    deduction = round(room * rules["deduction_rate"])
    saving = income_deduction_saving(profile, year, deduction)

    return Action(
        key=key,
        title="주택청약종합저축 한도까지 납입",
        benefit=BenefitStream(
            amount_per_year=saving, recurrence=Recurrence.RECURRING, explicit_years=None
        ),
        effort_tier=EffortTier.INSTANT,
        effort_recurrence=Recurrence.ONE_TIME,
        rationale=_rationale(
            year,
            f"추가 납입 {room:,}원 → 소득공제 {deduction:,}원, 연 {saving:,}원 절감",
            [
                f"납입 한도 {rules['annual_contribution_limit']:,}원의 "
                f"{rules['deduction_rate']:.0%} 소득공제",
                f"총급여 {rules['earned_income_ceiling']:,}원 이하 무주택 세대주",
            ],
            [
                "소득공제이므로 절감액은 한계세율에 비례합니다",
                "무주택 세대주 요건을 계속 유지한다고 가정",
            ],
        ),
        deadline=_year_end(year),
    )


@entry
def sme_employment_reduction(profile: Profile, year: int) -> Evaluation:
    """중소기업 취업자 소득세 감면 — 한 번 신청하면 정해진 기간 동안 자동."""
    key, title = "sme_employment_reduction", "중소기업 취업자 소득세 감면"
    rules = load_ruleset(year)["sme_employment_reduction"]

    if profile.sme_employment_start_year is None:
        return Ineligible(key, title, "중소기업 취업 이력이 프로필에 없습니다.")

    is_youth = rules["youth_min_age"] <= profile.age <= rules["youth_max_age"]
    if not is_youth and not profile.sme_special_category:
        return Ineligible(
            key, title,
            f"청년({rules['youth_min_age']}~{rules['youth_max_age']}세), 60세 이상, "
            f"장애인, 경력단절여성만 대상입니다.",
        )

    rate = rules["youth_reduction_rate"] if is_youth else rules["other_reduction_rate"]
    duration = (
        rules["youth_duration_years"] if is_youth else rules["other_duration_years"]
    )
    elapsed = year - profile.sme_employment_start_year
    remaining = duration - elapsed
    if remaining <= 0:
        return Ineligible(
            key, title,
            f"감면 기간 {duration}년이 이미 종료되었습니다 "
            f"({profile.sme_employment_start_year}년 취업).",
        )

    from ktax.tax import estimate_tax

    baseline = estimate_tax(profile, year)
    saving = min(round(baseline.income_tax * rate), rules["annual_cap"])
    saving += round(saving * load_ruleset(year)["local_income_tax_rate"])

    return Action(
        key=key,
        title="중소기업 취업자 소득세 감면 신청",
        benefit=BenefitStream(
            amount_per_year=saving,
            recurrence=Recurrence.RECURRING,
            explicit_years=remaining,
        ),
        effort_tier=EffortTier.SHORT,
        effort_recurrence=Recurrence.ONE_TIME,   # 감면신청서 1회 제출
        rationale=_rationale(
            year,
            f"소득세 {rate:.0%} 감면(연 {rules['annual_cap']:,}원 한도) → "
            f"연 {saving:,}원, 남은 기간 {remaining}년",
            [
                f"{'청년' if is_youth else '특례 대상'} 감면율 {rate:.0%}",
                f"감면 기간 {duration}년, 연 한도 {rules['annual_cap']:,}원",
            ],
            [
                f"{profile.sme_employment_start_year}년 취업 기준 {elapsed}년 경과",
                "재직 중인 회사가 감면 대상 업종이라고 가정",
            ],
        ),
    )


@entry
def isa_account(profile: Profile, year: int) -> Evaluation:
    """ISA 납입 — 금융소득이 있을 때만 의미가 있다."""
    key, title = "isa_account", "ISA 납입"
    rules = load_ruleset(year)["isa"]

    if profile.financial_income <= 0:
        return Ineligible(key, title, "금융소득이 없어 절감 효과를 계산할 수 없습니다.")

    annual_room = rules["annual_contribution_limit"] - profile.isa_contributed_this_year
    total_room = rules["total_contribution_limit"] - profile.isa_contributed_total
    room = min(annual_room, total_room)
    if room <= 0:
        return Ineligible(key, title, "납입 한도를 모두 사용했습니다.")

    # 기존 금융소득에서 역산한 실효 수익률을 가정값으로 쓴다.
    base = max(profile.isa_contributed_total, room)
    implied_rate = min(0.15, profile.financial_income / base) if base else 0.03

    sim = simulate_isa_contribution(profile, year, room, implied_rate)
    if sim.annual_saving <= 0:
        return Ineligible(key, title, "현재 수익 수준에서는 절감 효과가 없습니다.")

    return Action(
        key=key,
        title="ISA 한도까지 납입",
        benefit=sim.benefit,
        effort_tier=EffortTier.SHORT,
        effort_recurrence=Recurrence.ONE_TIME,
        rationale=sim.rationale,
        deadline=_year_end(year),
    )


# --------------------------------------------------------------------------
# 반복 노력 → 반복 효과 (maintenance)
# --------------------------------------------------------------------------

@entry
def monthly_rent_credit(profile: Profile, year: int) -> Evaluation:
    """월세 세액공제 — 매년 증빙을 다시 제출해야 한다."""
    key, title = "monthly_rent_credit", "월세 세액공제"
    rules = load_ruleset(year)["monthly_rent"]

    if profile.annual_rent_paid <= 0:
        return Ineligible(key, title, "월세 지출 내역이 없습니다.")
    if not profile.is_homeless_household_head:
        return Ineligible(key, title, "무주택 세대주만 공제 대상입니다.")
    if profile.earned_income > rules["earned_income_ceiling"]:
        return Ineligible(
            key, title,
            f"총급여 {rules['earned_income_ceiling']:,}원 이하만 대상입니다.",
        )

    rate = (
        rules["credit_rate_low_income"]
        if profile.earned_income <= rules["low_income_earned_income_ceiling"]
        else rules["credit_rate_high_income"]
    )
    eligible_rent = min(profile.annual_rent_paid, rules["annual_rent_limit"])
    credit = round(eligible_rent * rate)
    saving = tax_credit_saving(profile, year, credit)

    if saving <= 0:
        return Ineligible(key, title, "산출세액이 없어 세액공제를 받을 수 없습니다.")

    return Action(
        key=key,
        title="월세 세액공제 신청",
        benefit=BenefitStream(amount_per_year=saving, recurrence=Recurrence.RECURRING),
        effort_tier=EffortTier.SHORT,
        effort_recurrence=Recurrence.RECURRING,   # 매년 계약서·이체증빙 제출
        rationale=_rationale(
            year,
            f"월세 {eligible_rent:,}원 × {rate:.0%} = 세액공제 {credit:,}원, "
            f"연 {saving:,}원 절감",
            [
                f"공제율 {rate:.0%} (총급여 "
                f"{rules['low_income_earned_income_ceiling']:,}원 기준 차등)",
                f"공제 대상 월세 한도 {rules['annual_rent_limit']:,}원",
            ],
            ["임대차계약서와 이체 증빙을 매년 제출해야 합니다"],
        ),
    )


@entry
def medical_expense_credit(profile: Profile, year: int) -> Evaluation:
    """의료비 세액공제 — 총급여 3% 초과분만 대상."""
    key, title = "medical_expense_credit", "의료비 세액공제"
    rules = load_ruleset(year)["medical_expense"]

    if profile.medical_expenses <= 0:
        return Ineligible(key, title, "의료비 지출 내역이 없습니다.")

    floor = round(profile.earned_income * rules["income_threshold_rate"])
    excess = profile.medical_expenses - floor
    if excess <= 0:
        return Ineligible(
            key, title,
            f"의료비 {profile.medical_expenses:,}원이 공제 기준선 "
            f"{floor:,}원(총급여의 {rules['income_threshold_rate']:.0%})에 못 미칩니다.",
        )

    eligible = min(excess, rules["general_limit"])
    credit = round(eligible * rules["credit_rate"])
    saving = tax_credit_saving(profile, year, credit)

    return Action(
        key=key,
        title="의료비 세액공제 신청",
        benefit=BenefitStream(amount_per_year=saving, recurrence=Recurrence.RECURRING),
        effort_tier=EffortTier.SHORT,
        effort_recurrence=Recurrence.RECURRING,
        rationale=_rationale(
            year,
            f"기준선 {floor:,}원 초과분 {eligible:,}원 × {rules['credit_rate']:.0%} "
            f"→ 연 {saving:,}원 절감",
            [
                f"총급여의 {rules['income_threshold_rate']:.0%} 초과분만 공제 대상",
                f"공제율 {rules['credit_rate']:.0%}, 한도 {rules['general_limit']:,}원",
            ],
            [
                "본인·65세 이상·장애인 의료비의 한도 예외는 반영하지 않았습니다",
                "난임시술비 등 고율 항목은 별도 계산이 필요합니다",
            ],
        ),
    )


@entry
def donation_credit(profile: Profile, year: int) -> Evaluation:
    """기부금 세액공제 — 일정액 초과분은 공제율이 올라간다."""
    key, title = "donation_credit", "기부금 세액공제"
    rules = load_ruleset(year)["donation"]

    if profile.donations <= 0:
        return Ineligible(key, title, "기부 내역이 없습니다.")

    threshold = rules["high_rate_threshold"]
    base_part = min(profile.donations, threshold)
    high_part = max(0, profile.donations - threshold)
    credit = round(
        base_part * rules["credit_rate_base"] + high_part * rules["credit_rate_high"]
    )
    saving = tax_credit_saving(profile, year, credit)

    if saving <= 0:
        return Ineligible(key, title, "산출세액이 없어 세액공제를 받을 수 없습니다.")

    return Action(
        key=key,
        title="기부금 세액공제 신청",
        benefit=BenefitStream(amount_per_year=saving, recurrence=Recurrence.RECURRING),
        effort_tier=EffortTier.INSTANT,
        effort_recurrence=Recurrence.RECURRING,
        rationale=_rationale(
            year,
            f"기부금 {profile.donations:,}원 → 세액공제 {credit:,}원, "
            f"연 {saving:,}원 절감",
            [
                f"{threshold:,}원 이하 {rules['credit_rate_base']:.0%}, "
                f"초과분 {rules['credit_rate_high']:.0%}",
            ],
            [
                "지정기부금 기준입니다. 정치자금·법정기부금은 별도 산식입니다",
                "공제 한도(소득금액 대비)는 반영하지 않았습니다",
            ],
        ),
        deadline=_year_end(year),
    )


# --------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Discovery:
    applicable: list[Action]
    ineligible: list[Ineligible]


def discover_actions(profile: Profile, year: int) -> Discovery:
    """프로필에 적용 가능한 액션을 전부 찾는다.

    자격 미달 항목도 사유와 함께 돌려준다. "왜 나는 이게 안 뜨죠?"에
    답하지 못하는 도구는 에이전트를 곤란하게 만든다.
    """
    applicable: list[Action] = []
    ineligible: list[Ineligible] = []

    for evaluate in _CATALOG:
        result = evaluate(profile, year)
        if isinstance(result, Ineligible):
            ineligible.append(result)
        elif result.benefit.amount_per_year > 0:
            applicable.append(result)
        else:
            ineligible.append(
                Ineligible(result.key, result.title, "계산된 절감액이 0원입니다.")
            )

    return Discovery(applicable=applicable, ineligible=ineligible)


def catalog_keys() -> list[str]:
    return [fn.__name__ for fn in _CATALOG]
