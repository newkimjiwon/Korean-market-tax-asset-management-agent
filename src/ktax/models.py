"""도구 레이어 전체가 주고받는 공통 타입.

설계 원칙
---------
1. 도구는 상태를 저장하지 않는다. 프로필/스냅샷은 항상 인자로 들어오고,
   보관은 호스트(에이전트 런타임)의 몫이다.
2. 금액은 원 단위 정수로만 다룬다. 부동소수 누적 오차를 세액에 남기지 않는다.
3. 모든 계산 결과에는 근거(`Rationale`)가 붙는다. 숫자만 뱉는 도구는
   에이전트를 앵무새로 만든다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

Won = int


class Recurrence(str, Enum):
    """효과나 노력이 한 번으로 끝나는지, 매년 반복되는지."""

    ONE_TIME = "one_time"
    RECURRING = "recurring"


class EffortTier(str, Enum):
    """노력의 크기. 소수점 ROI 같은 가짜 정밀도 대신 등급으로 둔다."""

    INSTANT = "instant"   # 10분 이내, 앱에서 몇 번 누르면 끝
    SHORT = "short"       # 1~2시간, 서류 한두 개 필요
    PROJECT = "project"   # 며칠~몇 주, 기관 방문이나 다자간 조율 필요


class Quadrant(str, Enum):
    """노력 반복성 × 효과 반복성의 2x2.

    SET_AND_FORGET 이 다른 사분면을 압도한다. 한 번 세팅하면 매년
    자동으로 돌아오기 때문에, 절감액이 다소 작아도 위로 띄워야 한다.
    """

    SET_AND_FORGET = "set_and_forget"   # 1회 노력 → 반복 효과
    ONE_OFF = "one_off"                 # 1회 노력 → 1회 효과
    MAINTENANCE = "maintenance"         # 반복 노력 → 반복 효과
    POOR = "poor"                       # 반복 노력 → 1회 효과


class FilingType(str, Enum):
    EARNED = "earned"                   # 근로소득 (연말정산)
    COMPREHENSIVE = "comprehensive"     # 종합소득 (5월 신고)


@dataclass(frozen=True)
class Rationale:
    """계산 결과에 동봉되는 근거. 에이전트가 사용자에게 설명할 재료."""

    summary: str
    applied_rules: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    ruleset_year: int | None = None
    ruleset_verified: bool = True

    def warnings(self) -> list[str]:
        if self.ruleset_verified:
            return []
        return [
            f"{self.ruleset_year}년 세법 데이터가 아직 검증되지 않았습니다. "
            f"실제 신고 전 국세청 기준으로 확인이 필요합니다."
        ]


@dataclass(frozen=True)
class Profile:
    """납세자 상태. 매년 다시 찍어서 재계산하는 것이 전제다."""

    age: int
    filing_type: FilingType = FilingType.EARNED

    # 소득
    earned_income: Won = 0             # 총급여
    business_income: Won = 0           # 사업소득금액
    financial_income: Won = 0          # 이자 + 배당 합계
    other_income: Won = 0

    # 공제
    income_deductions: Won = 0         # 소득공제 총액 (과세표준 차감)
    tax_credits: Won = 0               # 기존 세액공제 총액

    # 상품 현황
    pension_savings_contributed: Won = 0   # 올해 연금저축 납입액
    irp_contributed: Won = 0               # 올해 IRP 납입액
    isa_contributed_this_year: Won = 0
    isa_contributed_total: Won = 0
    isa_preferential: bool = False         # 서민형/농어민형 여부

    # 주거
    is_homeless_household_head: bool = False   # 무주택 세대주
    housing_subscription_contributed: Won = 0  # 올해 주택청약종합저축 납입액
    annual_rent_paid: Won = 0                  # 올해 지출한 월세 총액

    # 지출 기반 공제
    medical_expenses: Won = 0
    donations: Won = 0

    # 중소기업 취업자 감면
    sme_employment_start_year: int | None = None
    sme_special_category: bool = False         # 60세 이상/장애인/경력단절여성

    # 지평 계산용
    planned_withdrawal_age: int | None = None

    @property
    def comprehensive_income(self) -> Won:
        """금융소득을 제외한 종합소득금액.

        소득세법 제62조에서 '이자소득등을 제외한 다른 종합소득금액'에 해당한다.
        금융소득은 종합과세 기준금액 초과 여부에 따라 합산 방식이 달라지므로
        여기서 섞지 않는다.
        """
        return self.earned_income + self.business_income + self.other_income

    def non_financial_taxable_base(self) -> Won:
        """금융소득을 제외한 과세표준.

        실제 과세표준은 금융소득 합산 여부에 따라 달라지므로 세법을 아는
        `ktax.tax.taxable_base(profile, year)` 를 쓴다. 이 메서드는 그 계산의
        재료이며, 단독으로는 과세표준이 아니다.
        """
        return max(0, self.comprehensive_income - self.income_deductions)


@dataclass(frozen=True)
class BenefitStream:
    """액션이 만들어내는 효과의 시간 구조.

    금액만으로는 비교가 안 된다. 1회성 500만원과 매년 100만원은
    완전히 다른 물건이고, 그 차이는 여기서 표현된다.
    """

    amount_per_year: Won
    recurrence: Recurrence
    starts_in_years: int = 0
    # 스트림이 끝나는 조건. 둘 중 하나만 주면 된다.
    explicit_years: int | None = None
    ends_at_age: int | None = None
    # 효과가 매년 이어질 확률. 하드 컷("10년째까지 100%, 11년째부터 0%")은
    # 현실의 어떤 것도 그렇게 작동하지 않으므로, 불확실성을 절벽이 아니라
    # 매끄러운 감쇠로 표현한다. 1.0 = 확정된 기간(불확실성 없음).
    persistence: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 < self.persistence <= 1.0:
            raise ValueError(
                f"persistence 는 (0, 1] 범위여야 합니다: {self.persistence}"
            )


@dataclass(frozen=True)
class Action:
    """절세 액션 후보."""

    key: str
    title: str
    benefit: BenefitStream
    effort_tier: EffortTier
    effort_recurrence: Recurrence
    rationale: Rationale
    deadline: str | None = None          # ISO date. 가치 점수와 섞지 않는다.
