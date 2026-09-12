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
from ktax.tax import estimate_tax

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


def _load(path: Path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    year = raw.get("year", 2025)

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

    year, profile, data = _load(args.scenario)
    known = data.known() if data else None
    discovery = discover_actions(profile, year, known=known)
    scored = score_actions(discovery.applicable, profile)
    estimate = estimate_tax(profile, year)

    if args.json:
        print(json.dumps({
            "year": year,
            "tax": {
                "taxable_base": estimate.taxable_base,
                "comprehensive_tax": estimate.comprehensive_tax,
                "separate_financial_tax": estimate.separate_financial_tax,
                "local_income_tax": estimate.local_income_tax,
                "total": estimate.total,
                "method": estimate.financial_income_taxation,
            },
            "actions": [s.to_dict() for s in VIEWS[args.view](scored)],
            "ineligible": [i.to_dict() for i in discovery.ineligible],
            "indeterminate": [i.to_dict() for i in discovery.indeterminate],
            "thresholds": [s.to_dict() for s in evaluate_thresholds(profile, year)],
            "conflicts": [c.to_dict() for c in data.conflicts] if data else [],
        }, ensure_ascii=False, indent=2))
        return 0

    _rule(f"세액 계산  ({year}년 귀속)")
    print(f"  과세표준           {_won(estimate.taxable_base)}")
    print(f"  종합소득 결정세액   {_won(estimate.comprehensive_tax)}")
    if estimate.separate_financial_tax:
        print(f"  분리과세 금융소득세 {_won(estimate.separate_financial_tax)}")
    print(f"  지방소득세         {_won(estimate.local_income_tax)}")
    print(f"  총 세부담          {_won(estimate.total)}")
    print(f"  금융소득 과세방식   {_METHOD_LABEL[estimate.financial_income_taxation]}")
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
        print(f"     현재가치 {_won(item.present_value)}"
              f" · 연환산 {_won(item.value.annual_equivalent)}"
              f" · 노력 {item.effort_tier.value}")
        print(f"     지평 {item.value.horizon.years}년 ({item.value.horizon.reason})"
              f" · 지속확률 {item.value.persistence:.0%}")
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
    for signal in evaluate_thresholds(profile, year):
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
                    f"{c.value:,}({c.source.value})" for c in conflict.candidates
                )
                print(f"  · {label_of(conflict.field_name)}: {values}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
