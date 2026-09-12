"""모델 가정.

세법 데이터(`rules/data/*.json`)와 의도적으로 분리한다. 세법은 확인하면
맞고 틀림이 정해지는 사실이지만, 여기 값들은 판단이다. 같은 파일에 두면
검증된 사실과 추정이 뒤섞여 어느 쪽이 어느 쪽인지 알 수 없게 된다.

지속확률(persistence)은 "이 효과가 내년에도 이어질 확률"이다.
1.0 은 확정된 기간을 뜻한다 — 불확실성이 없다는 의미이지, 영원하다는
뜻이 아니다. 기간 자체는 `BenefitStream` 의 종료 조건이 따로 정한다.
"""

from __future__ import annotations

# 값의 근거를 함께 적어둔다. 숫자만 남으면 나중에 아무도 손대지 못한다.
PERSISTENCE: dict[str, float] = {
    # 확정된 기간. 법으로 정해진 감면 기간이라 불확실성이 없다.
    "sme_employment_reduction": 1.00,

    # 본인이 계약한 법적 구조. 중도해지에 불이익이 있어 유지 유인이 크다.
    "pension_account": 0.97,

    # 의무보유 기간이 계약으로 정해져 있으나, 절감액은 금융소득이
    # 계속 발생하는지에 달려 있다.
    "isa_account": 0.95,

    # 본인 의지로 반복 가능하지만 소득·사정 변화에 쉽게 끊긴다.
    "donation_credit": 0.90,

    # 무주택 세대주 요건이 전제다. 자가 구매 시점에 바로 끊긴다.
    "housing_subscription": 0.90,

    # 자가 구매나 전세 전환으로 언제든 끊긴다. 연금저축과 같은
    # 확신도로 취급하면 순위가 왜곡된다.
    "monthly_rent_credit": 0.85,

    # 의료비 지출은 반복을 전제할 수 없다. 큰 금액일수록 일회성일 확률이
    # 높아 오히려 지속성이 낮다.
    "medical_expense_credit": 0.80,
}

DEFAULT_PERSISTENCE = 0.85


def persistence_for(action_key: str) -> float:
    return PERSISTENCE.get(action_key, DEFAULT_PERSISTENCE)


def persistence_note(action_key: str) -> str:
    """근거에 동봉할 설명. 사용자가 숫자의 출처를 물을 수 있어야 한다."""
    p = persistence_for(action_key)
    if p >= 1.0:
        return "기간이 확정되어 있어 지속성 할인을 적용하지 않았습니다"
    return (
        f"이 효과가 매년 이어질 확률을 {p:.0%}로 가정했습니다 "
        f"(모델 가정이며 세법이 정한 값이 아닙니다)"
    )
