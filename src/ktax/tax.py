"""세액 계산. 규칙 기반이라 결정론적이고, 따라서 검증 가능하다.

이 모듈의 경계를 '세금 효과 계산'에 둔다. 상품 추천이나 투자 판단은
맞고 틀림이 명확하지 않아 같은 방식으로 검증할 수 없으므로 다루지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite

from ktax.models import BenefitStream, Profile, Rationale, Recurrence, Won
from ktax.rules import load_ruleset


# The legacy engine requires caller-computed totals. Both MCP and CLI must
# check the original keys before Profile defaults erase unknown vs zero.
BASELINE_INPUT_FIELDS = frozenset({
    "earned_income", "business_income", "financial_income", "other_income",
    "income_deductions", "tax_credits",
})


@dataclass(frozen=True)
class TaxEstimate:
    """총 세부담.

    산출세액이 아니라 실제로 부담하는 세금 전체를 담는다. 금융소득이
    종합과세 기준금액 이하면 그 세금은 원천징수로 끝나 산출세액에
    잡히지 않는데, 초과하면 산출세액 안으로 들어온다. 산출세액만
    보고하면 같은 이름의 숫자가 경계 양쪽에서 다른 것을 가리키게 되어
    비교가 불가능해진다 — 하필 그 경계가 우리가 감시하는 지점이다.
    """

    taxable_base: Won
    comprehensive_tax: Won   # 종합소득 결정세액 (세액공제 차감 후)
    separate_financial_tax: Won  # 분리과세로 종결된 금융소득세 (소득세분)
    local_income_tax: Won    # 지방소득세 (소득세 합계의 10%)
    rationale: Rationale
    financial_income_taxation: str = "separate"   # separate | general | comparative

    @property
    def income_tax(self) -> Won:
        return self.comprehensive_tax + self.separate_financial_tax

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


def taxable_base(profile: Profile, year: int) -> Won:
    """과세표준.

    금융소득은 종합과세 기준금액을 넘는 부분만 합산된다. 기준금액 이하는
    원천징수로 과세가 종결되어 종합소득에 들어가지 않는다.
    """
    rules = load_ruleset(year)["financial_income"]
    excess = max(0, profile.financial_income - rules["comprehensive_taxation_threshold"])
    return max(
        0, profile.comprehensive_income + excess - profile.income_deductions
    )


def gross_income_tax_for(profile: Profile, year: int) -> tuple[Won, str]:
    """소득세법 제62조에 따른 산출세액과 적용된 방식.

    금융소득이 종합과세 기준금액을 넘으면 두 가지로 계산해 큰 쪽을 택한다.
    종합과세가 오히려 원천징수보다 가벼워지는 역전을 막기 위한 장치이므로,
    둘 중 하나만 계산하면 세액이 과소 산출된다.
    """
    rules = load_ruleset(year)
    fin = rules["financial_income"]
    threshold = fin["comprehensive_taxation_threshold"]
    rate = fin["withholding_rate_income_tax"]
    other_base = profile.non_financial_taxable_base()

    if profile.financial_income <= threshold:
        # 원천징수로 과세 종결. 금융소득은 종합소득에 들어가지 않는다.
        return gross_income_tax(other_base, year), "separate"

    excess = profile.financial_income - threshold

    # ① 일반산출세액: 기준금액까지는 원천징수세율, 초과분은 다른 소득과 합산
    general = round(threshold * rate) + gross_income_tax(other_base + excess, year)

    # ② 비교산출세액: 금융소득 전체를 원천징수세율로 분리
    comparative = round(profile.financial_income * rate) + gross_income_tax(
        other_base, year
    )

    if general >= comparative:
        return general, "general"
    return comparative, "comparative"


def estimate_tax(profile: Profile, year: int) -> TaxEstimate:
    """베이스라인 세액. 모든 액션 비교의 기준점."""
    rules = load_ruleset(year)
    fin = rules["financial_income"]
    base = taxable_base(profile, year)
    gross, method = gross_income_tax_for(profile, year)
    determined = max(0, gross - profile.tax_credits)

    # 분리과세로 종결된 금융소득세. 세액공제 대상이 아니므로 따로 더한다.
    # 종합과세로 넘어가면 이 금액은 이미 산출세액 안에 들어가 있다.
    separate_tax = (
        round(profile.financial_income * fin["withholding_rate_income_tax"])
        if method == "separate"
        else 0
    )
    local = round((determined + separate_tax) * rules["local_income_tax_rate"])

    applied = [
        f"{year}년 종합소득세율표 (한계세율 {marginal_rate(base, year):.0%})",
        f"지방소득세 = 소득세 × {rules['local_income_tax_rate']:.0%}",
    ]
    assumptions = []

    if method == "separate":
        applied.append(
            f"금융소득 {profile.financial_income:,}원은 종합과세 기준금액 "
            f"{fin['comprehensive_taxation_threshold']:,}원 이하로 분리과세 종결 "
            f"(원천징수 {fin['withholding_rate']:.1%})"
        )
    else:
        applied.append(
            f"소득세법 제62조 — 일반산출세액과 비교산출세액 중 큰 쪽 적용 "
            f"(이번 계산은 {'일반' if method == 'general' else '비교'}산출세액)"
        )
        assumptions.append(
            "배당가산액(Gross-up)과 배당세액공제는 반영하지 않았습니다. "
            "둘은 서로 상쇄되는 항목이라 한쪽만 넣으면 오히려 부정확해집니다"
        )

    return TaxEstimate(
        taxable_base=base,
        comprehensive_tax=determined,
        separate_financial_tax=separate_tax,
        local_income_tax=local,
        financial_income_taxation=method,
        rationale=Rationale(
            summary=(
                f"과세표준 {base:,}원 → 산출세액 {gross:,}원, "
                f"세액공제 {profile.tax_credits:,}원 차감 후 결정세액 {determined:,}원"
            ),
            applied_rules=applied,
            assumptions=assumptions,
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
    before = estimate_tax(profile, year)
    after = estimate_tax(
        replace(profile, income_deductions=profile.income_deductions + max(0, deduction)),
        year,
    )
    return max(0, before.total - after.total)


def tax_credit_saving(profile: Profile, year: int, credit: Won) -> Won:
    """세액공제 절감액. 산출세액을 넘는 공제는 버려지므로 상한을 둔다."""
    before = estimate_tax(profile, year)
    after = estimate_tax(
        replace(profile, tax_credits=profile.tax_credits + max(0, credit)), year
    )
    return max(0, before.total - after.total)


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

    if type(additional_contribution) is not int or additional_contribution < 0:
        raise ValueError("추가 납입액은 음수가 아닌 정수 원 단위여야 합니다.")
    already = min(profile.pension_savings_contributed, pension["pension_savings_limit"]) + profile.irp_contributed
    room = max(0, pension["combined_limit_with_irp"] - already)
    effective = min(additional_contribution, room)
    to_pension = min(effective, max(0, pension["pension_savings_limit"] - profile.pension_savings_contributed))
    to_irp = effective - to_pension

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
    saving = tax_credit_saving(profile, year, credit)

    baseline = estimate_tax(profile, year)

    # 납입은 수령 개시 연령까지 매년 반복 가능하다.
    withdrawal_age = profile.planned_withdrawal_age or pension["withdrawal_start_age"]

    notes = [f"추가 납입은 연금저축 {to_pension:,}원, IRP {to_irp:,}원으로 배분한다고 가정"]
    if saving < credit + round(credit * rules["local_income_tax_rate"]):
        notes.append("공제 가능액 중 남은 세금에서 실제 사용할 수 있는 금액만 절감액에 반영")
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


@dataclass(frozen=True)
class ISASimulation:
    """올해 소득세와 독립적인, 보유기간 전체 투자 세금 비교."""

    normal_account_tax: Won
    isa_account_tax: Won
    projected_gain: Won
    holding_years: int
    benefit: BenefitStream
    rationale: Rationale
    tax_scope: str = "investment_holding_period"

    @property
    def total_saving(self) -> Won:
        return self.normal_account_tax - self.isa_account_tax


def simulate_isa_contribution(
    profile: Profile, year: int, additional_contribution: Won, expected_return_rate: float,
    holding_years: int = 3, existing_net_gain: Won = 0,
) -> ISASimulation:
    """추가 납입분을 단리로 운용하는 가정의 보유기간 전체 세금 비교.

    existing_net_gain은 추가 납입분을 제외한 계좌의 종료 시점 예상 순손익.
    일반계좌 수익은 전액 15.4% 원천징수 대상이라고 가정한다.
    """
    rules = load_ruleset(year)
    isa = rules["isa"]
    fin = rules["financial_income"]
    if type(additional_contribution) is not int or additional_contribution < 0:
        raise ValueError("additional_contribution은 0 이상의 정수여야 합니다.")
    if (type(expected_return_rate) not in (int, float)
            or not isfinite(expected_return_rate) or not 0 <= expected_return_rate <= 1):
        raise ValueError("expected_return_rate는 0~1 사이의 유한한 수여야 합니다.")
    if type(holding_years) is not int or not isa["minimum_holding_years"] <= holding_years <= 100:
        raise ValueError("holding_years는 의무가입기간 이상, 100 이하의 정수여야 합니다.")
    if type(existing_net_gain) is not int:
        raise ValueError("existing_net_gain은 원 단위 정수여야 합니다.")
    for amount in (profile.isa_contributed_this_year, profile.isa_contributed_total):
        if type(amount) is not int or amount < 0:
            raise ValueError("ISA 기존 납입액은 0 이상의 정수여야 합니다.")

    annual_room = max(0, isa["annual_contribution_limit"] - profile.isa_contributed_this_year)
    total_room = max(0, isa["total_contribution_limit"] - profile.isa_contributed_total)
    effective = min(additional_contribution, annual_room, total_room)
    gain = round(effective * expected_return_rate * holding_years)
    tax_free_limit = (
        isa["tax_free_limit_preferential"] if profile.isa_preferential
        else isa["tax_free_limit_general"]
    )

    def account_tax(net_gain: Won) -> Won:
        return round(max(0, net_gain - tax_free_limit) * isa["separate_tax_rate"])

    # 비과세 한도는 계좌 전체에 한 번만 적용한다. 기존 손익에 더해지는
    # 추가 납입분의 증분 세금만 일반계좌의 추가 투자 세금과 비교한다.
    isa_tax = account_tax(existing_net_gain + gain) - account_tax(existing_net_gain)
    normal_tax = round(gain * fin["withholding_rate"])
    saving = normal_tax - isa_tax
    return ISASimulation(
        normal_account_tax=normal_tax,
        isa_account_tax=isa_tax,
        projected_gain=gain,
        holding_years=holding_years,
        benefit=BenefitStream(
            amount_per_year=saving,
            recurrence=Recurrence.ONE_TIME,
            starts_in_years=holding_years,
        ),
        rationale=Rationale(
            summary=(
                f"ISA {effective:,}원 추가 납입, {holding_years}년 총 예상 수익 {gain:,}원: "
                f"일반계좌 대비 기간 전체 예상 절세액 {saving:,}원"
            ),
            applied_rules=[
                f"계좌 전체 순이익 비과세 한도 {tax_free_limit:,}원, 종료 시 한 번 적용",
                f"초과분 분리과세 {isa['separate_tax_rate']:.1%}",
                f"일반계좌 원천징수 {fin['withholding_rate']:.1%}",
            ],
            assumptions=[
                f"연 기대수익률 {expected_return_rate:.1%}, 단리·재투자 없음; 수익 보장 아님",
                f"추가 납입 외 계좌의 종료 시점 예상 순손익 {existing_net_gain:,}원 가정",
                "일반계좌 수익 전액이 원천징수 대상인 경우만 비교; 비과세 매매차익·종합과세 제외",
                "세금 차액은 종료 시점의 1회성 혜택으로 평가; 중간 납세 시점 차이는 미반영",
                "올해 연말정산 세금·환급액과 독립적인 투자 시나리오",
                "가입 자격 충족 및 정상 해지 가정; 이월 납입한도·수수료·세법 변경 미반영",
            ],
            ruleset_year=year,
            ruleset_verified=rules.get("verified", False),
        ),
    )


# --------------------------------------------------------------------------
# 신용카드 등 사용금액 소득공제 (조세특례제한법 제126조의2)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CardDeduction:
    deductible: Won              # 한도 적용 후 최종 소득공제액
    gross_deductible: Won        # 한도 적용 전 공제대상금액
    minimum_spending: Won        # 총급여의 25%
    base_limit: Won
    extra_limit: Won
    rationale: Rationale


def _card_buckets(profile: Profile, rules: dict) -> list[tuple[str, Won, float]]:
    """결제수단별 사용액을 공제율이 낮은 순서로 늘어놓는다.

    이 순서가 결과를 좌우한다. 최저사용금액은 공제율이 낮은 쪽부터 차감되므로,
    안분해서 빼는 구현은 공제액을 과소 계산한다.
    """
    rates = rules["rates"]
    buckets = [
        ("신용카드", profile.credit_card_spending, rates["credit_card"]),
        ("직불·현금영수증", profile.debit_cash_spending, rates["debit_and_cash_receipt"]),
    ]
    if profile.earned_income <= rules["culture_income_ceiling"]:
        buckets.append(("문화체육", profile.culture_spending, rates["culture"]))
    buckets.append(("전통시장", profile.traditional_market_spending, rates["traditional_market"]))
    buckets.append(("대중교통", profile.public_transit_spending, rates["public_transit"]))
    return sorted(buckets, key=lambda b: b[2])


_EXTRA_BUCKETS = {"전통시장", "대중교통", "문화체육"}


def credit_card_deduction(profile: Profile, year: int) -> CardDeduction:
    """신용카드 등 사용금액 소득공제액."""
    ruleset = load_ruleset(year)
    rules = ruleset["credit_card"]
    buckets = _card_buckets(profile, rules)

    minimum = round(profile.earned_income * rules["minimum_spending_rate"])
    total_spending = sum(amount for _, amount, _ in buckets)

    # 최저사용금액을 공제율이 낮은 순서로 소진시킨다.
    remaining_minimum = minimum
    gross = 0
    extra_portion = 0
    for name, amount, rate in buckets:
        consumed = min(amount, remaining_minimum)
        remaining_minimum -= consumed
        credited = round((amount - consumed) * rate)
        gross += credited
        if name in _EXTRA_BUCKETS:
            extra_portion += credited

    children_index = min(profile.dependent_children, 2)
    limits = (
        rules["base_limit_low_income"]
        if profile.earned_income <= rules["income_threshold"]
        else rules["base_limit_high_income"]
    )
    base_limit = limits[children_index]
    extra_limit = (
        rules["extra_limit_with_culture"]
        if profile.earned_income <= rules["culture_income_ceiling"]
        else rules["extra_limit_standard"]
    )

    base_applied = min(gross, base_limit)
    overflow = gross - base_applied
    extra_applied = min(overflow, extra_portion, extra_limit)
    deductible = base_applied + extra_applied

    if total_spending <= minimum:
        summary = (
            f"총 사용액 {total_spending:,}원이 최저사용금액 {minimum:,}원"
            f"(총급여의 {rules['minimum_spending_rate']:.0%}) 이하로 공제액이 없습니다"
        )
    else:
        summary = (
            f"공제대상금액 {gross:,}원 → 한도 적용 후 {deductible:,}원 소득공제"
        )

    return CardDeduction(
        deductible=deductible,
        gross_deductible=gross,
        minimum_spending=minimum,
        base_limit=base_limit,
        extra_limit=extra_limit,
        rationale=Rationale(
            summary=summary,
            applied_rules=[
                f"최저사용금액 = 총급여 × {rules['minimum_spending_rate']:.0%} = {minimum:,}원",
                "최저사용금액은 공제율이 낮은 결제수단부터 차감",
                f"기본한도 {base_limit:,}원 (자녀 {profile.dependent_children}명 기준)",
                f"추가한도 {extra_limit:,}원 (전통시장·대중교통"
                + ("·문화체육" if profile.earned_income <= rules["culture_income_ceiling"] else "")
                + ")",
            ],
            assumptions=[
                "소비증가분 추가공제는 반영하지 않았습니다 (연도별 한시 조치)",
            ],
            ruleset_year=year,
            ruleset_verified=ruleset.get("verified", False),
        ),
    )


# --------------------------------------------------------------------------
# 의료비 세액공제 (소득세법 제59조의4 제2항)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MedicalCredit:
    credit: Won                  # 세액공제액
    threshold: Won               # 총급여의 3%
    eligible_general: Won        # 한도 적용 후 그 밖의 의료비
    shortfall_applied: Won       # 한도 없는 호에서 차감된 미달분
    breakdown: dict[str, Won]
    rationale: Rationale


def medical_expense_credit(profile: Profile, year: int) -> MedicalCredit:
    """의료비 세액공제액.

    3% 기준선은 '그 밖의 의료비'에서 먼저 차감되고, 그것으로 모자라면
    한도 없는 호(본인·65세 이상·장애인 등, 미숙아, 난임)에서 마저 뺀다.
    한도(700만원)는 '그 밖의 의료비'에만 붙는다 — 모든 의료비에 씌우면
    부양가족 의료비가 큰 납세자의 공제액이 크게 과소 계산된다.
    """
    ruleset = load_ruleset(year)
    rules = ruleset["medical_expense"]
    threshold = round(profile.earned_income * rules["income_threshold_rate"])

    general_excess = profile.medical_expenses - threshold
    if general_excess >= 0:
        eligible_general = min(general_excess, rules["general_limit"])
        shortfall = 0
    else:
        eligible_general = 0
        shortfall = -general_excess

    # 법정 호 순서: 제2호 → 제3호 → 제4호. 각 호는 앞선 호의
    # 의료비 합계와 기준금액의 차이만 차감하므로 미달분을 중복 차감하지 않는다.
    buckets = [
        ("본인·65세이상·장애인 등", profile.medical_expenses_unlimited, rules["credit_rate"]),
        ("미숙아·선천성이상아", profile.medical_expenses_premature, rules["credit_rate_premature"]),
        ("난임시술비", profile.medical_expenses_fertility, rules["credit_rate_fertility"]),
    ]

    shortfall_applied = 0
    credit = round(eligible_general * rules["credit_rate"])
    breakdown = {"그 밖의 의료비": round(eligible_general * rules["credit_rate"])}

    remaining_shortfall = shortfall
    for name, amount, rate in buckets:
        consumed = min(amount, remaining_shortfall)
        remaining_shortfall -= consumed
        shortfall_applied += consumed
        part = round((amount - consumed) * rate)
        credit += part
        if amount:
            breakdown[name] = part

    total_spent = (
        profile.medical_expenses
        + profile.medical_expenses_unlimited
        + profile.medical_expenses_fertility
        + profile.medical_expenses_premature
    )
    if credit <= 0:
        summary = (
            f"의료비 {total_spent:,}원이 기준선 {threshold:,}원"
            f"(총급여의 {rules['income_threshold_rate']:.0%})을 넘지 못해 공제액이 없습니다"
        )
    else:
        summary = f"의료비 세액공제 {credit:,}원"

    applied = [
        f"기준금액 = 총급여 × {rules['income_threshold_rate']:.0%} = {threshold:,}원",
        f"그 밖의 의료비만 연 {rules['general_limit']:,}원 한도, "
        f"본인·65세 이상·장애인·미숙아·난임은 한도 없음",
        f"공제율 {rules['credit_rate']:.0%} "
        f"(난임 {rules['credit_rate_fertility']:.0%}, "
        f"미숙아·선천성이상아 {rules['credit_rate_premature']:.0%})",
    ]
    assumptions = []
    if shortfall_applied:
        applied.append(
            f"그 밖의 의료비가 기준금액에 미달해 {shortfall_applied:,}원을 "
            f"한도 없는 호에서 차감"
        )
        applied.append(
            "소득세법 제59조의4 제2항: 제3호는 제1·2호 의료비 합계, "
            "제4호는 제1~3호 의료비 합계의 기준금액 미달분만 차감"
        )

    return MedicalCredit(
        credit=credit,
        threshold=threshold,
        eligible_general=eligible_general,
        shortfall_applied=shortfall_applied,
        breakdown=breakdown,
        rationale=Rationale(
            summary=summary,
            applied_rules=applied,
            assumptions=assumptions,
            ruleset_year=year,
            ruleset_verified=ruleset.get("verified", False),
        ),
    )
