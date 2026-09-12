"""세액 계산. 규칙 기반이라 결정론적이고, 따라서 검증 가능하다.

이 모듈의 경계를 '세금 효과 계산'에 둔다. 상품 추천이나 투자 판단은
맞고 틀림이 명확하지 않아 같은 방식으로 검증할 수 없으므로 다루지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ktax.models import BenefitStream, Profile, Rationale, Recurrence, Won
from ktax.rules import load_ruleset


@dataclass(frozen=True)
class TaxEstimate:
    taxable_base: Won
    income_tax: Won          # 산출세액에서 세액공제를 뺀 결정세액
    local_income_tax: Won    # 지방소득세 (소득세의 10%)
    rationale: Rationale

    @property
    def total(self) -> Won:
        return self.income_tax + self.local_income_tax


@dataclass(frozen=True)
class ActionSimulation:
    """액션을 적용했을 때의 세금 변화."""

    baseline_total: Won
    simulated_total: Won
    benefit: BenefitStream
    rationale: Rationale

    @property
    def annual_saving(self) -> Won:
        return self.baseline_total - self.simulated_total


def marginal_rate(taxable_base: Won, year: int) -> float:
    """한계세율 (지방소득세 제외)."""
    rules = load_ruleset(year)
    for bracket in rules["income_tax_brackets"]:
        upper = bracket["upper"]
        if upper is None or taxable_base <= upper:
            return bracket["rate"]
    raise AssertionError("마지막 구간의 upper 는 null 이어야 합니다")


def gross_income_tax(taxable_base: Won, year: int) -> Won:
    """산출세액. 누진공제 방식으로 계산한다."""
    rules = load_ruleset(year)
    for bracket in rules["income_tax_brackets"]:
        upper = bracket["upper"]
        if upper is None or taxable_base <= upper:
            tax = taxable_base * bracket["rate"] - bracket["progressive_deduction"]
            return max(0, round(tax))
    raise AssertionError("마지막 구간의 upper 는 null 이어야 합니다")


def estimate_tax(profile: Profile, year: int) -> TaxEstimate:
    """베이스라인 세액. 모든 액션 비교의 기준점."""
    rules = load_ruleset(year)
    base = profile.taxable_base()
    gross = gross_income_tax(base, year)
    determined = max(0, gross - profile.tax_credits)
    local = round(determined * rules["local_income_tax_rate"])

    return TaxEstimate(
        taxable_base=base,
        income_tax=determined,
        local_income_tax=local,
        rationale=Rationale(
            summary=(
                f"과세표준 {base:,}원 → 산출세액 {gross:,}원, "
                f"세액공제 {profile.tax_credits:,}원 차감 후 결정세액 {determined:,}원"
            ),
            applied_rules=[
                f"{year}년 종합소득세율표 (한계세율 {marginal_rate(base, year):.0%})",
                f"지방소득세 = 소득세 × {rules['local_income_tax_rate']:.0%}",
            ],
            assumptions=[
                "금융소득 종합과세 합산은 별도 판정 필요 (evaluate_thresholds 참고)",
            ],
            ruleset_year=year,
            ruleset_verified=rules.get("verified", False),
        ),
    )


# --------------------------------------------------------------------------
# 절감액 원시 계산
#
# 소득공제와 세액공제는 계산 방식이 근본적으로 다르다.
# 소득공제는 과세표준을 줄이므로 한계세율에 비례하고(= 고소득자에게 더 유리),
# 세액공제는 세액을 직접 줄이므로 소득과 무관하게 정해진 율이 적용된다.
# 이 둘을 섞으면 액션 간 비교가 전부 틀어진다.
# --------------------------------------------------------------------------

def income_deduction_saving(profile: Profile, year: int, deduction: Won) -> Won:
    """소득공제 절감액.

    한계세율을 곱하지 않고 실제로 두 번 계산해서 차분한다. 공제액이 구간
    경계를 걸치면 단일 한계세율을 곱한 값이 틀리기 때문이다.
    """
    rules = load_ruleset(year)
    base = profile.taxable_base()
    before = gross_income_tax(base, year)
    after = gross_income_tax(max(0, base - max(0, deduction)), year)
    diff = max(0, before - after)
    return diff + round(diff * rules["local_income_tax_rate"])


def tax_credit_saving(profile: Profile, year: int, credit: Won) -> Won:
    """세액공제 절감액. 산출세액을 넘는 공제는 버려지므로 상한을 둔다."""
    rules = load_ruleset(year)
    gross = gross_income_tax(profile.taxable_base(), year)
    headroom = max(0, gross - profile.tax_credits)
    effective = min(max(0, credit), headroom)
    return effective + round(effective * rules["local_income_tax_rate"])


# --------------------------------------------------------------------------
# 액션 시뮬레이터
# --------------------------------------------------------------------------

def simulate_pension_contribution(
    profile: Profile, year: int, additional_contribution: Won
) -> ActionSimulation:
    """연금저축/IRP 추가 납입의 세액공제 효과.

    세액공제이므로 한계세율과 무관하게 정해진 공제율이 적용된다.
    노력은 자동이체 한 번(1회), 효과는 납입하는 매년(반복) — 즉
    SET_AND_FORGET 사분면에 해당한다.
    """
    rules = load_ruleset(year)
    pension = rules["pension_account"]

    already = profile.pension_savings_contributed + profile.irp_contributed
    room = max(0, pension["combined_limit_with_irp"] - already)
    effective = min(additional_contribution, room)

    if profile.earned_income > 0:
        low_income = profile.earned_income <= pension["low_income_earned_income_ceiling"]
        basis = f"총급여 {profile.earned_income:,}원"
    else:
        low_income = (
            profile.comprehensive_income
            <= pension["low_income_comprehensive_income_ceiling"]
        )
        basis = f"종합소득금액 {profile.comprehensive_income:,}원"

    rate = (
        pension["credit_rate_low_income"] if low_income
        else pension["credit_rate_high_income"]
    )
    credit = round(effective * rate)
    saving = credit + round(credit * rules["local_income_tax_rate"])

    baseline = estimate_tax(profile, year)

    # 납입은 수령 개시 연령까지 매년 반복 가능하다.
    withdrawal_age = profile.planned_withdrawal_age or pension["withdrawal_start_age"]

    notes = []
    if effective < additional_contribution:
        notes.append(
            f"요청 {additional_contribution:,}원 중 한도 여유 {room:,}원까지만 반영"
        )

    return ActionSimulation(
        baseline_total=baseline.total,
        simulated_total=baseline.total - saving,
        benefit=BenefitStream(
            amount_per_year=saving,
            recurrence=Recurrence.RECURRING,
            ends_at_age=withdrawal_age,
        ),
        rationale=Rationale(
            summary=(
                f"연금계좌 {effective:,}원 납입 → 세액공제 {credit:,}원, "
                f"지방소득세 포함 연 {saving:,}원 절감"
            ),
            applied_rules=[
                f"연금저축 한도 {pension['pension_savings_limit']:,}원 / "
                f"IRP 합산 한도 {pension['combined_limit_with_irp']:,}원",
                f"세액공제율 {rate:.0%} ({basis} 기준)",
            ],
            assumptions=[
                f"납입은 {withdrawal_age}세까지 매년 유지한다고 가정",
                "중도해지 시 기타소득세 추징은 반영하지 않음",
                *notes,
            ],
            ruleset_year=year,
            ruleset_verified=rules.get("verified", False),
        ),
    )


def simulate_isa_contribution(
    profile: Profile, year: int, additional_contribution: Won, expected_return_rate: float
) -> ActionSimulation:
    """ISA 납입의 금융소득 과세 절감 효과.

    일반 계좌였다면 원천징수될 세금과 ISA 내 비과세/분리과세의 차액.
    """
    rules = load_ruleset(year)
    isa = rules["isa"]
    fin = rules["financial_income"]

    annual_room = max(0, isa["annual_contribution_limit"] - profile.isa_contributed_this_year)
    total_room = max(0, isa["total_contribution_limit"] - profile.isa_contributed_total)
    effective = min(additional_contribution, annual_room, total_room)

    gain = round(effective * expected_return_rate)
    tax_free_limit = (
        isa["tax_free_limit_preferential"] if profile.isa_preferential
        else isa["tax_free_limit_general"]
    )
    taxable_gain = max(0, gain - tax_free_limit)

    isa_tax = round(taxable_gain * isa["separate_tax_rate"])
    normal_tax = round(gain * fin["withholding_rate"])
    saving = max(0, normal_tax - isa_tax)

    baseline = estimate_tax(profile, year)

    return ActionSimulation(
        baseline_total=baseline.total,
        simulated_total=baseline.total - saving,
        benefit=BenefitStream(
            amount_per_year=saving,
            recurrence=Recurrence.RECURRING,
            explicit_years=isa["minimum_holding_years"],
        ),
        rationale=Rationale(
            summary=(
                f"ISA {effective:,}원 납입, 연 수익 {gain:,}원 가정 시 "
                f"일반계좌 대비 연 {saving:,}원 절감"
            ),
            applied_rules=[
                f"비과세 한도 {tax_free_limit:,}원"
                f"({'서민형' if profile.isa_preferential else '일반형'})",
                f"초과분 분리과세 {isa['separate_tax_rate']:.1%}",
                f"일반계좌 원천징수 {fin['withholding_rate']:.1%}",
            ],
            assumptions=[
                f"기대수익률 {expected_return_rate:.1%}는 사용자가 제시한 가정값이며 "
                f"보장된 수치가 아님",
                f"의무가입 {isa['minimum_holding_years']}년 유지 가정",
            ],
            ruleset_year=year,
            ruleset_verified=rules.get("verified", False),
        ),
    )
