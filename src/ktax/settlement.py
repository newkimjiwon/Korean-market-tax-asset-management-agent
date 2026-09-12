"""급여 원자료 → 공제·감면 → 결정세액 → 정산. 저장/네트워크 부작용 없음.

기존 Profile의 공제총액 입력 모드와 섞지 않는다. 명시적 입력만 계산하며
미확인 값은 질문으로 반환한다. 금액 계산에는 Decimal 및 원 미만 버림 사용.
"""

from __future__ import annotations

import calendar
import json
from copy import deepcopy
from datetime import date
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from ktax.settlement_schema import SCHEMA

RULES_DIR = Path(__file__).parent / "rules" / "settlement"


def won(value):
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_DOWN))


def pct(value, rate):
    return won(Decimal(value) * Decimal(str(rate)) / 100)


def available_settlement_years():
    return sorted(int(p.stem) for p in RULES_DIR.glob("*.json"))


class Inputs:
    def __init__(self):
        self.missing = []
        self.errors = []

    def issue(self, path, message):
        self.errors.append({"field": path, "message": message})

    def validate(self, data, schema, prefix=""):
        if not isinstance(data, dict):
            self.issue(prefix, "객체가 필요합니다.")
            return
        for key in data.keys() - schema.keys():
            self.issue(
                prefix + str(key),
                "지원하지 않는 필드입니다. describe_settlement_fields를 확인하세요.",
            )
        for key, spec in schema.items():
            path = prefix + key
            value = data.get(key)
            # Only requiredness is conditional. Supplied values are always validated.
            required = not spec.get("optional")
            if prefix == "sme." and key != "status":
                required = required and data.get("status") in (
                    "applied",
                    "eligible_unclaimed",
                )
            if prefix == "rent." and key != "payments":
                required = required and bool(data.get("payments"))
                if (
                    key == "head_claims_housing"
                    and data.get("household_role") != "member"
                ):
                    required = False
            if value is None:
                if required:
                    self.missing.append(
                        {
                            "field": path,
                            "question": spec["question"],
                            "sources": [spec["source"]],
                        }
                    )
                continue
            kind = spec["type"]
            valid = True
            if kind == "money":
                valid = type(value) is int and 0 <= value <= 9_000_000_000_000_000
            elif kind == "boolean":
                valid = type(value) is bool
            elif kind == "string":
                valid = isinstance(value, str) and bool(value.strip())
            elif kind == "enum":
                valid = type(value) in (str, int) and value in spec["values"]
            elif kind in ("date", "month"):
                try:
                    parsed = date.fromisoformat(
                        value + "-01" if kind == "month" else value
                    )
                    valid = value == (
                        parsed.isoformat()[:7]
                        if kind == "month"
                        else parsed.isoformat()
                    )
                except (ValueError, TypeError):
                    valid = False
            elif kind == "object":
                self.validate(value, spec["properties"], path + ".")
            elif kind == "array":
                valid = isinstance(value, list)
                if valid:
                    for i, item in enumerate(value):
                        self.validate(item, spec["items"], f"{path}[{i}].")
            if not valid:
                self.issue(
                    path,
                    f"{kind} 형식이 필요합니다. 금액은 음수가 아닌 정수 원 단위입니다.",
                )


def _gross_tax(base, rules):
    for row in rules["income_tax_brackets"]:
        if row["upper"] is None or base <= row["upper"]:
            return max(
                0,
                won(
                    Decimal(base) * Decimal(str(row["rate"]))
                    - row["progressive_deduction"]
                ),
            )
    raise AssertionError("마지막 세율 구간이 필요합니다")


def _earned_deduction(salary, rules):
    for upper, lower, fixed, rate in rules["earned_deduction"]:
        if upper is None or salary <= upper:
            return min(
                salary, rules["earned_deduction_cap"], fixed + pct(salary - lower, rate)
            )
    raise AssertionError("마지막 근로소득공제 구간이 필요합니다")


def _earned_credit(salary, gross, reduction, rules):
    r = rules["earned_credit"]
    credit = (
        pct(gross, r["low_rate"])
        if gross <= r["threshold"]
        else r["high_base"] + pct(gross - r["threshold"], r["high_rate"])
    )
    for upper, initial, lower, slope, minimum in r["caps"]:
        if upper is None or salary <= upper:
            cap = max(
                minimum,
                won(Decimal(initial) - Decimal(salary - lower) * Decimal(str(slope))),
            )
            credit = min(credit, cap)
            break
    return credit * (gross - reduction) // gross if gross else 0


def _period(start, end, path, inputs):
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if a > b:
        inputs.issue(path, "시작일이 종료일보다 늦습니다.")
    return a, b


def _covered(start, end, periods):
    cursor = start
    for a, b in sorted(periods):
        if a > cursor:
            break
        if b >= end:
            return True
        if b >= cursor:
            cursor = date.fromordinal(b.toordinal() + 1)
    return False


def _rent_amount(data, year, salary, rules, inputs, periods):
    rent = data["rent"]
    result = {
        "paid": 0,
        "support": 0,
        "eligible": 0,
        "credit": 0,
        "excluded": [],
        "status": "not_claimed",
    }
    if not rent["payments"]:
        if rent.get("support"):
            inputs.issue(
                "rent.support", "월세 납부내역 없이 지원금만 입력할 수 없습니다."
            )
        return result
    if rent["household_role"] == "spouse_exception":
        result["status"] = "unsupported_scope"
        return result
    household_ok = rent["homeless_household"] and (
        rent["household_role"] == "head" or not rent["head_claims_housing"]
    )
    leases = {}
    for i, lease in enumerate(rent["leases"]):
        if lease["id"] in leases:
            inputs.issue(f"rent.leases[{i}].id", "계약 식별자가 중복됐습니다.")
        a, b = _period(lease["start"], lease["end"], f"rent.leases[{i}]", inputs)
        c, d = _period(
            lease["registered_from"],
            lease["registered_until"],
            f"rent.leases[{i}]",
            inputs,
        )
        leases[lease["id"]] = (lease, max(a, c), min(b, d))
    supports = {}
    for support in rent["support"]:
        key = (support["lease_id"], support["month"])
        if support["lease_id"] not in leases:
            inputs.issue("rent.support", "지원금의 계약 식별자가 존재하지 않습니다.")
        if key in supports:
            inputs.issue(
                "rent.support",
                "계약별 지원 대상 월은 한 행으로 합산하세요. 중복 차감을 방지합니다.",
            )
        supports[key] = support["amount"]
    seen = set()
    for i, pay in enumerate(rent["payments"]):
        path = f"rent.payments[{i}]"
        key = (pay["lease_id"], pay["month"])
        if key in seen:
            inputs.issue(
                path, "계약별 대상 월은 한 행으로 합산하세요. 중복 계산을 방지합니다."
            )
        seen.add(key)
        if pay["lease_id"] not in leases:
            inputs.issue(path + ".lease_id", "존재하지 않는 계약입니다.")
            continue
        target = date.fromisoformat(pay["month"] + "-01")
        paid = date.fromisoformat(pay["paid_on"])
        if target.year != paid.year:
            inputs.issue(
                path,
                "연도를 넘긴 선납·연체 월세는 귀속 판단 후 별도 검토가 필요합니다.",
            )
            continue
        if paid.year != year:
            continue
        result["paid"] += pay["amount"]
        support = supports.get(key, 0)
        result["support"] += support
        if support > pay["amount"]:
            inputs.issue(path, "지원액이 해당 월 납부 월세를 초과합니다.")
            continue
        lease, a, b = leases[pay["lease_id"]]
        end = target.replace(day=calendar.monthrange(target.year, target.month)[1])
        overlap_start, overlap_end = max(target, a), min(end, b)
        overlaps = overlap_start <= overlap_end and any(
            x <= overlap_end and y >= overlap_start for x, y in periods
        )
        if (
            not household_ok
            or not lease["contract_eligible"]
            or not lease["home_eligible"]
            or not overlaps
        ):
            result["excluded"].append(
                {"month": pay["month"], "reason": "주거·계약·전입·근로기간 요건 미충족"}
            )
            continue
        whole = a <= target and b >= end and _covered(target, end, periods)
        eligible = pay["amount"]
        if not whole and pay.get("eligible_amount") is None:
            inputs.missing.append(
                {
                    "field": path + ".eligible_amount",
                    "question": "월중 조건 변경이 있습니다. 요건 충족 기간에 해당하는 월세액(지원금 차감 전)은 얼마인가요?",
                    "sources": ["contract", "payroll"],
                }
            )
            continue
        if pay.get("eligible_amount") is not None:
            eligible = pay["eligible_amount"]
            if eligible > pay["amount"]:
                inputs.issue(
                    path + ".eligible_amount", "적격 월세가 납부액을 초과합니다."
                )
        eligible_support = support
        if (
            eligible < pay["amount"]
            and support
            and pay.get("eligible_support_amount") is None
        ):
            inputs.missing.append(
                {
                    "field": path + ".eligible_support_amount",
                    "question": "공적 지원금 중 공제요건 충족 기간의 월세에 해당하는 지원액은 얼마인가요?",
                    "sources": ["support_notice", "contract"],
                }
            )
            continue
        if pay.get("eligible_support_amount") is not None:
            eligible_support = pay["eligible_support_amount"]
        if (
            not (0 <= eligible_support <= min(support, eligible))
            or support - eligible_support > pay["amount"] - eligible
        ):
            inputs.issue(
                path + ".eligible_support_amount",
                "적격·비적격 기간별 지원액이 지원 총액 또는 각 기간의 월세액과 맞지 않습니다.",
            )
            continue
        result["eligible"] += eligible - eligible_support
    for key in supports.keys() - seen:
        if int(key[1][:4]) == year:
            inputs.issue("rent.support", "지원 대상 월의 월세 납부 내역이 없습니다.")
    r = rules["rent"]
    if salary <= r["earned_income_ceiling"]:
        rate = (
            r["credit_rate_low_income"]
            if salary <= r["low_income_earned_income_ceiling"]
            else r["credit_rate_high_income"]
        )
        result["credit"] = won(
            Decimal(min(result["eligible"], r["annual_rent_limit"]))
            * Decimal(str(rate))
        )
    result["status"] = "eligible" if result["credit"] else "ineligible"
    return result


def calculate_settlement(data: dict, year: int) -> dict:
    """미확인 입력은 세액을 만들지 않는다. 환급 자료 부족은 별도 상태로 반환."""
    result = {
        "year": year,
        "status": "indeterminate",
        "tax": None,
        "settlement": None,
        "missing_fields": [],
        "errors": [],
        "next_questions": [],
        "warnings": [],
    }
    if type(year) is not int or year not in available_settlement_years():
        result.update(
            status="unsupported_year", available_years=available_settlement_years()
        )
        return result
    rules = json.loads((RULES_DIR / f"{year}.json").read_text())
    result["rules"] = {
        k: rules[k]
        for k in ("scope", "verified", "reviewed_on", "sources", "limitations")
    }
    if not rules["verified"]:
        result["warnings"].append(rules["verification_note"])
    inputs = Inputs()
    inputs.validate(data, SCHEMA)

    def finish():
        result["missing_fields"] = [m["field"] for m in inputs.missing]
        result["next_questions"] = inputs.missing[:3]
        result["errors"] = inputs.errors
        if inputs.errors:
            result.update(status="invalid_input", tax=None, settlement=None)
        elif inputs.missing:
            result.update(status="indeterminate", tax=None, settlement=None)
        return result

    if inputs.errors:
        return finish()
    if data.get("resident") is False or data.get("other_income_present") is True:
        result.update(
            status="unsupported_scope",
            warnings=result["warnings"]
            + [
                "혼합소득·비거주자 정산은 지원 범위 밖입니다. 일반 근로소득 계산으로 대체하지 않습니다."
            ],
        )
        return result
    if inputs.missing:
        return finish()
    salary = data["gross_salary"] - data["non_taxable_salary"]
    if salary < 0:
        inputs.issue("non_taxable_salary", "비과세가 전체 급여보다 큽니다.")
    periods = [
        _period(p["start"], p["end"], f"employment_periods[{i}]", inputs)
        for i, p in enumerate(data["employment_periods"])
    ]
    annual_start, annual_end = date(year, 1, 1), date(year, 12, 31)
    if salary > 0 and not any(
        a <= annual_end and b >= annual_start for a, b in periods
    ):
        inputs.issue(
            "employment_periods", "급여가 있으나 해당 연도의 근로기간이 없습니다."
        )
    sme = data["sme"]
    if sme["status"] != "not_applicable":
        a, b = _period(sme["start"], sme["end"], "sme", inputs)
        if not sme["eligibility_confirmed"]:
            inputs.missing.append(
                {
                    "field": "sme.eligibility_confirmed",
                    "question": "감면 대상 기업·취업 당시 연령·이전 이력 및 감면기간을 회사에 확인해주세요.",
                    "sources": ["payroll"],
                }
            )
        if sme["eligible_salary"] > salary:
            inputs.issue("sme.eligible_salary", "감면 대상 급여가 총급여보다 큽니다.")
        if sme["eligible_salary"] and (
            a > annual_end
            or b < annual_start
            or not any(
                max(a, x, annual_start) <= min(b, y, annual_end) for x, y in periods
            )
        ):
            inputs.issue(
                "sme.eligible_salary", "감면·근로기간과 귀속연도가 겹치지 않습니다."
            )
        result["warnings"].append(
            "감면 대상 급여는 회사가 확인한 기간별 급여를 사용합니다. 연간 급여를 월수로 자동 안분하지 않습니다."
        )
    if inputs.errors or inputs.missing:
        return finish()
    rent = _rent_amount(data, year, salary, rules, inputs, periods)
    if rent["status"] == "unsupported_scope":
        result["status"] = "unsupported_scope"
        result["warnings"].append(
            "별도 주소 배우자 월세 특례는 합산 한도·배우자 자료를 확인해야 합니다."
        )
        return result
    if inputs.errors or inputs.missing:
        return finish()
    result["rent"] = rent
    result["evidence"] = deepcopy(data.get("evidence") or [])
    result["warnings"].append(
        "부양가족과 추가 공제는 자격·개별 한도를 확인한 입력만 사용합니다. 월세 부분기간의 지원액은 확인된 배분을 사용합니다."
    )
    deduction = _earned_deduction(salary, rules)
    earned = salary - deduction
    basic = rules["basic_deduction"] * (1 + data["eligible_dependents"])
    special_insurance = sum(
        data[k] for k in ("health_insurance", "long_term_care", "employment_insurance")
    )
    pension = rules["pension"]
    pension_amount = min(
        min(data["pension_savings"], pension["pension_savings_limit"]) + data["irp"],
        pension["combined_limit_with_irp"],
    )
    pension_rate = (
        pension["credit_rate_low_income"]
        if salary <= pension["low_income_earned_income_ceiling"]
        else pension["credit_rate_high_income"]
    )
    pension_credit = won(Decimal(pension_amount) * Decimal(str(pension_rate)))

    def route(standard):
        deductions = [
            x for x in data["other_deductions"] if not (standard and x["special"])
        ]
        extra = sum(x["amount"] for x in deductions if not x["limited"]) + min(
            sum(x["amount"] for x in deductions if x["limited"]),
            rules["deduction_overall_cap"],
        )
        base = max(
            0,
            earned
            - basic
            - data["employee_pension"]
            - extra
            - (0 if standard else special_insurance),
        )
        gross = _gross_tax(base, rules)
        reduction = 0
        if sme["status"] == "applied" and salary:
            reduction = min(
                rules["sme_annual_cap"],
                gross * sme["eligible_salary"] * sme["rate"] // (salary * 100),
            )
        earned_credit = _earned_credit(salary, gross, reduction, rules)
        remaining = gross - reduction
        credits = []
        for name, candidate in [
            ("earned_income", earned_credit),
            ("pension", pension_credit),
            (
                "other",
                sum(
                    x["amount"]
                    for x in data["other_credits"]
                    if not (standard and x["special"])
                ),
            ),
            ("standard", rules["standard_credit"] if standard else 0),
            (
                "rent",
                0
                if standard or data["rent"].get("status") != "applied"
                else rent["credit"],
            ),
        ]:
            used = min(remaining, candidate)
            credits.append(
                {
                    "key": name,
                    "available": candidate,
                    "used": used,
                    "unused": candidate - used,
                }
            )
            remaining -= used
        local = won(Decimal(remaining) * Decimal(rules["local_rate"]))
        return {
            "method": "standard" if standard else "itemized",
            "taxable_salary": salary,
            "earned_income_deduction": deduction,
            "earned_income": earned,
            "basic_deduction": basic,
            "employee_pension_deduction": data["employee_pension"],
            "special_insurance_deduction": 0 if standard else special_insurance,
            "other_deductions": extra,
            "taxable_base": base,
            "gross_income_tax": gross,
            "sme_reduction": reduction,
            "credits": credits,
            "income_tax": remaining,
            "local_income_tax": local,
            "total": remaining + local,
        }

    routes = [route(False), route(True)]
    tax = min(routes, key=lambda x: x["total"])
    result["tax"] = tax
    result["alternatives"] = [
        {"method": r["method"], "total": r["total"]} for r in routes
    ]
    has_estimates = any(
        item["source"] == "estimated" for item in (data.get("evidence") or [])
    )
    if has_estimates:
        result["warnings"].append(
            "추정 출처의 입력이 포함되어 확정 실적으로 처리하지 않습니다."
        )
    result["status"] = (
        "complete"
        if rules["verified"] and data["amount_basis"] == "actual" and not has_estimates
        else "provisional"
    )
    result["amount_basis"] = data["amount_basis"]
    result["sme_status"] = sme["status"]
    withheld = ("withheld_income_tax", "withheld_local_tax")
    gaps = [k for k in withheld if data.get(k) is None]
    if gaps:
        result["missing_fields"] = gaps
        result["settlement"] = {
            "status": "indeterminate",
            "refund": None,
            "additional_payment": None,
        }
        result["next_questions"] += [
            {
                "field": k,
                "question": SCHEMA[k]["question"],
                "sources": [SCHEMA[k]["source"]],
            }
            for k in gaps
        ]
    else:
        national_due = tax["income_tax"] - data["withheld_income_tax"]
        local_due = tax["local_income_tax"] - data["withheld_local_tax"]
        due = national_due + local_due
        result["settlement"] = {
            "status": result["status"],
            "income_tax_due": national_due,
            "local_tax_due": local_due,
            "net_due": due,
            "refund": max(0, -due),
            "additional_payment": max(0, due),
        }
    return result


def compare_settlements(before: dict, after: dict, year: int) -> dict:
    """여러 변경을 함께 재계산한다. 독립 절감액을 합산하지 않는다."""
    a, b = calculate_settlement(before, year), calculate_settlement(after, year)
    if a["tax"] is None or b["tax"] is None:
        status = next(
            s
            for s in (
                "invalid_input",
                "unsupported_year",
                "unsupported_scope",
                "indeterminate",
            )
            if s in (a["status"], b["status"])
        )
        return {
            "status": status,
            "additional_saving": None,
            "before": a,
            "after": b,
        }
    if a["amount_basis"] != b["amount_basis"]:
        return {
            "status": "invalid_input",
            "additional_saving": None,
            "errors": [
                {
                    "field": "amount_basis",
                    "message": "실적과 연간 예상을 같은 기준으로 맞춰주세요.",
                }
            ],
        }
    return {
        "status": "provisional"
        if "provisional" in (a["status"], b["status"])
        else "complete",
        "additional_saving": a["tax"]["total"] - b["tax"]["total"],
        "before": a,
        "after": b,
    }


def recommend_settlement_actions(data: dict, year: int) -> dict:
    """기존 혜택을 유지한 채 신청/추가납입의 한계 효과만 비교한다."""
    baseline = calculate_settlement(data, year)
    if baseline["tax"] is None:
        return {"status": baseline["status"], "baseline": baseline, "actions": []}
    actions = []
    if data["sme"]["status"] == "eligible_unclaimed":
        changed = deepcopy(data)
        changed["sme"]["status"] = "applied"
        comparison = compare_settlements(data, changed, year)
        actions.append(
            {"key": "claim_sme", "additional_saving": comparison["additional_saving"]}
        )
    if (
        data["rent"].get("status") == "eligible_unclaimed"
        and baseline["rent"]["credit"]
    ):
        changed = deepcopy(data)
        changed["rent"]["status"] = "applied"
        comparison = compare_settlements(data, changed, year)
        actions.append(
            {"key": "claim_rent", "additional_saving": comparison["additional_saving"]}
        )
    rules = json.loads((RULES_DIR / f"{year}.json").read_text())["pension"]
    space = max(
        0,
        rules["combined_limit_with_irp"]
        - min(data["pension_savings"], rules["pension_savings_limit"])
        - data["irp"],
    )
    if space:
        changed = deepcopy(data)
        changed["irp"] += space
        comparison = compare_settlements(data, changed, year)
        actions.append(
            {
                "key": "additional_irp",
                "contribution": space,
                "additional_saving": comparison["additional_saving"],
            }
        )
    return {
        "status": baseline["status"],
        "baseline": baseline,
        "actions": actions,
        "already_applied": ["sme"] if data["sme"]["status"] == "applied" else [],
        "note": "각 항목은 같은 기준 대비 독립 시나리오이며 합산하지 않습니다. 함께 적용하려면 compare_settlements를 사용하세요. 절감액 0은 자격 미달을 뜻하지 않습니다.",
    }
