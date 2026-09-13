"""시나리오 하나를 끝까지 계산해 보여주는 CLI.

정확도를 검증하려면 숫자만으로는 부족하다. 어떤 규칙을 적용했고 무엇을
가정했는지가 함께 보여야 손으로 검산할 수 있다. 그래서 근거를 전부 찍는다.

    uv run python -m ktax examples/salaried_33.json
    uv run python -m ktax examples/salaried_33.json --view quick_wins
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from ktax.catalog import discover_actions
from ktax.models import FilingType, Profile
from ktax.monitor import evaluate_thresholds
from ktax.scoring import (
    score_actions,
    set_and_forget,
    sort_by_quick_wins,
    sort_by_value,
    urgent,
)
from ktax.sources import FieldSource, from_hometax, from_source, label_of, merge
from ktax.tax import BASELINE_INPUT_FIELDS, estimate_tax

VIEWS = {
    "value": sort_by_value,
    "quick_wins": sort_by_quick_wins,
    "set_and_forget": set_and_forget,
    "urgent": urgent,
}

_METHOD_LABEL = {
    "separate": "분리과세 종결",
    "general": "종합과세 · 일반산출세액",
    "comparative": "종합과세 · 비교산출세액",
}


def _rule(title: str) -> None:
    print(f"\n{title}\n{'─' * 72}")


def _won(value) -> str:
    return f"{value:,}원" if isinstance(value, int) else str(value)


def _load(raw: dict):
    year = raw["year"]

    if "records" in raw:
        parsed = []
        for record in raw["records"]:
            as_of = date.fromisoformat(record["as_of"])
            if "categories" in record:
                parsed.append(from_hometax(as_of, record["categories"]))
            else:
                parsed.append(
                    from_source(
                        FieldSource(record["source"]), as_of, record.get("values", {})
                    )
                )
        data = merge(*parsed)
        return year, data.to_profile(), data

    values = dict(raw["profile"])
    if "filing_type" in values:
        values["filing_type"] = FilingType(values["filing_type"])
    return year, Profile(**values), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ktax", description=__doc__)
    parser.add_argument("scenario", type=Path, help="시나리오 JSON 파일")
    parser.add_argument("--view", choices=sorted(VIEWS), default="value")
    parser.add_argument("--json", action="store_true", help="기계용 JSON 출력")
    args = parser.parse_args(argv)

    def input_error(message):
        payload = {"status": "invalid_input", "errors": [{"message": message}]}
        print(json.dumps(payload, ensure_ascii=False) if args.json else message)
        return 2

    try:
        raw = json.loads(args.scenario.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return input_error("시나리오 파일을 읽을 수 없거나 올바른 JSON이 아닙니다.")
    if not isinstance(raw, dict) or type(raw.get("year")) is not int:
        return input_error("시나리오 객체와 명시적인 정수 귀속연도가 필요합니다.")
    if sum(key in raw for key in ("settlement", "profile", "records")) != 1:
        return input_error("settlement, profile, records 중 한 입력 방식만 사용하세요.")
    if "settlement" in raw:
        from ktax.settlement import recommend_settlement_actions

        payload = recommend_settlement_actions(raw["settlement"], raw.get("year"))
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            baseline = payload["baseline"]
            _rule(f"근로자 세액 계산 ({raw.get('year')}년 귀속 · {baseline['status']})")
            if baseline["tax"]:
                print(f"  최종 세부담 {_won(baseline['tax']['total'])}")
            if baseline["settlement"] and baseline["settlement"]["refund"] is not None:
                print(f"  환급 예상 {_won(baseline['settlement']['refund'])}")
                print(
                    f"  추가 납부 예상 {_won(baseline['settlement']['additional_payment'])}"
                )
            for warning in baseline["warnings"]:
                print(f"  ! {warning}")
            for question in baseline["next_questions"]:
                print(f"  ? {question['question']}")
            for error in baseline["errors"]:
                print(f"  ! {error['field']}: {error['message']}")
            for action in payload["actions"]:
                print(
                    f"  · {action['key']}: 추가 절감 {_won(action['additional_saving'])}"
                )
        return (
            2
            if payload["status"]
            in {"invalid_input", "unsupported_year", "unsupported_scope"}
            else 0
        )

    from ktax.rules import available_years

    if raw["year"] not in available_years():
        payload = {
            "status": "unsupported_year",
            "available_years": available_years(),
            "tax": None,
        }
        print(
            json.dumps(payload, ensure_ascii=False)
            if args.json
            else "지원하지 않는 레거시 계산 연도입니다. 통합 settlement 경로를 확인하세요."
        )
        return 2
    try:
        year, profile, data = _load(raw)
    except (KeyError, TypeError, ValueError, AttributeError):
        return input_error("레거시 프로필 또는 자료 목록의 필드·형식을 확인하세요.")
    known = data.known() if data else frozenset(raw["profile"])
    try:
        discovery = discover_actions(profile, year, known=known)
    except ValueError as exc:
        return input_error(str(exc))
    values = {k: v.value for k, v in data.fields.items()} if data else raw["profile"]
    missing = sorted(k for k in BASELINE_INPUT_FIELDS if values.get(k) is None)
    conflicts = bool(data and data.conflicts)
    unsafe = missing or conflicts
    estimate = None if unsafe else estimate_tax(profile, year)
    scored = [] if unsafe else score_actions(discovery.applicable, profile)
    status = (
        "conflict" if conflicts else "indeterminate" if missing else "legacy_simulation"
    )
    tax_result = {"status": status, "total": None, "missing_fields": missing}
    if estimate:
        tax_result.update(
            {
                "taxable_base": estimate.taxable_base,
                "comprehensive_tax": estimate.comprehensive_tax,
                "separate_financial_tax": estimate.separate_financial_tax,
                "local_income_tax": estimate.local_income_tax,
                "total": estimate.total,
                "method": estimate.financial_income_taxation,
            }
        )

    if args.json:
        print(
            json.dumps(
                {
                    "status": status,
                    "warnings": [
                        "레거시 공제총액 입력 모드입니다. 실제 연말정산은 settlement 입력을 사용하세요."
                    ],
                    "year": year,
                    "tax": tax_result,
                    "actions": [s.to_dict() for s in VIEWS[args.view](scored)],
                    "ineligible": [i.to_dict() for i in discovery.ineligible],
                    "indeterminate": [i.to_dict() for i in discovery.indeterminate],
                    "thresholds": []
                    if unsafe
                    else [s.to_dict() for s in evaluate_thresholds(profile, year)],
                    "conflicts": [c.to_dict() for c in data.conflicts] if data else [],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(
        "레거시 공제총액 입력 시뮬레이션입니다. 실제 연말정산은 settlement 입력을 사용하세요."
    )
    _rule(f"세액 계산  ({year}년 귀속)")
    if not estimate:
        print("  세액·절감액 계산 보류: 핵심 입력 누락 또는 출처 충돌")
        for name in missing:
            print(f"  ? {label_of(name)}를 확인해주세요.")
    else:
        print(f"  과세표준           {_won(estimate.taxable_base)}")
        print(f"  종합소득 결정세액   {_won(estimate.comprehensive_tax)}")
        if estimate.separate_financial_tax:
            print(f"  분리과세 금융소득세 {_won(estimate.separate_financial_tax)}")
        print(f"  지방소득세         {_won(estimate.local_income_tax)}")
        print(f"  총 세부담          {_won(estimate.total)}")
        print(
            f"  금융소득 과세방식   {_METHOD_LABEL[estimate.financial_income_taxation]}"
        )
        print("\n  근거:")
        for line in estimate.rationale.applied_rules:
            print(f"    · {line}")
        for line in estimate.rationale.assumptions:
            print(f"    ~ {line}")
        for line in estimate.rationale.warnings():
            print(f"    ! {line}")

    _rule(f"절세 액션  (정렬: {args.view})")
    ranked = VIEWS[args.view](scored)
    if not ranked:
        print("  적용 가능한 액션이 없습니다.")
    for i, item in enumerate(ranked, 1):
        flag = " ★한번세팅" if item.is_set_and_forget else ""
        print(f"\n  {i}. {item.action.title}{flag}")
        print(
            f"     현재가치 {_won(item.present_value)}"
            f" · 연환산 {_won(item.value.annual_equivalent)}"
            f" · 노력 {item.effort_tier.value}"
        )
        print(
            f"     지평 {item.value.horizon.years}년 ({item.value.horizon.reason})"
            f" · 지속확률 {item.value.persistence:.0%}"
        )
        if item.action.deadline:
            days = item.days_until_deadline
            if days is None:
                when = ""
            elif days < 0:
                when = f" (마감 지남, {-days}일 전)"
            else:
                when = f" (D-{days})"
            print(f"     마감 {item.action.deadline}{when}")
        print(f"     {item.action.rationale.summary}")
        for line in item.action.rationale.applied_rules:
            print(f"       · {line}")
        for line in item.action.rationale.assumptions:
            print(f"       ~ {line}")

    _rule("임계값 감시")
    for signal in [] if unsafe else evaluate_thresholds(profile, year):
        mark = {"info": " ", "watch": "△", "act": "●"}[signal.severity.value]
        print(f"  {mark} {signal.title}: 여유 {_won(signal.headroom)}")
        print(f"      {signal.detail}")

    if discovery.ineligible:
        _rule("자격 미달")
        for item in discovery.ineligible:
            print(f"  · {item.title}: {item.reason}")

    if discovery.indeterminate:
        _rule("판단 불가 — 자료 부족")
        for item in discovery.indeterminate:
            names = ", ".join(label_of(n) for n in item.missing)
            print(f"  · {item.title}: {names}")

    if data:
        if data.missing():
            _rule("미확보 필드")
            print("  " + ", ".join(sorted(label_of(n) for n in data.missing())))
        if data.conflicts:
            _rule("출처 충돌")
            for conflict in data.conflicts:
                values = " vs ".join(
                    f"{c.value}({c.source.value})" for c in conflict.candidates
                )
                print(f"  · {label_of(conflict.field_name)}: {values}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
