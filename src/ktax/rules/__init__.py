"""세법은 코드가 아니라 데이터다.

세율과 한도는 매년 개정되고, 과거 귀속연도를 다시 계산해야 할 일도 생긴다.
로직에 상수를 박으면 해마다 코드를 뜯어야 하고 과거 재계산은 불가능해진다.
그래서 귀속연도별 JSON을 단일 진실 공급원으로 두고, 계산 모듈은 읽기만 한다.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).parent / "data"


class RulesetNotFound(LookupError):
    pass


@lru_cache(maxsize=None)
def load_ruleset(year: int) -> dict[str, Any]:
    """귀속연도 세법 데이터를 읽는다."""
    path = _DATA_DIR / f"{year}.json"
    if not path.exists():
        available = sorted(p.stem for p in _DATA_DIR.glob("*.json"))
        raise RulesetNotFound(
            f"{year}년 세법 데이터가 없습니다. 사용 가능: {', '.join(available) or '없음'}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def available_years() -> list[int]:
    return sorted(int(p.stem) for p in _DATA_DIR.glob("*.json"))
