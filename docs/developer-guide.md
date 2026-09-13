# 개발·에이전트 연동 안내

사용 방법과 전체 구조는 [README](../README.md), 급여 정산 입력 계약은
[근로자 통합 정산](salary-settlement.md)을 참고하세요.

## 두 계산 경로

통합 정산은 `settlement_schema.py`의 입력 계약을 바탕으로 `settlement.py`에서
급여·공제·감면을 함께 계산합니다. 규칙은 `rules/settlement/<연도>.json`입니다.
여러 자료의 병합은 `settlement_sources.py`가 담당합니다. 동일 권위 원천이
충돌하면 계산용 데이터를 확정하지 않고 `conflict`를 반환합니다.

기존 `Profile` 경로는 소득공제·세액공제 총액을 사전에 계산해 넣는 시뮬레이션입니다.
`tax.py`가 세금 변화를, `catalog.py`가 행동별 적용 여부를 계산하며,
`rules/data/<연도>.json`을 사용합니다. 통합 경로의 지원 연도를 기존 경로에
그대로 적용하지 않습니다. 항목별 독립 시나리오의 절세액도 합산하지 않습니다.

## MCP 도구 호출 흐름

급여 원자료로 계산할 때:

1. `list_settlement_years`로 지원 연도와 검증 여부를 확인합니다.
2. `describe_settlement_fields`로 조건별 질문과 출처를 확인합니다.
3. 자료가 여러 개면 `collect_salary_inputs`로 병합합니다.
4. `calculate_salary_settlement`에 알려진 값만 전달합니다.
5. `next_questions`에 따라 부족한 자료를 보완합니다.
6. `recommend_salary_actions`로 추가 절세 시나리오를 확인합니다.
7. 복수 변경은 `compare_salary_settlements`로 함께 비교합니다.

기존 시뮬레이션은 `describe_profile_fields` → `collect_profile` →
`recommend_actions` 순서로 사용합니다. `explain_missing_field`는 부족한 입력의
질문과 출처를 설명합니다. `check_thresholds`와 `compare_snapshots`는 임계값과
변화 확인용이며, 이 도구를 실행하는 주기와 기록 보관은 호스트에서 관리합니다.

## 입력·출처 처리

통합 정산에서 누락과 `null`은 미확인입니다. 확인한 없음만 `0`, `false`, 빈 목록으로
표현합니다. 레거시 수집 모델의 선택적 필드에서 쓰는 `null` 의미와 혼용하지 않습니다.
직접 `Profile`을 생성할 때 기본값을 사용하면 미확인 여부가 사라지므로, 추천에서는
원래 입력의 키를 `known`으로 전달합니다. MCP는 이를 기본으로 처리합니다.

홈택스·마이데이터 인증, 원본 문서 파싱, 실제 데이터 조회는 구현돼 있지 않습니다.
`sources.py`와 `settlement_sources.py`는 전달받은 값의 출처·기준일·병합 규칙을
관리합니다. 계산 모듈은 프로필이나 스냅샷을 자체 저장하지 않습니다.

## 추천과 변화 감지

`BenefitStream`은 효과 금액, 반복 여부, 시작 시점, 기간을 표현합니다.
`value.py`가 현재가치와 공통 기간의 연간 환산액을 계산하고, `scoring.py`는
노력과 효과의 반복성을 분류해 뷰별로 정렬합니다. 마감은 별도로 표시합니다.
`assumptions.py`의 지속확률은 세법이 아닌 모델 가정이며 결과에 함께 표시됩니다.
ISA는 보유기간 종료 시점의 1회성 효과입니다.

`sources.py`의 `diff_profile_data`는 실제 값 변경, 자료 정정, 정보 보완,
출처 개선, 정보 소실을 구분합니다. 출처 없는 스냅샷 비교만으로는 실제 생활 변화와
자료 정정을 구분할 수 없습니다. 알림 전송·예약 실행은 호스트의 역할입니다.

## 규칙 검증과 테스트

규칙 파일에는 검토 근거와 검증 여부가 있습니다. 레거시 2025 규칙은 재검증 중이며,
통합 2025·2026 규칙도 전체 검증 완료 상태가 아닙니다. 테스트 통과는 구현의
회귀 검증이며 세법 전체의 정확성을 보증하지 않습니다.

```bash
uv run pytest -q
```

기부금 종류별 한도·이월, 배당가산 및 배당세액공제, 연도별 카드 특례 등 미지원
범위는 규칙 파일의 검증 메타데이터와 [통합 정산 범위](salary-settlement.md)에서
확인합니다. 테스트와 예제에는 실제 사용자 정보 대신 독립적인 합성 데이터를 씁니다.

### ISA 투자 시나리오의 범위

`simulate_isa`는 올해 소득세·연말정산 환급액과 독립적으로 **추가 납입분의
보유기간 전체 투자 세금**을 비교한다. 연 수익률을 단리로 적용하며, 기본
보유기간은 3년이다. 계좌 전체 순이익에 비과세 한도를 한 번 적용하고,
절세액은 종료 시점의 1회성 혜택으로 평가한다.

입력 `holding_years`로 보유기간을, `existing_net_gain`으로 추가 납입분을 제외한
계좌의 종료 시점 예상 순손익을 지정한다. 후자의 기본값 0은 명시적인 가정이다.
기존 수익으로 비과세 한도를 이미 소진한 경우 그 한도를 다시 주지 않는다.
일반계좌 수익 전액이 15.4% 원천징수되는 경우만 비교하며, 가입 자격 판정,
비과세 매매차익, 종합과세, 수수료, 이월 납입한도는 지원하지 않는다.
자동 추천의 역산 수익률은 가상 시나리오이므로 실제 예상 수익률과 기존 손익으로
다시 계산해야 한다.

ISA 응답은 `tax_scope: investment_holding_period`, `total_saving`,
`normal_account_tax`, `isa_account_tax`, `projected_gain`, `holding_years`를 제공한다.
두 세금 필드는 추가 납입에 따른 증분 세금이다. 기존의 `annual_saving`,
`baseline_total`, `simulated_total`은 ISA 응답에서 제거했으므로 연동 코드를 갱신해야 한다.
연금 시뮬레이터 응답은 그대로 유지한다.

근거: [KB국민은행 ISA 세제 혜택](https://obank.kbstar.com/quics?page=C041167),
[미래에셋증권 ISA 가입자격 및 종류](https://trading.securities.miraeasset.com/hks/hks4659/n02.do).
이는 해당 투자 시나리오의 근거이며 전체 세법 규칙 검증 완료를 뜻하지 않는다.


### 주거·문화비 입력 보완

2025년 주택청약 공제는 무주택 세대주 외에 세대주의 배우자도 포함한다.
`is_homeless_household_head_spouse`로 배우자 자격을 입력하며, 세대주가 아닌데
배우자 여부를 모르면 추천은 자격 미달 대신 추가 질문을 반환한다.
[국세청 주택마련저축 안내](https://www.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=239022&mi=40610)

카드 사용액은 일반 신용카드, 일반 직불·현금, 전통시장, 대중교통, 문화비를
중복 없이 분리해서 입력한다. 총급여 7천만원 초과자는 문화비를 버리지 않고
실제 결제수단의 일반 공제율로 계산한다. 이때 `culture_credit_card_spending`과
`culture_debit_cash_spending`의 합계가 `culture_spending`과 같아야 한다.
이 두 세부 금액은 문화비의 내역이므로 일반 사용액에 다시 더하지 않는다.
[국세청 카드 공제 산식](https://webtv.nts.go.kr/nts/cm/cntnts/cntntsView.do?cntntsId=7794&mi=2469)

프로필 금액·나이 등은 음수가 아닌 정수, 여부 값은 JSON 불리언만 허용한다.
CLI와 MCP 및 직접 Python 호출에 동일한 검증을 적용한다.
