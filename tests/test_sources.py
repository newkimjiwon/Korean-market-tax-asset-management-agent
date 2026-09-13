"""프로필 자동 수집 계층."""

from datetime import date

import pytest

from ktax.catalog import REQUIRED_FIELDS, catalog_keys, discover_actions
from ktax.models import Profile
from ktax.sources import (
    FIELD_PROMPTS,
    HOMETAX_CATEGORY_MAP,
    USER_ONLY_FIELDS,
    FieldSource,
    FieldValue,
    ProfileData,
    from_hometax,
    from_source,
    how_to_fill,
    merge,
)

TODAY = date(2026, 1, 20)
YEAR = 2025


def hometax(**categories):
    return from_hometax(TODAY, categories)


def user(**values):
    return from_source(FieldSource.USER, TODAY, values)


# --------------------------------------------------------------------------
# 모름 vs 0 vs 없음
# --------------------------------------------------------------------------

def test_omitted_field_is_unknown():
    data = merge(user(age=33))
    assert "credit_card_spending" not in data.known()
    assert "credit_card_spending" in data.missing()


def test_explicit_none_is_known_absence():
    """사용자가 '해당 없음'이라고 답한 것과 아직 안 물어본 것은 다르다."""
    data = merge(user(age=33, sme_employment_start_year=None))
    assert "sme_employment_start_year" in data.known()
    assert data.fields["sme_employment_start_year"].value is None


def test_zero_is_a_real_value_not_a_gap():
    data = merge(user(age=33, credit_card_spending=0))
    assert "credit_card_spending" in data.known()
    assert "credit_card_spending" not in data.missing()


def test_age_is_required_to_build_a_profile():
    with pytest.raises(ValueError, match="age"):
        merge(user(earned_income=50_000_000)).to_profile()


# --------------------------------------------------------------------------
# 병합과 우선순위
# --------------------------------------------------------------------------

def test_higher_authority_wins():
    data = merge(
        user(age=33, earned_income=50_000_000),
        from_source(FieldSource.WITHHOLDING_RECEIPT, TODAY, {"earned_income": 52_000_000}),
    )
    assert data.fields["earned_income"].value == 52_000_000
    assert data.source_of("earned_income") is FieldSource.WITHHOLDING_RECEIPT


def test_estimate_never_beats_user_input():
    data = merge(
        user(age=33, financial_income=3_000_000),
        from_source(FieldSource.ESTIMATED, TODAY, {"financial_income": 9_000_000}),
    )
    assert data.fields["financial_income"].value == 3_000_000


def test_same_authority_disagreement_is_reported_not_hidden():
    """간소화자료와 원천징수영수증이 어긋나면 사용자가 알아야 한다."""
    data = merge(
        from_source(FieldSource.HOMETAX_SIMPLIFIED, TODAY, {"donations": 400_000}),
        from_source(FieldSource.WITHHOLDING_RECEIPT, TODAY, {"donations": 700_000}),
        user(age=33),
    )
    assert len(data.conflicts) == 1
    assert data.conflicts[0].field_name == "donations"
    assert {c.value for c in data.conflicts[0].candidates} == {400_000, 700_000}


def test_agreement_is_not_a_conflict():
    data = merge(
        from_source(FieldSource.HOMETAX_SIMPLIFIED, TODAY, {"donations": 400_000}),
        from_source(FieldSource.WITHHOLDING_RECEIPT, TODAY, {"donations": 400_000}),
        user(age=33),
    )
    assert data.conflicts == []


def test_conflict_still_yields_a_usable_value():
    """충돌을 보고하되 계산은 막지 않는다. 최신 값을 잠정 채택한다."""
    data = merge(
        from_source(FieldSource.HOMETAX_SIMPLIFIED, date(2026, 1, 1), {"donations": 400_000}),
        from_source(FieldSource.WITHHOLDING_RECEIPT, date(2026, 2, 1), {"donations": 700_000}),
        user(age=33),
    )
    assert data.fields["donations"].value == 700_000
    assert data.conflicts


def test_unknown_profile_field_is_rejected():
    with pytest.raises(KeyError, match="Profile 에 없는 필드"):
        merge(user(age=33, 마법의필드=1))


# --------------------------------------------------------------------------
# 홈택스 매핑
# --------------------------------------------------------------------------

def test_debit_and_cash_receipt_are_summed_not_overwritten():
    """공제율이 같아 한 필드로 가는 항목들. 덮어쓰면 한쪽이 사라진다."""
    values = hometax(직불카드등=3_000_000, 현금영수증=1_000_000)
    assert values["debit_cash_spending"].value == 4_000_000


def test_hometax_values_carry_their_source():
    values = hometax(신용카드=20_000_000)
    assert values["credit_card_spending"].source is FieldSource.HOMETAX_SIMPLIFIED
    assert values["credit_card_spending"].as_of == TODAY


def test_unmapped_category_is_loud():
    """조용히 버리면 공제 항목이 사라진 걸 아무도 모른다."""
    with pytest.raises(KeyError, match="매핑되지 않은"):
        hometax(신용카드=1_000_000, 알수없는항목=500_000)


def test_every_mapped_field_exists_on_profile():
    from dataclasses import fields

    names = {f.name for f in fields(Profile)}
    assert set(HOMETAX_CATEGORY_MAP.values()) <= names


# --------------------------------------------------------------------------
# 빠진 값 안내
# --------------------------------------------------------------------------

def test_every_profile_field_has_a_prompt():
    from dataclasses import fields

    assert {f.name for f in fields(Profile)} <= set(FIELD_PROMPTS)


def test_user_only_fields_are_marked():
    assert how_to_fill("is_homeless_household_head")["user_only"] is True
    assert how_to_fill("credit_card_spending")["user_only"] is False


def test_automatable_field_names_its_source():
    assert "hometax_simplified" in how_to_fill("donations")["sources"]


# --------------------------------------------------------------------------
# 카탈로그 연동 — 이 계층의 존재 이유
# --------------------------------------------------------------------------

def test_missing_data_is_not_reported_as_ineligible():
    """예전에는 값이 없으면 '공제 대상 아님'이라고 단정했다. 틀린 말이었다."""
    data = merge(user(age=33, earned_income=50_000_000))
    d = discover_actions(data.to_profile(), YEAR, known=data.known())

    keys = {i.key for i in d.indeterminate}
    assert "medical_expense_credit" in keys
    assert "medical_expense_credit" not in {i.key for i in d.ineligible}


def test_known_zero_is_correctly_ineligible():
    """진짜로 0인 것은 자격 미달이 맞다."""
    data = merge(user(
        age=33, earned_income=50_000_000,
        medical_expenses=0, medical_expenses_unlimited=0,
        medical_expenses_fertility=0, medical_expenses_premature=0,
    ))
    d = discover_actions(data.to_profile(), YEAR, known=data.known())

    assert "medical_expense_credit" in {i.key for i in d.ineligible}
    assert "medical_expense_credit" not in {i.key for i in d.indeterminate}


def test_available_value_still_blocked_by_a_user_only_field():
    """월세액을 간소화자료에서 받아도 무주택 세대주 여부는 물어야 한다."""
    data = merge(hometax(월세액=8_400_000), user(age=33, earned_income=50_000_000))
    d = discover_actions(data.to_profile(), YEAR, known=data.known())

    rent = next(i for i in d.indeterminate if i.key == "monthly_rent_credit")
    assert rent.missing == ["is_homeless_household_head"]


def test_indeterminate_tells_the_agent_what_to_ask():
    data = merge(user(age=33, earned_income=50_000_000))
    d = discover_actions(data.to_profile(), YEAR, known=data.known())
    item = next(i for i in d.indeterminate if i.key == "pension_account").to_dict()

    assert item["how_to_fill"]
    assert all(step["question"] for step in item["how_to_fill"])


def test_missing_fields_are_deduplicated():
    data = merge(user(age=33))
    d = discover_actions(data.to_profile(), YEAR, known=data.known())
    names = d.missing_fields()
    assert len(names) == len(set(names))


def test_full_data_yields_no_indeterminate():
    from dataclasses import fields

    values = {f.name: getattr(Profile(age=33), f.name) for f in fields(Profile)}
    data = merge(user(**values))
    assert discover_actions(data.to_profile(), YEAR, known=data.known()).indeterminate == []


def test_omitting_known_preserves_old_behaviour():
    p = Profile(age=33, earned_income=50_000_000)
    assert discover_actions(p, YEAR).indeterminate == []


def test_every_catalog_action_declares_its_requirements():
    assert set(catalog_keys()) == set(REQUIRED_FIELDS)


def test_required_fields_exist_on_profile():
    from dataclasses import fields

    names = {f.name for f in fields(Profile)}
    for key, required in REQUIRED_FIELDS.items():
        assert required <= names, f"{key} 가 없는 필드를 요구한다"
