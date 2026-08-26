# 멀티테넌트 커스텀 설정 분리 계획 (v6 파이프라인)

> 상태: **설계 계획서 (구현 전)** · 작성일 2026-08-26 · 대상 `scripts/perf_analytics_v6/pipeline.py`

## 1. 목표

- 코드 1벌로 N개 고객사를 서비스한다. **테넌트 특화 설정을 코드에서 데이터(설정)로** 이동한다.
- 대표 사례인 `derive_page_group`의 로케일 휴리스틱을 **플러그형 전략**으로 일반화한다.
- **기존 고객(Samsung) 동작은 100% 보존**한다(골든 리포트 회귀로 검증).

### 배경 — 왜 지금

`derive_page_group`([pipeline.py:90](../scripts/perf_analytics_v6/pipeline.py))은 "URL 첫 경로 세그먼트가 1~5자면 로케일로 간주하고 그 다음 세그먼트를 page type으로" 라는 규칙을 쓴다. 이는 `samsung.com/{locale}/{category}/...` 구조에 맞춘 **고객사 특화 패턴**이라 범용적으로 쓸 수 없다. 멀티테넌트를 도입하려면 이런 테넌트 특화 로직을 분리해야 한다.

## 2. 현황 진단

- **인프라 설정**(paths / ftp / llm / email / retry)은 이미 `config/ops_pipeline.conf` + `AppConfig`로 외부화되어 있다.
- **분석 로직**(page_group 규칙, 차원 구성, 임계값)은 `pipeline.py`에 **모듈 전역 상수로 하드코딩**되어 있고, 일부만 ENV로 뺀 상태다 (`ARTIFACT_MS`, `SEVERITY_FLOOR_MS`, `EFFECT_FLOOR_MS`, `TIMER_METRIC`).

## 3. 무엇이 테넌트 특화인가 — 인벤토리

| 분류 | 현재 위치(하드코딩) | 처리 방향 |
|---|---|---|
| **① 테넌트 특화 (반드시 분리)** | | |
| page_group 파생(로케일 휴리스틱) | `derive_page_group` (pipeline.py:90) | → 전략 플러그인 |
| top_k(=15) 그룹 수 | 같은 함수 인자 | → 테넌트 설정 |
| 차원 구성 | `SECONDARY_DIMS`, `CATEGORICAL`, `DELIVERY_NUMERIC` (39–44), `ENV_EXTRA_DIMS` (480) | → 테넌트 설정(비콘 필드 의존) |
| 지역/국가 라벨 | `REGION_NAMES` (2834) | → 테넌트/공용 혼합 |
| 이메일 수신자·FTP 소스 | `ses_email.conf`, `[ftp]` | 이미 외부화(테넌트별 분리만) |
| **② 메트릭 공용이나 튜닝 가능** | | |
| 임계·플로어 | `SEVERITY_FLOOR_MS`(47), `MIN_SEG_N`(49), `MIN_SHARE_PP`(50), `MAX_FOCUS`(51), `ARTIFACT_MS`(46), `EFFECT_FLOOR_MS`, `NEW_SEG_FOCUS_*`(620–621), `RESOURCE_FLOORS`(511) | → 테넌트 오버라이드(기본값 유지) |
| 메트릭 프로파일 | `METRIC_PROFILES` (854) — good/poor/artifact/delivery_relevant | → 공용 기본 + 테넌트 오버라이드 |
| **③ 제품 공용 (코드 유지)** | | |
| DFL 분해·SHAP·검증기·number-binding·섹션 구조 | 전반 | 유지 |
| Akamai 플레이북 | `AKAMAI_PLAYBOOK` (2650) | 유지(추후 화이트라벨 시 분리) |
| 로컬라이제이션 | `_LANG_NAMES` (1471) | 유지 |

## 4. 설계 — 테넌트 프로파일 레이어

**새 파일**: `config/tenants/<tenant_id>.toml` (테넌트당 1개, **비밀정보 없음** → PUBLIC repo 안전; 비밀은 `.sec/<tenant>/`에 분리).

```toml
[tenant]
id = "samsung"
display_name = "Samsung Electronics"

[page_group]
strategy = "locale_then_segment"     # §5 전략 중 택1
locale_pattern = "[a-z_\\-]{1,5}"    # 또는 locale_whitelist = ["sec","us","in",...]
top_k = 15
other_label = "other"

[dimensions]
secondary = ["country","connectiontype","deviceType","isp"]
delivery_numeric = ["cdncacherate","origintime","edgetime"]
env_extra = ["connectiontype","rtt_bucket"]

[thresholds]              # 생략 시 공용 기본값
severity_floor_ms = 50
min_seg_n = 300
max_focus = 3

[metrics.waitingtime]     # 메트릭별 오버라이드(선택)
poor_ms = 400
```

**로딩**: `TenantConfig` 데이터클래스 → `run_v6(csv_path, ..., tenant=TenantConfig)`로 주입. 현재 ENV 오버라이드 방식(`ARTIFACT_MS` 등)을 확장해 모듈 전역 상수를 테넌트 값으로 대체한다.

## 5. page_group을 플러그형 전략으로

`derive_page_group`을 `strategy`로 분기한다.

| 전략 | 동작 | 용도 |
|---|---|---|
| `locale_then_segment` | 현행 Samsung 방식(로케일 뒤 첫 세그먼트). `locale_whitelist`로 휴리스틱 오작동 방지 가능 | 기본/하위호환 |
| `first_segment` | 로케일 없이 첫 세그먼트 | 로케일 없는 사이트 |
| `depth_n` | 첫 N개 세그먼트(`/smartphones/galaxy-a`) | 세분화 필요 |
| `regex_map` | 테넌트가 작성한 순서형 규칙 `[{pattern:"^/event/", name:"event"}, ...]` | 정보구조 명시 매핑(최고 유연) |

모든 전략 뒤에 공통 `top_k` + `other` 접기를 적용한다. **미설정 시 = 현행 동작**(하위호환).

### 알려진 취약점(계획에 반영)

현행 로케일 휴리스틱은 "첫 세그먼트 ≤5자 = 로케일"로 가정한다. 로케일 없이 첫 경로가 5자 이하인 사이트(예: 로케일 없는 `/tvs/micro-rgb/`)에서는 `tvs`를 로케일로 오인한다. `locale_whitelist` 옵션으로 이 오작동을 차단할 수 있게 한다. 또한 로케일 세그먼트만 있는 URL(`/sec/`)이 `sec` page type이 되는 엣지케이스도 전략 레벨에서 처리 여부를 결정한다.

## 6. 테넌트 식별(라우팅)

CSV가 어느 테넌트인지 결정하는 방법(택1, 승인 필요):

- **(권장) mPulse app id**: 비콘의 `app`/도메인 필드 → 테넌트 매핑 테이블. 데이터 자체로 결정되어 견고.
- 테넌트별 **incoming 디렉토리** / FTP 소스 분리 → 경로로 결정.
- 파일명 규약에 tenant 태그.

모니터(`watch_incoming.py`)가 `run_v6` 호출 전 테넌트를 해석해 주입한다.

## 7. 단계별 이행 (기존 고객 무중단)

1. **Phase 1 — 무동작 추출**: 위 상수들을 `tenants/samsung.toml`로 옮기되 값은 현행과 동일. `TenantConfig` 로딩 추가. **71-샘플 골든 리포트가 바이트 동일**한지 회귀(핵심 안전장치).
2. **Phase 2 — page_group 플러그화**: 전략 분기 도입, 기본 = `locale_then_segment`. 동일 회귀.
3. **Phase 3 — 2번째 테넌트 파일럿**: 로케일 없는 가상/실제 테넌트로 격리·라우팅 검증.
4. **Phase 4 — 튜닝 노출**: 임계·메트릭 오버라이드까지 테넌트화.

## 8. 보안 (멀티테넌트)

- 테넌트 **비밀 격리**: `.sec/<tenant>/`(FTP·SES). PUBLIC repo에 테넌트 config를 커밋할 때 **비밀 키는 절대 불가**(비밀은 참조만).
- **데이터·리포트 크로스-테넌트 누출 방지**: 이메일/저장 경로를 테넌트 키로 강제 분리. 테넌트 해석 실패 시 처리 중단(fail-closed).

## 9. Effort / Risk

| Phase | Effort | Risk |
|---|---|---|
| 1 (무동작 추출) | 중 | 낮음 — 골든 회귀로 담보 |
| 2 (page_group 전략) | 중 | 낮음 — 기본값 하위호환 |
| 3 (라우팅·2nd 테넌트) | 중~높음 | **중** — 오라우팅/누출 → 테스트·fail-closed로 완화 |
| 4 (튜닝 노출) | 낮음 | 낮음 |

## 10. 승인·결정이 필요한 사항

1. **테넌트 식별 방식**: mPulse app id(권장) / incoming 분리 / 파일명 중?
2. **설정 포맷**: TOML(권장) / YAML / JSON?
3. **테넌트 config 저장 위치**: PUBLIC repo(비밀 제외) vs 별도 private 저장소?
4. **1차 범위**: page_group만 우선 분리 vs 분석 노브 전체 externalize?
5. Akamai 플레이북 화이트라벨(벤더 중립화)까지 로드맵에 넣을지?

---

*구현 시 워크플로우: Phase 1(무동작 추출 + 골든 회귀)부터 계획 → 승인 → 구현 → 검증 → 노트북 동기화 → 배포.*
