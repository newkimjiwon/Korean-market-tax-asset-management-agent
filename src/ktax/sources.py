"""프로필 자동 수집 계층.

Profile 필드는 30개가 넘는다. 사용자가 손으로 채울 규모가 아니므로
외부 원천에서 끌어와야 하고, 그러면 세 가지가 새로 필요해진다.

1. **모름과 0의 구분.** 자동 수집에서 값이 없다는 것은 "쓰지 않았다"가
   아니라 대개 "아직 못 가져왔다"는 뜻이다. 이 둘을 합치면 에이전트가
   "공제 대상이 아닙니다"라고 단정하게 되는데, 사실은 자료가 없을 뿐이다.

2. **필드별 출처와 기준일.** 숫자의 신뢰도는 출처에 달렸다. 에이전트가
   "간소화자료 기준입니다"와 "3월에 직접 알려주신 값입니다"를 구분해
   말할 수 있어야 한다. 스냅샷 비교에서도 값이 변한 것과 더 나은 출처를
   확보한 것은 다른 사건이다.

3. **충돌의 보존.** 두 원천이 다른 값을 주면 조용히 하나를 고르지 않는다.
   간소화자료와 마이데이터의 카드 사용액이 어긋나는 것은 사용자가 알아야
   할 사실이지 우리가 대신 판단할 문제가 아니다.

이 모듈은 수집 규약만 정의한다. 실제 커넥터(홈택스, 마이데이터)는 인증과
기관 허가가 필요해 도구 계층 밖이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum
from typing import Any

from ktax.models import Profile


class FieldSource(str, Enum):
    """값의 출처. 순서가 아니라 권위를 나타낸다 (RANK 참조)."""

    HOMETAX_SIMPLIFIED = "hometax_simplified"   # 연말정산 간소화 자료
    WITHHOLDING_RECEIPT = "withholding_receipt" # 원천징수영수증
    MYDATA = "mydata"                           # 금융 마이데이터
    USER = "user"                               # 사용자가 직접 알려준 값
    ESTIMATED = "estimated"                     # 다른 값에서 추정


# 같은 필드를 여러 원천이 줄 때의 우선순위. 숫자가 클수록 우선한다.
#
# 간소화자료를 사용자 입력보다 위에 두는 이유는, 국세청이 실제 신고에서
# 쓰는 자료가 그것이기 때문이다. 사용자의 기억과 어긋나면 기억이 아니라
# 자료가 기준이 된다 — 다만 어긋났다는 사실 자체는 충돌로 보고된다.
RANK: dict[FieldSource, int] = {
    FieldSource.HOMETAX_SIMPLIFIED: 40,
    FieldSource.WITHHOLDING_RECEIPT: 40,
    FieldSource.MYDATA: 30,
    FieldSource.USER: 20,
    FieldSource.ESTIMATED: 10,
}

# 외부 원천이 알 수 없고 사용자만 답할 수 있는 것들.
USER_ONLY_FIELDS = frozenset({
    "age",
    "filing_type",
    "is_homeless_household_head",
    "is_homeless_household_head_spouse",
    "isa_preferential",
    "dependent_children",
    "sme_employment_start_year",
    "sme_special_category",
    "planned_withdrawal_age",
})


@dataclass(frozen=True)
class FieldValue:
    value: Any
    source: FieldSource
    as_of: date
    note: str = ""

    @property
    def rank(self) -> int:
        return RANK[self.source]


@dataclass(frozen=True)
class Conflict:
    """같은 권위의 두 원천이 다른 값을 준 경우."""

    field_name: str
    candidates: list[FieldValue]

    def to_dict(self) -> dict:
        return {
            "field": self.field_name,
            "candidates": [
                {
                    "value": c.value,
                    "source": c.source.value,
                    "as_of": c.as_of.isoformat(),
                }
                for c in self.candidates
            ],
        }


@dataclass(frozen=True)
class ProfileData:
    """수집된 필드 값들. Profile 로 바꾸기 전 단계.

    Profile 과 달리 '값이 없음'을 표현할 수 있다. 그 차이가 이 계층의
    존재 이유다.
    """

    fields: dict[str, FieldValue] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    def known(self) -> frozenset[str]:
        return frozenset(self.fields)

    def missing(self) -> frozenset[str]:
        return frozenset(_profile_field_names()) - self.known()

    def source_of(self, name: str) -> FieldSource | None:
        entry = self.fields.get(name)
        return entry.source if entry else None

    def to_profile(self) -> Profile:
        """알려진 값으로 Profile 을 만든다.

        모르는 필드는 Profile 기본값이 되므로, 계산 결과를 해석할 때는
        반드시 `missing()` 을 함께 봐야 한다. 그래서 이 메서드만 쓰지 말고
        `build_profile()` 을 쓰는 편이 안전하다.
        """
        known = {k: v.value for k, v in self.fields.items()}
        if "age" not in known:
            raise ValueError("age 는 필수입니다. 나이 없이는 지평을 계산할 수 없습니다.")
        return Profile(**known)


def _profile_field_names() -> frozenset[str]:
    from dataclasses import fields as dc_fields

    return frozenset(f.name for f in dc_fields(Profile))


def merge(*records: dict[str, FieldValue]) -> ProfileData:
    """여러 원천의 값을 권위 순으로 합친다.

    같은 권위에서 값이 갈리면 하나를 고르지 않고 충돌로 남긴다. 최신 값을
    잠정 채택하되, 충돌 사실은 호출자에게 그대로 전달된다.
    """
    gathered: dict[str, list[FieldValue]] = {}
    valid = _profile_field_names()

    for record in records:
        for name, value in record.items():
            if name not in valid:
                raise KeyError(f"Profile 에 없는 필드: {name}")
            gathered.setdefault(name, []).append(value)

    chosen: dict[str, FieldValue] = {}
    conflicts: list[Conflict] = []

    for name, candidates in gathered.items():
        top_rank = max(c.rank for c in candidates)
        top = [c for c in candidates if c.rank == top_rank]
        distinct = {c.value for c in top}

        if len(distinct) > 1:
            conflicts.append(Conflict(field_name=name, candidates=sorted(
                top, key=lambda c: c.as_of, reverse=True
            )))
        chosen[name] = max(top, key=lambda c: c.as_of)

    return ProfileData(fields=chosen, conflicts=conflicts)


def from_source(
    source: FieldSource, as_of: date, values: dict[str, Any], note: str = ""
) -> dict[str, FieldValue]:
    """원천 하나가 제공한 값들을 FieldValue 로 감싼다.

    키를 넣지 않으면 '모름', None 을 넣으면 '없는 것으로 확인됨'이다.
    이 둘은 다르다 — 사용자가 "중소기업에 다닌 적 없다"고 답한 것과
    아직 물어보지 않은 것을 같이 취급하면 계속 되묻게 된다.
    """
    return {
        name: FieldValue(value=v, source=source, as_of=as_of, note=note)
        for name, v in values.items()
    }


# --------------------------------------------------------------------------
# 홈택스 연말정산 간소화 자료 매핑
# --------------------------------------------------------------------------
#
# 간소화 자료의 항목 구분을 Profile 필드로 옮기는 표. 커넥터가 붙기
# 전에도 매핑은 확정해 둘 수 있고, 여러 항목이 한 필드로 합쳐지는
# 지점(직불카드 + 현금영수증)을 명시해 두는 편이 안전하다.
HOMETAX_CATEGORY_MAP: dict[str, str] = {
    "신용카드": "credit_card_spending",
    "직불카드등": "debit_cash_spending",
    "현금영수증": "debit_cash_spending",        # 공제율이 같아 한 필드로 합산
    "도서공연등": "culture_spending",
    "전통시장": "traditional_market_spending",
    "대중교통": "public_transit_spending",
    "기부금": "donations",
    "주택마련저축": "housing_subscription_contributed",
    "연금저축": "pension_savings_contributed",
    "퇴직연금": "irp_contributed",
    "월세액": "annual_rent_paid",
}

# 합산해서 한 필드에 들어가는 항목들. 덮어쓰면 한쪽이 사라진다.
_ACCUMULATING = frozenset({"debit_cash_spending"})


def from_hometax(
    as_of: date, categories: dict[str, int]
) -> dict[str, FieldValue]:
    """간소화 자료 항목별 금액을 Profile 필드 값으로 변환한다.

    직불카드와 현금영수증처럼 여러 항목이 한 필드로 가는 경우는 합산한다.
    덮어쓰면 한쪽 금액이 조용히 사라진다.
    """
    totals: dict[str, int] = {}
    unmapped: list[str] = []

    for category, amount in categories.items():
        name = HOMETAX_CATEGORY_MAP.get(category)
        if name is None:
            unmapped.append(category)
            continue
        if name in _ACCUMULATING:
            totals[name] = totals.get(name, 0) + amount
        else:
            totals[name] = amount

    if unmapped:
        raise KeyError(
            f"매핑되지 않은 간소화 자료 항목: {', '.join(sorted(unmapped))}. "
            f"HOMETAX_CATEGORY_MAP 에 추가하거나 호출 전에 걸러내세요."
        )

    return from_source(
        FieldSource.HOMETAX_SIMPLIFIED, as_of, totals, note="연말정산 간소화 자료"
    )


# --------------------------------------------------------------------------
# 필드를 채우려면 무엇을 물어야 하는가
# --------------------------------------------------------------------------
#
# 에이전트가 빠진 값을 스스로 메울 수 있게, 필드마다 어디서 오는지와
# 사용자에게 물을 문장을 함께 둔다.
FIELD_PROMPTS: dict[str, str] = {
    "age": "나이가 어떻게 되세요?",
    "earned_income": "총급여가 얼마인가요? (원천징수영수증 기준)",
    "business_income": "사업소득금액이 있으신가요?",
    "financial_income": "작년 이자·배당 소득 합계가 얼마인가요?",
    "other_income": "기타소득이 있으신가요?",
    "income_deductions": "소득공제 총액이 얼마인가요?",
    "tax_credits": "이미 적용된 세액공제가 있나요?",
    "pension_savings_contributed": "올해 연금저축에 얼마 넣으셨나요?",
    "irp_contributed": "올해 IRP에 얼마 넣으셨나요?",
    "isa_contributed_this_year": "올해 ISA 납입액이 얼마인가요?",
    "isa_contributed_total": "ISA 총 납입액이 얼마인가요?",
    "isa_preferential": "ISA가 서민형·농어민형인가요?",
    "is_homeless_household_head": "무주택 세대주이신가요?",
    "is_homeless_household_head_spouse": "과세연도 중 무주택인 세대의 세대주 배우자에 해당하시나요(12월 31일 기준)?",
    "culture_credit_card_spending": "문화비 중 신용카드 결제액은 얼마인가요? 일반 신용카드 사용액과 중복 입력하지 마세요.",
    "culture_debit_cash_spending": "문화비 중 직불카드·현금영수증 결제액은 얼마인가요? 일반 사용액과 중복 입력하지 마세요.",
    "housing_subscription_contributed": "올해 주택청약종합저축 납입액이 얼마인가요?",
    "annual_rent_paid": "계산할 귀속연도에 낸 월세 총액이 얼마인가요?",
    "medical_expenses": "부양가족(본인·65세 이상·장애인 제외) 의료비가 얼마인가요?",
    "medical_expenses_unlimited": "본인·6세 이하·65세 이상·장애인 의료비가 얼마인가요?",
    "medical_expenses_fertility": "난임시술비 지출이 있으신가요?",
    "medical_expenses_premature": "미숙아·선천성이상아 의료비 지출이 있으신가요?",
    "donations": "작년 기부금이 얼마인가요?",
    "credit_card_spending": "작년 신용카드 사용액이 얼마인가요?",
    "debit_cash_spending": "작년 체크카드·현금영수증 사용액이 얼마인가요?",
    "traditional_market_spending": "전통시장 사용액이 얼마인가요?",
    "public_transit_spending": "대중교통 이용액이 얼마인가요?",
    "culture_spending": "도서·공연·체육시설 사용액이 얼마인가요?",
    "dependent_children": "부양 자녀가 몇 명인가요?",
    "sme_employment_start_year": "중소기업에 취업하신 적이 있나요? 있다면 몇 년도인가요?",
    "sme_special_category": "60세 이상·장애인·경력단절여성에 해당하시나요?",
    "planned_withdrawal_age": "연금을 몇 살부터 받으실 계획인가요?",
    "filing_type": "근로소득자이신가요, 종합소득 신고 대상이신가요?",
}

# 각 필드를 자동으로 채울 수 있는 원천. 에이전트가 묻기 전에 먼저
# 가져올 곳이 있는지 판단하는 데 쓴다.
FIELD_SOURCES: dict[str, tuple[FieldSource, ...]] = {
    name: (FieldSource.USER,) for name in USER_ONLY_FIELDS
}
FIELD_SOURCES.update({
    name: (FieldSource.HOMETAX_SIMPLIFIED, FieldSource.USER)
    for name in set(HOMETAX_CATEGORY_MAP.values())
})
FIELD_SOURCES.update({
    "earned_income": (FieldSource.WITHHOLDING_RECEIPT, FieldSource.USER),
    "income_deductions": (FieldSource.WITHHOLDING_RECEIPT, FieldSource.USER),
    "tax_credits": (FieldSource.WITHHOLDING_RECEIPT, FieldSource.USER),
    "financial_income": (FieldSource.MYDATA, FieldSource.USER),
    "isa_contributed_this_year": (FieldSource.MYDATA, FieldSource.USER),
    "isa_contributed_total": (FieldSource.MYDATA, FieldSource.USER),
    "medical_expenses_unlimited": (FieldSource.HOMETAX_SIMPLIFIED, FieldSource.USER),
    "medical_expenses_fertility": (FieldSource.HOMETAX_SIMPLIFIED, FieldSource.USER),
    "medical_expenses_premature": (FieldSource.HOMETAX_SIMPLIFIED, FieldSource.USER),
})


def how_to_fill(name: str) -> dict[str, Any]:
    """빠진 필드를 채우는 방법. 에이전트가 그대로 소비한다."""
    sources = FIELD_SOURCES.get(name, (FieldSource.USER,))
    return {
        "field": name,
        "question": FIELD_PROMPTS.get(name, f"{name} 값을 알려주세요."),
        "sources": [s.value for s in sources],
        "user_only": name in USER_ONLY_FIELDS,
    }


# 사람이 읽을 필드 이름. 알림 문구와 변화 보고에 쓴다.
FIELD_LABELS: dict[str, str] = {
    "age": "나이",
    "filing_type": "신고 유형",
    "earned_income": "근로소득",
    "business_income": "사업소득",
    "financial_income": "금융소득",
    "other_income": "기타소득",
    "income_deductions": "소득공제 총액",
    "tax_credits": "세액공제 총액",
    "pension_savings_contributed": "연금저축 납입액",
    "irp_contributed": "IRP 납입액",
    "isa_contributed_this_year": "올해 ISA 납입액",
    "isa_contributed_total": "ISA 총 납입액",
    "isa_preferential": "ISA 서민형 여부",
    "is_homeless_household_head": "무주택 세대주 여부",
    "is_homeless_household_head_spouse": "무주택 세대주의 배우자 여부",
    "culture_credit_card_spending": "문화비 신용카드 결제액",
    "culture_debit_cash_spending": "문화비 직불·현금 결제액",
    "housing_subscription_contributed": "주택청약 납입액",
    "annual_rent_paid": "연간 월세액",
    "medical_expenses": "의료비(그 밖의 부양가족)",
    "medical_expenses_unlimited": "의료비(본인·65세 이상·장애인 등)",
    "medical_expenses_fertility": "난임시술비",
    "medical_expenses_premature": "미숙아·선천성이상아 의료비",
    "donations": "기부금",
    "credit_card_spending": "신용카드 사용액",
    "debit_cash_spending": "체크카드·현금영수증 사용액",
    "traditional_market_spending": "전통시장 사용액",
    "public_transit_spending": "대중교통 이용액",
    "culture_spending": "도서·공연·체육 사용액",
    "dependent_children": "부양 자녀 수",
    "sme_employment_start_year": "중소기업 취업 연도",
    "sme_special_category": "중소기업 감면 특례 대상",
    "planned_withdrawal_age": "연금 수령 예정 나이",
}


def label_of(name: str) -> str:
    return FIELD_LABELS.get(name, name)
