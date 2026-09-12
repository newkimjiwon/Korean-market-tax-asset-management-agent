"""전부 독립 합성 데이터. 실제 사용자 프로필은 테스트에 넣지 않는다."""

import json
from copy import deepcopy

import pytest

from ktax.server import (
    calculate_salary_settlement,
    describe_settlement_fields,
    estimate_tax_liability,
    list_settlement_years,
)
from ktax.settlement import (
    calculate_settlement,
    compare_settlements,
    recommend_settlement_actions,
)


def synthetic():
    return {
        "resident": True,
        "other_income_present": False,
        "amount_basis": "actual",
        "gross_salary": 30_000_000,
        "non_taxable_salary": 0,
        "eligible_dependents": 0,
        "employee_pension": 0,
        "health_insurance": 0,
        "long_term_care": 0,
        "employment_insurance": 0,
        "employment_periods": [{"start": "2026-01-01", "end": "2026-12-31"}],
        "other_deductions": [],
        "other_credits": [],
        "pension_savings": 0,
        "irp": 0,
        "sme": {"status": "not_applicable"},
        "rent": {"payments": []},
    }


def housing():
    p = synthetic()
    p["gross_salary"] = 72_000_000
    p["rent"] = {
        "status": "applied",
        "homeless_household": True,
        "household_role": "head",
        "head_claims_housing": False,
        "leases": [
            {
                "id": "synthetic-a",
                "start": "2026-01-01",
                "end": "2027-12-31",
                "registered_from": "2026-01-01",
                "registered_until": "2027-12-31",
                "contract_eligible": True,
                "home_eligible": True,
            }
        ],
        "payments": [
            {
                "lease_id": "synthetic-a",
                "month": f"2026-{m:02}",
                "paid_on": f"2026-{m:02}-25",
                "amount": 720_000,
            }
            for m in range(1, 13)
        ],
        "support": [
            {
                "lease_id": "synthetic-a",
                "month": f"2026-{m:02}",
                "received_on": f"2026-{m:02}-28",
                "amount": 90_000,
            }
            for m in range(4, 13)
        ],
    }
    return p


def add_sme(p, start="2024-06-18", end="2029-06-30"):
    p["sme"] = {
        "status": "applied",
        "eligibility_confirmed": True,
        "start": start,
        "end": end,
        "rate": 90,
        "eligible_salary": p["gross_salary"] - p["non_taxable_salary"],
    }
    return p


def test_hand_calculated_standard_route():
    r = calculate_settlement(synthetic(), 2026)
    t = r["tax"]
    assert t["earned_income_deduction"] == 9_750_000
    assert t["taxable_base"] == 18_750_000
    assert t["gross_income_tax"] == 1_552_500
    assert t["income_tax"] == 682_500
    assert t["local_income_tax"] == 68_250
    assert t["total"] == 750_750
    assert r["settlement"]["refund"] is None
    assert r["status"] == "provisional"  # Full annual verification is not fabricated.


def test_missing_not_zero_and_no_age_required():
    r = calculate_settlement({"gross_salary": 30_000_000}, 2026)
    assert r["tax"] is None
    assert r["status"] == "indeterminate"
    assert "non_taxable_salary" in r["missing_fields"]
    assert not any("age" in k for k in r["missing_fields"])
    assert all(x["question"] and x["sources"] for x in r["next_questions"])


@pytest.mark.parametrize("value", [None, -1, 1.5, True, "30000000"])
def test_invalid_or_unknown_money(value):
    p = synthetic()
    p["gross_salary"] = value
    r = calculate_settlement(p, 2026)
    assert r["tax"] is None
    assert r["status"] == ("indeterminate" if value is None else "invalid_input")


def test_non_taxable_salary_and_double_count_guard():
    p = synthetic()
    p["gross_salary"] += 2_400_000
    p["non_taxable_salary"] = 2_400_000
    assert (
        calculate_settlement(p, 2026)["tax"]
        == calculate_settlement(synthetic(), 2026)["tax"]
    )
    p["income_deductions"] = 100
    assert calculate_settlement(p, 2026)["status"] == "invalid_input"


def test_unsupported_year_and_scope():
    assert calculate_settlement({}, 2098)["status"] == "unsupported_year"
    for k, v in [("resident", False), ("other_income_present", True)]:
        p = synthetic()
        p[k] = v
        assert calculate_settlement(p, 2026)["status"] == "unsupported_scope"


def test_rent_support_uses_target_month_and_separates_years():
    p = housing()
    p["rent"]["support"].append(
        {
            "lease_id": "synthetic-a",
            "month": "2027-01",
            "received_on": "2027-02-12",
            "amount": 90_000,
        }
    )
    r = calculate_settlement(p, 2026)
    assert r["rent"]["paid"] == 8_640_000
    assert r["rent"]["support"] == 810_000
    assert r["rent"]["eligible"] == 7_830_000
    assert r["rent"]["credit"] == 1_174_500
    # A late receipt is still allocated to the supported rent period, not receipt year.
    p["rent"]["support"][0]["received_on"] = "2027-01-10"
    assert calculate_settlement(p, 2026)["rent"] == r["rent"]


def test_mid_month_move_requires_exact_eligible_amount():
    p = housing()
    p["rent"]["leases"][0]["registered_from"] = "2026-02-12"
    r = calculate_settlement(p, 2026)
    assert r["status"] == "indeterminate"
    assert "rent.payments[1].eligible_amount" in r["missing_fields"]
    p["rent"]["payments"][1]["eligible_amount"] = 360_000
    r = calculate_settlement(p, 2026)
    assert r["rent"]["eligible"] == 6_750_000
    assert r["rent"]["excluded"][0]["month"] == "2026-01"


def test_partial_employment_not_silently_prorated():
    p = housing()
    p["employment_periods"][0]["start"] = "2026-03-15"
    assert (
        "rent.payments[2].eligible_amount"
        in calculate_settlement(p, 2026)["missing_fields"]
    )


def test_contract_eligibility_and_member_rules():
    p = housing()
    p["rent"]["household_role"] = "member"
    assert calculate_settlement(p, 2026)["rent"]["credit"] > 0
    p["rent"]["head_claims_housing"] = True
    assert calculate_settlement(p, 2026)["rent"]["credit"] == 0
    p = housing()
    p["rent"]["leases"][0]["contract_eligible"] = False
    assert calculate_settlement(p, 2026)["rent"]["credit"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "support_excess",
        "duplicate_payment",
        "unknown_lease",
        "reversed_dates",
        "cross_year_payment",
    ],
)
def test_inconsistent_rent_rejected(mutation):
    p = housing()
    if mutation == "support_excess":
        p["rent"]["support"][0]["amount"] = 900_000
    if mutation == "duplicate_payment":
        p["rent"]["payments"].append(deepcopy(p["rent"]["payments"][0]))
    if mutation == "unknown_lease":
        p["rent"]["payments"][0]["lease_id"] = "missing"
    if mutation == "reversed_dates":
        p["rent"]["leases"][0]["end"] = "2025-01-01"
    if mutation == "cross_year_payment":
        p["rent"]["payments"][0]["paid_on"] = "2027-01-01"
    assert calculate_settlement(p, 2026)["status"] == "invalid_input"


def test_sme_adjusts_earned_credit_and_caps_reduction():
    p = add_sme(synthetic())
    r = calculate_settlement(p, 2026)
    # Itemized: gross 1,552,500; relief 1,397,250; credit 740,000 * .1.
    assert r["tax"]["sme_reduction"] == 1_397_250
    assert (
        next(x for x in r["tax"]["credits"] if x["key"] == "earned_income")["available"]
        == 74_000
    )
    p = add_sme(housing())
    r = calculate_settlement(p, 2026)
    assert r["tax"]["sme_reduction"] == 2_000_000
    assert (
        next(x for x in r["tax"]["credits"] if x["key"] == "earned_income")["available"]
        < 660_000
    )


def test_sme_expiry_and_partial_salary():
    p = add_sme(synthetic(), end="2025-12-31")
    assert calculate_settlement(p, 2026)["status"] == "invalid_input"
    p = add_sme(synthetic(), end="2026-02-28")
    p["sme"]["eligible_salary"] = 5_000_000
    r = calculate_settlement(p, 2026)
    assert r["tax"]["sme_reduction"] == 232_875
    p["sme"]["eligibility_confirmed"] = False
    assert calculate_settlement(p, 2026)["status"] == "indeterminate"


def test_already_claimed_not_recommended_and_zero_is_not_ineligible():
    p = add_sme(synthetic())
    r = recommend_settlement_actions(p, 2026)
    assert r["baseline"]["tax"]["total"] == 0
    assert r["already_applied"] == ["sme"]
    assert not any(a["key"] == "claim_sme" for a in r["actions"])
    assert (
        next(a for a in r["actions"] if a["key"] == "additional_irp")[
            "additional_saving"
        ]
        == 0
    )


def test_combined_savings_never_exceed_baseline():
    p = housing()
    q = add_sme(deepcopy(p))
    q["irp"] = 9_000_000
    r = compare_settlements(p, q, 2026)
    assert 0 <= r["additional_saving"] <= r["before"]["tax"]["total"]
    assert (
        r["additional_saving"]
        == r["before"]["tax"]["total"] - r["after"]["tax"]["total"]
    )


def test_standard_and_special_are_not_added_together():
    p = synthetic()
    p["health_insurance"] = 1_000_000
    r = calculate_settlement(p, 2026)
    assert r["tax"]["method"] == "itemized"
    assert (
        next(c for c in r["tax"]["credits"] if c["key"] == "standard")["available"] == 0
    )
    p["health_insurance"] = 10_000
    assert calculate_settlement(p, 2026)["tax"]["method"] == "standard"


def test_refund_and_additional_payment_are_separate_from_credit():
    p = synthetic()
    p.update(withheld_income_tax=800_000, withheld_local_tax=80_000)
    s = calculate_settlement(p, 2026)["settlement"]
    assert s["refund"] == 129_250 and s["additional_payment"] == 0
    p.update(withheld_income_tax=0, withheld_local_tax=0)
    s = calculate_settlement(p, 2026)["settlement"]
    assert s["refund"] == 0 and s["additional_payment"] == 750_750


def test_pension_limit_and_no_negative_tax():
    p = synthetic()
    p["pension_savings"] = 100_000_000
    p["irp"] = 100_000_000
    r = calculate_settlement(p, 2026)
    assert (
        next(x for x in r["tax"]["credits"] if x["key"] == "pension")["available"]
        == 1_350_000
    )
    assert r["tax"]["total"] == 0


def test_evidence_preserved_without_mutating_input():
    p = synthetic()
    p["evidence"] = [
        {"field": "gross_salary", "source": "payroll", "as_of": "2026-12-31"}
    ]
    original = deepcopy(p)
    r = calculate_settlement(p, 2026)
    assert r["evidence"] == p["evidence"] and p == original


def test_tool_surface_and_legacy_guard():
    assert "gross_salary" in describe_settlement_fields()["fields"]
    assert 2026 in [r["year"] for r in list_settlement_years()["years"]]
    assert calculate_salary_settlement(synthetic(), 2026)["tax"]["total"] == 750_750
    assert (
        estimate_tax_liability({"age": 40, "earned_income": 72_000_000}, 2025)["status"]
        == "indeterminate"
    )


def test_cli_synthetic_roundtrip(tmp_path, capsys):
    from ktax.__main__ import main

    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps({"year": 2026, "settlement": synthetic()}))
    assert main([str(path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["baseline"]["tax"]["total"] == 750_750
    assert main([str(path)]) == 0
    assert "근로자 세액 계산" in capsys.readouterr().out


def test_unclaimed_rent_action_uses_actual_difference():
    p = housing()
    p["rent"]["status"] = "eligible_unclaimed"
    result = recommend_settlement_actions(p, 2026)
    action = next(a for a in result["actions"] if a["key"] == "claim_rent")
    assert 0 < action["additional_saving"] <= result["baseline"]["tax"]["total"]


def test_collector_merges_nested_fields_preserves_sources_and_blocks_conflict():
    from ktax.settlement_sources import collect_salary_inputs

    base = {
        "year": 2026,
        "amount_basis": "forecast",
        "as_of": "2026-04-03",
        "source": "user",
    }
    records = [
        {**base, "values": {"gross_salary": 72_000_000, "sme": {"status": "applied"}}},
        {
            **base,
            "source": "payroll",
            "values": {"gross_salary": 73_000_000, "sme": {"rate": 70}},
        },
    ]
    r = collect_salary_inputs(records)
    assert r["data"]["gross_salary"] == 73_000_000
    assert r["data"]["sme"] == {"status": "applied", "rate": 70}
    assert r["data"]["evidence"] and r["corrections"]
    records.append(
        {**base, "source": "payroll", "values": {"gross_salary": 74_000_000}}
    )
    r = collect_salary_inputs(records)
    assert r["status"] == "conflict" and r["data"] is None
    assert r["conflicts"][0]["field"] == "gross_salary"


def test_collector_does_not_mix_years_or_actual_forecast():
    from ktax.settlement_sources import collect_salary_inputs

    a = {
        "year": 2026,
        "amount_basis": "forecast",
        "source": "user",
        "as_of": "2026-04-03",
        "values": {},
    }
    assert collect_salary_inputs([a, {**a, "year": 2025}])["status"] == "invalid_input"
    assert (
        collect_salary_inputs([a, {**a, "amount_basis": "actual"}])["status"]
        == "invalid_input"
    )


def test_collector_rejects_conflicting_shapes_instead_of_crashing():
    from ktax.settlement_sources import collect_salary_inputs

    base = {
        "year": 2026,
        "amount_basis": "actual",
        "source": "user",
        "as_of": "2026-12-31",
    }
    r = collect_salary_inputs(
        [
            {**base, "values": {"sme": "applied"}},
            {**base, "values": {"sme": {"status": "applied"}}},
        ]
    )
    assert r["status"] == "invalid_input" and r["data"] is None


def test_mcp_registered_tools_execute_through_protocol_surface():
    import asyncio

    from ktax.server import server

    async def run():
        names = {t.name for t in await server.list_tools()}
        assert {
            "calculate_salary_settlement",
            "collect_salary_inputs",
            "compare_salary_settlements",
        } <= names
        result = await server.call_tool(
            "calculate_salary_settlement", {"data": synthetic(), "year": 2026}
        )
        assert not result.is_error
        payload = json.loads(result.content[0].text)
        assert payload["tax"]["total"] == 750_750

    asyncio.run(run())


def test_withholding_gaps_are_reported_without_blocking_tax():
    r = calculate_settlement(synthetic(), 2026)
    assert r["tax"] is not None
    assert set(r["missing_fields"]) == {"withheld_income_tax", "withheld_local_tax"}


def test_unreasonably_large_amount_is_validation_error():
    p = synthetic()
    p["gross_salary"] = 10**100
    assert calculate_settlement(p, 2026)["status"] == "invalid_input"


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("sme", "rate", {"bad": True}),
        ("sme", "eligible_salary", -1),
        ("rent", "status", "invalid"),
        ("rent", "leases", "not-an-array"),
    ],
)
def test_inactive_sections_still_validate_supplied_values(section, field, value):
    p = synthetic()
    p[section][field] = value
    assert calculate_settlement(p, 2026)["status"] == "invalid_input"


def test_missing_selector_does_not_request_all_dependent_fields():
    p = synthetic()
    p["sme"] = {}
    assert calculate_settlement(p, 2026)["missing_fields"] == ["sme.status"]
    p = synthetic()
    p["rent"] = {}
    assert calculate_settlement(p, 2026)["missing_fields"] == ["rent.payments"]


def test_head_does_not_need_an_answer_about_another_heads_claims():
    p = housing()
    del p["rent"]["head_claims_housing"]
    assert calculate_settlement(p, 2026)["tax"] is not None
    p["rent"]["household_role"] = "member"
    assert "rent.head_claims_housing" in calculate_settlement(p, 2026)["missing_fields"]


def test_duplicate_support_is_rejected_instead_of_deducted_twice():
    p = housing()
    p["rent"]["support"].append(deepcopy(p["rent"]["support"][0]))
    result = calculate_settlement(p, 2026)
    assert result["status"] == "invalid_input"
    assert result["tax"] is None


def test_partial_month_support_needs_verified_allocation():
    p = housing()
    p["rent"]["leases"][0]["registered_from"] = "2026-04-12"
    p["rent"]["payments"][3]["eligible_amount"] = 360_000
    r = calculate_settlement(p, 2026)
    assert r["tax"] is None
    assert r["missing_fields"] == ["rent.payments[3].eligible_support_amount"]
    p["rent"]["payments"][3]["eligible_support_amount"] = 45_000
    r = calculate_settlement(p, 2026)
    assert r["rent"]["eligible"] == 5_355_000


@pytest.mark.parametrize(
    "eligible,support_allocation", [(360_000, 100_000), (10_000, 20_000), (700_000, 0)]
)
def test_support_allocation_must_fit_both_parts_of_the_month(
    eligible, support_allocation
):
    p = housing()
    pay = p["rent"]["payments"][3]
    pay.update(eligible_amount=eligible, eligible_support_amount=support_allocation)
    assert calculate_settlement(p, 2026)["status"] == "invalid_input"


@pytest.mark.parametrize(
    "status", ["unsupported_year", "unsupported_scope", "invalid_input"]
)
def test_comparison_preserves_reason_calculation_is_unavailable(status):
    a = synthetic()
    b = synthetic()
    year = 2026
    if status == "unsupported_year":
        year = 2098
    if status == "unsupported_scope":
        b["other_income_present"] = True
    if status == "invalid_input":
        b["gross_salary"] = -1
    r = compare_settlements(a, b, year)
    assert r["status"] == status and r["additional_saving"] is None


def test_null_source_does_not_silently_preserve_old_known_amount():
    from ktax.settlement_sources import collect_salary_inputs

    record = {
        "year": 2026,
        "amount_basis": "actual",
        "as_of": "2026-12-31",
        "source": "user",
    }
    r = collect_salary_inputs(
        [
            {**record, "values": {"gross_salary": 30_000_000}},
            {**record, "source": "payroll", "values": {"gross_salary": None}},
        ]
    )
    assert r["data"]["gross_salary"] is None
    r = collect_salary_inputs(
        [
            {**record, "values": {"gross_salary": 30_000_000}},
            {**record, "values": {"gross_salary": None}},
        ]
    )
    assert r["status"] == "conflict" and r["data"] is None


def test_null_group_from_authoritative_source_invalidates_old_children():
    from ktax.settlement_sources import collect_salary_inputs

    record = {
        "year": 2026,
        "amount_basis": "actual",
        "as_of": "2026-12-31",
        "source": "user",
    }
    r = collect_salary_inputs(
        [
            {
                **record,
                "values": {"sme": {"status": "applied", "eligible_salary": 30_000_000}},
            },
            {**record, "source": "payroll", "values": {"sme": None}},
        ]
    )
    assert r["data"]["sme"]["status"] is None
    assert r["data"]["sme"]["eligible_salary"] is None


def test_estimated_source_cannot_be_complete_even_with_verified_rules(
    monkeypatch, tmp_path
):
    import ktax.settlement as module

    rules = json.loads((module.RULES_DIR / "2026.json").read_text())
    rules["verified"] = True
    (tmp_path / "2026.json").write_text(json.dumps(rules))
    monkeypatch.setattr(module, "RULES_DIR", tmp_path)
    p = synthetic()
    assert calculate_settlement(p, 2026)["status"] == "complete"
    p["evidence"] = [
        {"field": "gross_salary", "source": "estimated", "as_of": "2026-12-31"}
    ]
    assert calculate_settlement(p, 2026)["status"] == "provisional"


@pytest.mark.parametrize("gross,reduction,expected", [(18, 16, 1), (33, 22, 6)])
def test_reduction_ratio_does_not_lose_a_won_to_repeating_decimal(
    gross, reduction, expected
):
    from ktax.settlement import RULES_DIR, _earned_credit

    rules = json.loads((RULES_DIR / "2026.json").read_text())
    # 9 * 2/18 = 1, and 18 * 11/33 = 6 exactly.
    assert _earned_credit(2_000_000, gross, reduction, rules) == expected
