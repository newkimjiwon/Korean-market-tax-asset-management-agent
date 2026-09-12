"""정산용 원자료 병합. 동일 권위 충돌은 계산 입력을 반환하지 않는다."""

from copy import deepcopy
from datetime import date

from ktax.settlement import Inputs
from ktax.settlement_schema import SCHEMA

AUTHORITY = {
    "estimated": 10,
    "user": 20,
    "payroll": 40,
    "withholding_receipt": 40,
    "hometax": 40,
    "support_notice": 40,
    "contract": 40,
}


def collect_salary_inputs(records: list[dict]) -> dict:
    """values는 settlement 입력의 부분 객체. 목록은 항목 누적 대신 전체 대체.

    원자료에 대한 인증/파싱은 호스트가 수행한다. 파일이나 로그에 저장하지 않는다.
    서로 다른 귀속연도/실적·예상 자료는 먼저 호출자가 구분해야 한다.
    """
    candidates = {}
    errors = []
    years = set()
    bases = set()

    def flatten(values, schema=SCHEMA, prefix=""):
        for key, value in values.items():
            if not isinstance(key, str) or "." in key:
                raise ValueError("values의 키는 점 없는 문자열이어야 합니다.")
            path = prefix + key
            spec = schema[key]
            if spec["type"] == "object":
                # A null object revokes knowledge of its children, rather than
                # silently retaining older known values or creating path collisions.
                children = (
                    value
                    if value is not None
                    else {k: None for k in spec["properties"]}
                )
                yield from flatten(children, spec["properties"], path + ".")
            else:
                yield path, value

    if not isinstance(records, list):
        return {
            "status": "invalid_input",
            "data": None,
            "errors": ["records는 목록이어야 합니다."],
        }
    for index, record in enumerate(records):
        try:
            if not isinstance(record, dict) or set(record) - {
                "year",
                "amount_basis",
                "source",
                "as_of",
                "values",
            }:
                raise ValueError(
                    "record는 year, amount_basis, source, as_of, values 객체입니다."
                )
            if (
                type(record.get("year")) is not int
                or not 1900 <= record["year"] <= 9999
            ):
                raise ValueError("자료 귀속연도가 필요합니다.")
            if record.get("amount_basis") not in ("actual", "forecast"):
                raise ValueError("actual/forecast 구분이 필요합니다.")
            if record.get("source") not in AUTHORITY or not isinstance(
                record.get("values"), dict
            ):
                raise ValueError("지원하는 출처와 values 객체가 필요합니다.")
            validation = Inputs()
            validation.validate(record["values"], SCHEMA)
            if validation.errors:
                errors.extend({"record": index, **error} for error in validation.errors)
                continue
            stamp = date.fromisoformat(record["as_of"])
            if stamp.isoformat() != record["as_of"]:
                raise ValueError("as_of는 YYYY-MM-DD입니다.")
            years.add(record["year"])
            bases.add(record["amount_basis"])
            for path, value in flatten(record["values"]):
                if path in ("evidence", "amount_basis"):
                    raise ValueError("evidence와 amount_basis는 병합기가 생성합니다.")
                candidates.setdefault(path, []).append(
                    {
                        "value": deepcopy(value),
                        "source": record["source"],
                        "as_of": record["as_of"],
                    }
                )
        except (ValueError, TypeError, KeyError):
            errors.append(
                {
                    "record": index,
                    "message": "입력 계약을 확인하세요: year, amount_basis, source, as_of(YYYY-MM-DD), values.",
                }
            )
    if len(years) > 1 or len(bases) > 1:
        errors.append(
            {"message": "귀속연도 또는 실적/예상 기준이 다른 자료는 섞을 수 없습니다."}
        )
    if errors:
        return {"status": "invalid_input", "data": None, "errors": errors}
    data = {}
    evidence = []
    conflicts = []
    corrections = []
    for path, items in candidates.items():
        best_rank = max(AUTHORITY[item["source"]] for item in items)
        top = [item for item in items if AUTHORITY[item["source"]] == best_rank]
        chosen = max(top, key=lambda x: x["as_of"])
        if any(item["value"] != chosen["value"] for item in top):
            conflicts.append({"field": path, "candidates": top})
        if any(
            item["value"] != chosen["value"]
            for item in items
            if AUTHORITY[item["source"]] < best_rank
        ):
            corrections.append({"field": path, "selected_source": chosen["source"]})
        target = data
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = deepcopy(chosen["value"])
        evidence.append(
            {"field": path, "source": chosen["source"], "as_of": chosen["as_of"]}
        )
    if conflicts:
        return {
            "status": "conflict",
            "data": None,
            "conflicts": conflicts,
            "corrections": corrections,
        }
    data["evidence"] = evidence
    if bases:
        data["amount_basis"] = next(iter(bases))
    return {
        "status": "collected",
        "year": next(iter(years)) if years else None,
        "data": data,
        "conflicts": [],
        "corrections": corrections,
    }
