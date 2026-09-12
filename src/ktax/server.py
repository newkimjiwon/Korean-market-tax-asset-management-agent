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

server = MCPServer("ktax")


def _profile(data: dict[str, Any]) -> Profile:
    data = dict(data)
    if "filing_type" in data:
        data["filing_type"] = FilingType(data["filing_type"])
    return Profile(**data)


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
    """베이스라인 세액을 계산한다. 모든 액션 비교의 기준점."""
    est = estimate_tax(_profile(profile), year)
    return {
        "taxable_base": est.taxable_base,
        "income_tax": est.income_tax,
        "local_income_tax": est.local_income_tax,
        "total": est.total,
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
# 카탈로그
# --------------------------------------------------------------------------

@server.tool()
def recommend_actions(
    profile: dict[str, Any], year: int, view: str = "value"
) -> dict[str, Any]:
    """이 사람이 지금 할 수 있는 절세 액션을 찾아 순위를 매긴다.

    카탈로그 전체를 프로필에 대해 평가하고 가치·노력·사분면·마감을 붙여
    돌려준다. 대부분의 경우 이 도구 하나면 충분하다.

    view:
      - "value":          현재가치 순. 시간이 충분할 때
      - "quick_wins":     노력이 가벼운 순. 지금 5분뿐일 때
      - "set_and_forget": 1회 세팅 → 매년 자동인 것만
      - "urgent":         마감 임박 오버레이

    `ineligible` 에는 자격 미달 항목이 사유와 함께 담긴다. 사용자가
    "왜 나는 이게 안 뜨죠?"라고 물으면 여기서 답을 찾을 수 있다.
    """
    p = _profile(profile)
    discovery = _discover(p, year)
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
def check_thresholds(profile: dict[str, Any], year: int) -> list[dict[str, Any]]:
    """경계선까지 남은 여유를 계산한다.

    금융소득 종합과세 2천만원, 연금계좌 한도, 세율 구간 경계, ISA 한도.
    사람이 상시 추적하지 못하는 것들이다. 알림을 쏠지 말지는 호출자가 정한다 —
    severity 가 info 면 조용히 있는 것이 맞다.
    """
    return [s.to_dict() for s in evaluate_thresholds(_profile(profile), year)]


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
