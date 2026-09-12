"""근로자 정산 입력 계약. 실제 개인정보나 원본 문서를 저장하지 않는다."""

from __future__ import annotations

from copy import deepcopy


def field(kind, question, source="user", **kw):
    return {"type": kind, "question": question, "source": source, **kw}


MONEY = lambda q: field("money", q, "payroll")
DATE = lambda q: field("date", q)
BOOL = lambda q: field("boolean", q)

SCHEMA = {
    "resident": BOOL("한국 세법상 거주자인가요?"),
    "other_income_present": BOOL("이 급여 외에 이자·배당·사업 등 다른 소득이 있나요?"),
    "amount_basis": field(
        "enum",
        "해당 귀속연도 전체 실적인가요, 연말까지의 예상액인가요?",
        values=["actual", "forecast"],
    ),
    "gross_salary": MONEY(
        "해당 귀속연도 전체 급여·상여 합계는 얼마인가요? 비과세 포함 금액입니다."
    ),
    "non_taxable_salary": MONEY(
        "그중 식대 등을 포함한 비과세 급여 합계는 얼마인가요? 없으면 0원입니다."
    ),
    "eligible_dependents": field(
        "money", "기본공제 요건을 확인한 부양가족은 본인 제외 몇 명인가요?"
    ),
    "employee_pension": MONEY("해당 귀속연도 공적연금 본인 부담액은 얼마인가요?"),
    "health_insurance": MONEY("건강보험 본인 부담액은 얼마인가요?"),
    "long_term_care": MONEY("장기요양보험 본인 부담액은 얼마인가요?"),
    "employment_insurance": MONEY("고용보험 본인 부담액은 얼마인가요?"),
    "employment_periods": field(
        "array",
        "해당 연도 근로기간을 모두 알려주세요.",
        items={
            "start": DATE("근로 시작일은 언제인가요?"),
            "end": DATE("근로 종료일 또는 예상 종료일은 언제인가요?"),
        },
    ),
    "other_deductions": field(
        "array",
        "추가 인적·주택·카드 등 다른 소득공제 자료가 있나요? 없으면 빈 목록입니다.",
        "hometax",
        items={
            "amount": MONEY(
                "위 자동 계산 항목을 제외한, 요건과 개별 한도를 확인한 소득공제액은 얼마인가요?"
            ),
            "special": BOOL("특별소득공제에 해당하나요?"),
            "limited": BOOL("소득공제 종합한도 대상인가요?"),
        },
    ),
    "other_credits": field(
        "array",
        "다른 세액공제 자료가 있나요? 없으면 빈 목록입니다.",
        "hometax",
        items={
            "amount": MONEY(
                "근로소득·연금계좌·월세를 제외한 검증된 공제액은 얼마인가요?"
            ),
            "special": BOOL("표준세액공제와 중복 불가한 특별세액공제인가요?"),
        },
    ),
    "pension_savings": MONEY("해당 연도 연금저축 본인 납입액은 얼마인가요?"),
    "irp": MONEY(
        "해당 연도 IRP·퇴직연금 본인 추가 납입액은 얼마인가요? 회사 부담금은 제외합니다."
    ),
    "sme": field(
        "object",
        "중소기업 취업자 감면 적용 상태는 무엇인가요?",
        properties={
            "status": field(
                "enum",
                "이미 적용 중인가요, 자격 확인 후 미신청인가요, 비대상인가요?",
                values=["applied", "eligible_unclaimed", "not_applicable"],
            ),
            "eligibility_confirmed": BOOL(
                "회사·취업 당시 연령·병역·이전 감면 이력까지 감면 요건이 확인됐나요?"
            ),
            "start": DATE("최초 감면 시작일은 언제인가요? 이직일로 갱신하지 않습니다."),
            "end": DATE("회사 감면내역에 기재된 감면 종료일은 언제인가요?"),
            "rate": field("enum", "확인된 감면율은 얼마인가요?", values=[70, 90]),
            "eligible_salary": MONEY(
                "올해 총급여 중 감면기간에 해당하는 감면 대상 급여는 얼마인가요? 회사 내역 기준입니다."
            ),
        },
    ),
    "rent": field(
        "object",
        "해당 귀속연도 월세 납부 내역이 있나요?",
        properties={
            "status": field(
                "enum",
                "이번 정산에 월세 공제를 반영할 예정인가요, 아직 미신청 상태인가요?",
                values=["applied", "eligible_unclaimed"],
            ),
            "payments": field(
                "array",
                "월세 납부 건별 금액·대상 월·납부일은 무엇인가요? 없으면 빈 목록입니다.",
                items={
                    "lease_id": field(
                        "string", "계약 구분용 익명 식별자는 무엇인가요?"
                    ),
                    "month": field(
                        "month", "월세 대상 월은 언제인가요? YYYY-MM 형식입니다."
                    ),
                    "paid_on": DATE("실제 납부일은 언제인가요?"),
                    "amount": MONEY("관리비를 제외한 납부 월세는 얼마인가요?"),
                    "eligible_support_amount": field(
                        "money",
                        "부분기간 적격 월세에 배분되는 공적 지원액은 얼마인가요? 지원내역에서 확인합니다.",
                        "support_notice",
                        optional=True,
                    ),
                    "eligible_amount": field(
                        "money",
                        "월중 전입·취업 등인 경우 요건 충족 기간의 월세액은 얼마인가요?",
                        optional=True,
                    ),
                },
            ),
            "support": field(
                "array",
                "계약별·지원 대상 월별 합산한 공적 월세지원 내역은 무엇인가요? 없으면 빈 목록입니다.",
                items={
                    "lease_id": field("string", "지원 대상 계약 식별자는 무엇인가요?"),
                    "month": field(
                        "month", "지원 대상 월은 언제인가요? 입금 월과 구별합니다."
                    ),
                    "amount": MONEY("이 대상 월의 공적 지원액은 얼마인가요?"),
                    "received_on": DATE("지원금 수령일 또는 예상 수령일은 언제인가요?"),
                },
            ),
            "homeless_household": BOOL("연말 기준 세대가 무주택인가요?"),
            "household_role": field(
                "enum",
                "세대주인가요, 세대원인가요? 별도 주소 배우자 특례는 별도 검토 대상입니다.",
                values=["head", "member", "spouse_exception"],
            ),
            "head_claims_housing": BOOL(
                "세대원이면 세대주가 중복 제한되는 주택 공제를 신청하나요? 세대주 본인이면 false입니다."
            ),
            "leases": field(
                "array",
                "계약·전입 기간과 주택 요건을 알려주세요.",
                items={
                    "id": field("string", "계약 익명 식별자는 무엇인가요?"),
                    "start": DATE("계약 시작일은 언제인가요?"),
                    "end": DATE("계약 종료일은 언제인가요?"),
                    "registered_from": DATE("이 계약 주소로 전입한 날짜는 언제인가요?"),
                    "registered_until": DATE(
                        "주소 일치 종료일 또는 예상 종료일은 언제인가요?"
                    ),
                    "contract_eligible": BOOL(
                        "본인 또는 기본공제대상자 명의 계약 등 계약 요건을 확인했나요?"
                    ),
                    "home_eligible": BOOL(
                        "주거용 여부와 면적 또는 기준시가 요건을 확인했나요?"
                    ),
                },
            ),
        },
    ),
    "withheld_income_tax": field(
        "money",
        "해당 귀속연도 기납부 소득세는 얼마인가요? 이미 환급·추징된 금액을 반영한 순액입니다.",
        "payroll",
        optional=True,
    ),
    "withheld_local_tax": field(
        "money", "기납부 지방소득세 순액은 얼마인가요?", "payroll", optional=True
    ),
    "evidence": field(
        "array",
        "각 값의 출처와 기준일을 기록할 수 있습니다. 원본 개인정보는 넣지 마세요.",
        optional=True,
        items={
            "field": field("string", "어떤 입력 경로의 출처인가요?"),
            "source": field(
                "enum",
                "출처는 무엇인가요?",
                values=[
                    "user",
                    "payroll",
                    "withholding_receipt",
                    "hometax",
                    "support_notice",
                    "contract",
                    "estimated",
                ],
            ),
            "as_of": DATE("자료 기준일은 언제인가요?"),
        },
    ),
}


def describe_settlement_fields():
    return {
        "fields": deepcopy(SCHEMA),
        "unknown": "누락 또는 null은 미확인. 0/false/[]는 확인된 없음.",
        "scope": "일반 거주자 근로소득 정산. 혼합소득·특례는 unsupported_scope로 반환.",
        "privacy": "입력은 메모리에서 계산하며 파일·로그에 저장하지 않습니다. 이름·주소·회사명·주민번호 불필요.",
    }
