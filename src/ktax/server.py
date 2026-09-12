"""MCP 도구 표면.

여기는 얇아야 한다. 계산은 전부 아래 모듈에 있고, 이 파일은 그것을
에이전트가 붙여 쓸 수 있는 모양으로 노출하기만 한다.

도구는 상태를 저장하지 않는다. 프로필과 스냅샷은 매 호출마다 인자로
들어오고, 보관과 스케줄링(월 1회 배치)은 호스트가 맡는다.

실행:
    uv run --extra mcp python -m ktax.server
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer

from ktax.models import (
    Action,
    BenefitStream,
    EffortTier,
    FilingType,
    Profile,
    Rationale,
    Recurrence,
)
from ktax.catalog import catalog_keys, discover_actions as _discover
from ktax.sources import (
    FieldSource,
    label_of,
    from_hometax,
    from_source,
    how_to_fill,
    merge as _merge,
)
from ktax.monitor import diff_snapshots, evaluate_thresholds
from ktax.rules import available_years, load_ruleset
from ktax.scoring import (
    score_actions,
    set_and_forget,
    sort_by_quick_wins,
    sort_by_value,
    urgent,
)
from ktax.tax import estimate_tax, simulate_isa_contribution, simulate_pension_contribution

_INSTRUCTIONS = """한국 세무·자산관리 계산 도구.

쓰는 순서
  1. describe_profile_fields 로 어떤 값이 필요한지 본다.
  2. 값을 모은다. 자동 수집 자료가 있으면 collect_profile 로 합치고,
     없으면 사용자에게 물어 직접 프로필 dict 를 만든다.
  3. recommend_actions 로 할 수 있는 일을 찾는다.
  4. check_thresholds 로 경계에 다가선 것이 있는지 본다.
  5. 다음 달에 다시 볼 때는 compare_snapshots 로 무엇이 달라졌는지 확인한다.

반드시 지킬 것
  · 프로필에 넣지 않은 필드는 '모름'으로 처리된다. 확인해서 0원이면
    0 을 명시적으로 넣어라. 비워두면 '자격 미달'이 아니라 '판단 불가'가 된다.
  · indeterminate 항목을 '공제 대상이 아닙니다'라고 전하지 마라. 자격이
    없는 것이 아니라 우리가 아직 모르는 것이다. how_to_fill 이 무엇을
    물어야 하는지 알려준다.
  · 모든 계산 결과의 rationale 을 근거로 삼아 설명하라. applied_rules 는
    적용한 규칙, assumptions 는 가정, warnings 는 신뢰도 경고다.
  · 절세 효과 계산만 한다. 어떤 상품에 투자하라는 판단은 이 도구의 범위가
    아니다.
"""

server = MCPServer("ktax", instructions=_INSTRUCTIONS)


def _profile(data: dict[str, Any]) -> Profile:
    """dict 를 Profile 로. 실패하면 에이전트가 회복할 수 있게 알려준다.

    Profile(**data) 를 그대로 부르면 TypeError 가 파이썬 내부를 노출할 뿐
    무엇이 유효한 이름인지 알려주지 않는다. 에이전트는 그 메시지로는
    다음 수를 두지 못한다.
    """
    from dataclasses import fields as dc_fields
    from difflib import get_close_matches

    valid = {f.name for f in dc_fields(Profile)}
    data = dict(data)

    unknown = sorted(set(data) - valid)
    if unknown:
        hints = []
        for name in unknown:
            close = get_close_matches(name, valid, n=2, cutoff=0.6)
            hints.append(f"{name}" + (f" (혹시 {' 또는 '.join(close)}?)" if close else ""))
        raise ValueError(
            f"프로필에 없는 필드: {', '.join(hints)}. "
            f"describe_profile_fields 로 전체 필드 이름과 뜻을 확인하세요."
        )

    if "age" not in data:
        raise ValueError(
            "age 는 필수입니다. 나이를 모르면 액션의 지평(몇 년간 효과가 "
            "이어지는지)을 계산할 수 없습니다. 사용자에게 나이를 물어보세요."
        )

    if "filing_type" in data:
        try:
            data["filing_type"] = FilingType(data["filing_type"])
        except ValueError:
            raise ValueError(
                f"filing_type 은 'earned'(근로소득, 연말정산) 또는 "
                f"'comprehensive'(종합소득, 5월 신고) 중 하나입니다."
            ) from None

    return Profile(**data)


def _resolve_known(
    profile: dict[str, Any],
    known_fields: list[str] | None,
    assume_complete: bool,
) -> frozenset[str] | None:
    """무엇을 안다고 볼 것인가.

    기본값은 '전달된 키가 곧 아는 것'이다. 예전에는 아무것도 주지 않으면
    모든 필드를 안다고 보았는데, 그러면 채우지 않은 필드가 0으로 읽혀
    '무주택 세대주가 아니라 공제 대상이 아닙니다' 같은 단정이 나갔다.
    물어본 적도 없는 사실을 사용자에게 전달하게 되는 셈이다.

    안전한 쪽이 기본이어야 한다.
    """
    if assume_complete:
        return None
    if known_fields is not None:
        return frozenset(known_fields)
    return frozenset(profile)


def _rationale_dict(r: Rationale) -> dict[str, Any]:
    return {
        "summary": r.summary,
        "applied_rules": r.applied_rules,
        "assumptions": r.assumptions,
        "warnings": r.warnings(),
    }


# --------------------------------------------------------------------------
# 세법 데이터
# --------------------------------------------------------------------------

@server.tool()
def get_ruleset(year: int) -> dict[str, Any]:
    """해당 귀속연도의 세율·한도 데이터를 그대로 돌려준다.

    에이전트가 근거를 직접 인용해야 할 때 쓴다.
    """
    return load_ruleset(year)


@server.tool()
def list_ruleset_years() -> list[int]:
    """계산 가능한 귀속연도 목록."""
    return available_years()


# --------------------------------------------------------------------------
# 계산
# --------------------------------------------------------------------------

@server.tool()
def estimate_tax_liability(profile: dict[str, Any], year: int) -> dict[str, Any]:
    """총 세부담을 계산한다. 모든 액션 비교의 기준점.

    산출세액이 아니라 실제 부담 전체를 돌려준다. 금융소득이 종합과세
    기준금액 이하면 그 세금은 원천징수로 끝나 산출세액에 잡히지 않으므로,
    산출세액만 보고하면 경계 양쪽 숫자를 비교할 수 없다.

    `financial_income_taxation` 은 적용된 방식이다:
      - "separate":    기준금액 이하, 원천징수로 종결
      - "general":     소득세법 제62조 일반산출세액이 적용됨
      - "comparative": 비교산출세액이 적용됨 (종합과세가 더 가벼워 역전 방지)
    """
    est = estimate_tax(_profile(profile), year)
    return {
        "taxable_base": est.taxable_base,
        "comprehensive_tax": est.comprehensive_tax,
        "separate_financial_tax": est.separate_financial_tax,
        "income_tax": est.income_tax,
        "local_income_tax": est.local_income_tax,
        "total": est.total,
        "financial_income_taxation": est.financial_income_taxation,
        "rationale": _rationale_dict(est.rationale),
    }


@server.tool()
def simulate_pension_account(
    profile: dict[str, Any], year: int, additional_contribution: int
) -> dict[str, Any]:
    """연금저축/IRP 추가 납입 시 절감액과 효과의 시간 구조를 계산한다."""
    sim = simulate_pension_contribution(_profile(profile), year, additional_contribution)
    return {
        "annual_saving": sim.annual_saving,
        "baseline_total": sim.baseline_total,
        "simulated_total": sim.simulated_total,
        "benefit": asdict(sim.benefit),
        "rationale": _rationale_dict(sim.rationale),
    }


@server.tool()
def simulate_isa(
    profile: dict[str, Any],
    year: int,
    additional_contribution: int,
    expected_return_rate: float,
) -> dict[str, Any]:
    """ISA 납입 시 일반계좌 대비 금융소득 과세 절감액을 계산한다.

    expected_return_rate 는 사용자가 제시하는 가정값이다. 이 도구는
    수익률을 예측하지 않는다.
    """
    sim = simulate_isa_contribution(
        _profile(profile), year, additional_contribution, expected_return_rate
    )
    return {
        "annual_saving": sim.annual_saving,
        "baseline_total": sim.baseline_total,
        "simulated_total": sim.simulated_total,
        "benefit": asdict(sim.benefit),
        "rationale": _rationale_dict(sim.rationale),
    }


# --------------------------------------------------------------------------
# 프로필 수집
# --------------------------------------------------------------------------

@server.tool()
def describe_profile_fields(only_missing_from: dict[str, Any] | None = None) -> dict[str, Any]:
    """프로필 필드의 전체 목록과 각각의 뜻·출처·질문 문장.

    다른 도구를 부르기 전에 이것부터 보면 필드 이름을 추측하지 않아도 된다.
    `only_missing_from` 에 지금까지 모은 프로필을 주면 아직 없는 것만 추린다.

    각 항목의 `user_only` 가 true 면 외부 자료로는 알 수 없어 반드시
    사용자에게 물어야 한다. false 면 `sources` 에 적힌 곳에서 가져올 수 있다.
    """
    from dataclasses import fields as dc_fields

    names = [f.name for f in dc_fields(Profile)]
    if only_missing_from is not None:
        names = [n for n in names if n not in only_missing_from]

    return {
        "required": ["age"],
        "fields": [
            {**how_to_fill(name), "label": label_of(name)} for name in names
        ],
        "note": (
            "프로필에 넣지 않은 필드는 '모름'으로 처리된다. 확인해서 0원이면 "
            "0 을 명시적으로 넣어라."
        ),
    }



@server.tool()
def collect_profile(records: list[dict[str, Any]]) -> dict[str, Any]:
    """여러 원천의 값을 합쳐 프로필을 만든다.

    각 record 는 다음 형태다:
      {"source": "hometax_simplified"|"withholding_receipt"|"mydata"|"user"|"estimated",
       "as_of": "2026-01-20",
       "values": {"earned_income": 52000000, ...}}

    간소화 자료는 `values` 대신 `categories` 에 항목별 금액을 그대로 넣어도
    된다 ({"신용카드": 22000000, "현금영수증": 1000000, ...}). 직불카드와
    현금영수증처럼 한 필드로 합쳐지는 항목은 자동으로 합산된다.

    **키를 넣지 않으면 '모름', null 을 넣으면 '없는 것으로 확인됨'이다.**
    이 둘은 다르다. 사용자가 "중소기업에 다닌 적 없다"고 답한 것을 모름으로
    두면 같은 질문을 계속 하게 된다.

    돌려주는 `missing` 은 아직 확보하지 못한 필드이고, `conflicts` 는 같은
    권위의 두 원천이 다른 값을 준 경우다. 충돌은 임의로 덮지 않는다 —
    간소화자료와 마이데이터의 금액이 어긋나는 것은 사용자가 알아야 할 사실이다.
    """
    parsed = []
    for record in records:
        source = FieldSource(record["source"])
        as_of = date.fromisoformat(record["as_of"])
        if "categories" in record:
            parsed.append(from_hometax(as_of, record["categories"]))
        else:
            parsed.append(from_source(source, as_of, record.get("values", {})))

    data = _merge(*parsed)
    return {
        "profile": {k: v.value for k, v in data.fields.items()},
        "sources": {k: v.source.value for k, v in data.fields.items()},
        "missing": sorted(data.missing()),
        "conflicts": [c.to_dict() for c in data.conflicts],
    }


@server.tool()
def explain_missing_field(name: str) -> dict[str, Any]:
    """빠진 값을 어떻게 채우는지 알려준다.

    어느 원천에서 가져올 수 있는지와 사용자에게 물을 문장을 함께 돌려준다.
    `user_only` 가 true 면 외부 자료로는 알 수 없어 반드시 물어야 한다.
    """
    return how_to_fill(name)


# --------------------------------------------------------------------------
# 카탈로그
# --------------------------------------------------------------------------

@server.tool()
def recommend_actions(
    profile: dict[str, Any],
    year: int,
    view: str = "value",
    known_fields: list[str] | None = None,
    assume_complete: bool = False,
) -> dict[str, Any]:
    """이 사람이 지금 할 수 있는 절세 액션을 찾아 순위를 매긴다.

    카탈로그 전체를 프로필에 대해 평가하고 가치·노력·사분면·마감을 붙여
    돌려준다. 대부분의 경우 이 도구 하나면 충분하다.

    view:
      - "value":          현재가치 순. 시간이 충분할 때
      - "quick_wins":     노력이 가벼운 순. 지금 5분뿐일 때
      - "set_and_forget": 1회 세팅 → 매년 자동인 것만
      - "urgent":         마감 임박 오버레이

    `known_fields` 에 실제로 값을 확보한 필드 이름을 주면(collect_profile 의
    결과에서 얻는다) 값이 없는 액션을 `indeterminate` 로 분류한다. 주지 않으면
    모든 값을 안다고 보고 계산하므로, 자동 수집을 쓴다면 반드시 넘겨야 한다 —
    값이 없는 것을 '자격 미달'로 단정하면 사용자에게 틀린 말을 하게 된다.

    `ineligible` 에는 자격 미달 항목이 사유와 함께 담긴다. 사용자가
    "왜 나는 이게 안 뜨죠?"라고 물으면 여기서 답을 찾을 수 있다.
    `indeterminate` 는 자격 미달이 아니라 판단할 자료가 없는 경우이며,
    각 항목에 무엇을 어디서 채워야 하는지가 함께 담긴다.
    """
    p = _profile(profile)
    discovery = _discover(
        p, year, known=_resolve_known(profile, known_fields, assume_complete)
    )
    scored = score_actions(discovery.applicable, p)

    views = {
        "value": sort_by_value,
        "quick_wins": sort_by_quick_wins,
        "set_and_forget": set_and_forget,
        "urgent": urgent,
    }
    if view not in views:
        raise ValueError(f"알 수 없는 view: {view}. 가능: {', '.join(views)}")

    return {
        "actions": [s.to_dict() for s in views[view](scored)],
        "ineligible": [i.to_dict() for i in discovery.ineligible],
        "indeterminate": [i.to_dict() for i in discovery.indeterminate],
        "missing_fields": discovery.missing_fields(),
        "view": view,
        "year": year,
    }


@server.tool()
def list_catalog() -> list[str]:
    """카탈로그에 등록된 액션 종류. 어떤 절세 항목을 다루는지 확인할 때 쓴다."""
    return catalog_keys()


# --------------------------------------------------------------------------
# 랭킹
# --------------------------------------------------------------------------

@server.tool()
def rank_actions(
    actions: list[dict[str, Any]],
    profile: dict[str, Any],
    view: str = "value",
) -> list[dict[str, Any]]:
    """액션 후보에 가치·노력·사분면·마감을 붙여 정렬한다.

    절감액을 노력으로 나눈 단일 ROI 점수는 만들지 않는다. 두 축을
    따로 유지해야 사용자 상황에 맞는 목록을 고를 수 있다.

    view:
      - "value":          현재가치 순. 시간이 충분할 때
      - "quick_wins":     노력이 가벼운 순. 지금 5분뿐일 때
      - "set_and_forget": 1회 세팅 → 매년 자동인 것만. 사실상 지배적인 사분면
      - "urgent":         마감 임박 오버레이. 가치 순위와 독립

    actions 의 각 항목:
      key, title, amount_per_year, benefit_recurrence("one_time"|"recurring"),
      effort_tier("instant"|"short"|"project"),
      effort_recurrence("one_time"|"recurring"),
      선택: starts_in_years, explicit_years, ends_at_age, deadline(ISO), summary
    """
    parsed = [
        Action(
            key=a["key"],
            title=a.get("title", a["key"]),
            benefit=BenefitStream(
                amount_per_year=a["amount_per_year"],
                recurrence=Recurrence(a["benefit_recurrence"]),
                starts_in_years=a.get("starts_in_years", 0),
                explicit_years=a.get("explicit_years"),
                ends_at_age=a.get("ends_at_age"),
            ),
            effort_tier=EffortTier(a["effort_tier"]),
            effort_recurrence=Recurrence(a["effort_recurrence"]),
            rationale=Rationale(summary=a.get("summary", "")),
            deadline=a.get("deadline"),
        )
        for a in actions
    ]

    scored = score_actions(parsed, _profile(profile))
    views = {
        "value": sort_by_value,
        "quick_wins": sort_by_quick_wins,
        "set_and_forget": set_and_forget,
        "urgent": urgent,
    }
    if view not in views:
        raise ValueError(f"알 수 없는 view: {view}. 가능: {', '.join(views)}")
    return [s.to_dict() for s in views[view](scored)]


# --------------------------------------------------------------------------
# 감시
# --------------------------------------------------------------------------

@server.tool()
def check_thresholds(profile: dict[str, Any], year: int) -> dict[str, Any]:
    """경계선까지 남은 여유를 계산한다.

    금융소득 종합과세 2천만원, 연금계좌 한도, 세율 구간 경계, ISA 한도.
    사람이 상시 추적하지 못하는 것들이다. 알림을 쏠지 말지는 호출자가 정한다 —
    severity 가 info 면 조용히 있는 것이 맞다.

    `assumed_zero` 에는 프로필에 없어서 0으로 간주한 필드가 담긴다. 그 값이
    실제로 0인지 아직 모르는지 구분되지 않으므로, 여기 이름이 올라온 항목의
    신호는 사용자에게 단정적으로 전하면 안 된다.
    """
    p = _profile(profile)
    inputs = {
        "financial_income", "pension_savings_contributed", "irp_contributed",
        "isa_contributed_this_year", "earned_income", "income_deductions",
    }
    return {
        "signals": [s.to_dict() for s in evaluate_thresholds(p, year)],
        "assumed_zero": sorted(inputs - set(profile)),
    }


@server.tool()
def compare_snapshots(
    previous: dict[str, Any], current: dict[str, Any]
) -> list[dict[str, Any]]:
    """직전 스냅샷 대비 유의미한 변화만 추출한다.

    세금 상황은 날짜가 아니라 인생이 바뀔 때 변한다. 결과가 비어 있으면
    재계산을 촉발할 이유가 없다는 뜻이다.
    """
    return [c.to_dict() for c in diff_snapshots(_profile(previous), _profile(current))]


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
