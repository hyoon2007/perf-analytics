# v6 리포트 판정 로직 (Verdict & Recommended Actions)

이 문서는 v6 파이프라인(`pipeline.py`)이 이상 리포트를 **어떻게 자동으로 구성**하는지
설명합니다: **판정(verdict, §1–4)**, **권장 조치(Recommended Actions, §5–6)**,
**문제 섹션(focus) 선정과 materiality 게이트(§7)**, **서술 문장 생성·스코프 규칙(§8)**.
이 모두는 LLM이 지어내는 것이 아니라 **결정론적 규칙**으로 정해지며, LLM은 정해진
내용을 고객용 문장으로 다듬기만 합니다.

## 1. 큰 그림 — 숫자가 오르는 이유는 2가지

파이프라인은 **"정상 시간대" vs "이상 시간대"** 두 구간의 페이지 로딩 시간(p75)을
비교합니다. 숫자가 올라갔을 때 근본 원인은 둘 중 하나(또는 둘 다)입니다.

| 원인 | 뜻 | 마트 비유 |
|---|---|---|
| **① 구성 변화 (mix)** | 사이트가 실제로 느려진 게 아니라, 원래 느린 페이지로 트래픽이 더 몰림 | 느린 계산대에 손님이 더 줄 섬 |
| **② 자체 저하 (within / regression)** | 같은 페이지가 스스로 진짜 느려짐 (진짜 성능 문제) | 계산원 자체가 느려짐 |

판정은 결국 **"이 둘 중 무엇이, 몇 개 구간에서 일어났나"**를 분류하는 이름표입니다.

## 2. 판정에 쓰는 재료

`select_verdict()`가 읽는 신호들:

| 재료 | 쉬운 뜻 |
|---|---|
| **severity** | 변화가 애초에 의미 있을 만큼 큰가? (작으면 무시) |
| **delivery.verdict** | CDN/원본서버 배달 구간(edgetime·origintime 등)이 나빠졌나? |
| **mix_material** | ① 구성 변화 효과가 무시 못 할 만큼 큰가 |
| **within_material** | ② 자체 저하 효과가 무시 못 할 만큼 큰가 |
| **focus 개수 (multi)** | 문제 구간이 1개냐, 2개 이상이냐 |
| **coverage** | 지목한 구간들이 전체 변화를 다 설명하는가 |

> **material("유의미")의 뜻**: 효과가 절대 기준값(`abs_floor_ms`)과 상대 기준값
> (`rel_floor`)을 **둘 다** 넘어야 "진짜 효과"로 인정됩니다. 찔끔한 효과는 버립니다.

## 3. 판정 흐름도

위에서부터 순서대로 내려가며 **처음 걸리는 조건**이 판정이 됩니다.

```mermaid
flowchart TD
    Start(["정상 vs 이상 시간대 p75 비교"]) --> Q1{"severity가<br/>none / info / improved?"}
    Q1 -->|예| NoAction["<b>no_action</b><br/>의미 있는 저하 없음"]
    Q1 -->|아니오| Q2{"delivery.verdict<br/>== degraded?"}
    Q2 -->|예| Delivery["<b>delivery_regression</b><br/>배달 구간부터 조사"]
    Q2 -->|아니오| Q3{"문제 구간이<br/>2개 이상 (multi)?"}

    Q3 -->|"예"| Q4{"자체 저하가 material?<br/>self-regressed 구간 존재"}
    Q4 -->|예| MSR["<b>multi_segment_regression</b><br/>여러 구간이 각자 진짜 느려짐"]
    Q4 -->|아니오| MSM["<b>multi_segment_mix_shift</b><br/>여러 느린 구간이 비중만 커짐"]

    Q3 -->|"아니오, 단일"| Q5{"어떤 효과가<br/>material인가?"}
    Q5 -->|"mix + within 둘 다"| MSL["<b>mix_shift_with_local_regression</b><br/>비중도 커지고 + 자체로도 느려짐"]
    Q5 -->|"mix만"| TMS["<b>traffic_mix_shift</b><br/>느린 구간에 트래픽만 몰림"]
    Q5 -->|"within만"| SR["<b>segment_regression</b><br/>특정 구간이 스스로 느려짐"]
    Q5 -->|"둘 다 미미"| TMSB["<b>traffic_mix_shift_broad</b><br/>원인이 여러 곳에 얕게 퍼짐"]
```

> **coverage 참고**: 지목한 구간이 전체 변화를 다 설명하지 못하면(`coverage < 100%`)
> "일부 미설명분 있음" 문구만 덧붙고 **판정 코드는 바뀌지 않습니다.**

## 4. 판정 코드 요약

| 코드 | 무슨 상황인가 | 걸리는 조건 |
|---|---|---|
| **no_action** | 의미 있는 저하 없음 (극단값 몇 개가 평균만 부풀렸을 수 있음) | severity = none/info/improved |
| **delivery_regression** | CDN/원본 배달 구간이 나빠짐 → 여기부터 조사 | delivery = degraded |
| **multi_segment_regression** | 여러 구간이 각자 진짜 느려짐 | 문제구간 ≥2 **그리고** 자체저하 material |
| **multi_segment_mix_shift** | 여러 느린 구간이 비중만 커짐 (나머지는 멀쩡) | 문제구간 ≥2, 자체저하 미미 |
| **mix_shift_with_local_regression** | 한 구간이 비중도 커지고 + 자체로도 느려짐 | 문제구간 1, mix·within **둘 다** material |
| **traffic_mix_shift** | 느린 구간에 트래픽만 몰림 (진짜 느려진 건 아님) | 문제구간 1, **mix만** material |
| **segment_regression** | 특정 구간이 스스로 느려짐 (구성으로 설명 안 됨) | 문제구간 1, **within만** material |
| **traffic_mix_shift_broad** | 원인이 여러 곳에 얕게 퍼짐 (단일 주범 없음) | 문제구간 1, mix·within **둘 다** 미미 |

## 5. Recommended Actions — 어떻게 선택되나

권장 조치도 LLM이 지어내지 않습니다. **규칙 기반 플레이북 매칭**으로 정해지고 LLM은
문장만 다듬습니다.

### 흐름

1. **플레이북 정의** (`playbook.py`의 `AKAMAI_PLAYBOOK`): 조치 후보들이 "데이터"로
   들어있습니다. 각 항목 = `applies_to`(적용 메트릭) + `when`(조건들) + `levers`(Akamai
   기능) + `action`(문장 원본) + `scope_tag`.
2. **매칭** (`match_playbook`): 각 항목에 대해 — ① 현재 메트릭이 `applies_to`에 있고,
   ② `when`의 **모든** 조건이 참이면 선택 → `findings["remediation_playbook"]`.
   조건 문법은 단순 4종: `a.b == x`, `a.b > n`, `a.b < n`, `a.b in x,y,z`.
3. **LLM은 문장만 재작성**: 프롬프트가 *"Recommended Actions는 오직
   remediation_playbook의 action만 사용하고, 새 조치를 지어내지 말 것"*을 강제.
4. **스코프 가드**: `check_recommendation_scope`가 플레이북 밖 조치를 발견하면 draft를
   거부하고 재시도. critic 패스도 out-of-scope를 감시. LLM이 끝내 실패하면
   `render_fallback_report_v5`가 **같은 플레이북 action을 결정론적으로 삽입**.

즉 판정(verdict)이 플레이북 조건에 그대로 들어가 **"원인 → 대응"**이 자동 연결됩니다.

### 플레이북 항목 (요약)

| id | 언제 켜지나 (`when`) | 적용 메트릭 | Akamai lever |
|---|---|---|---|
| `ivm_hero_cold_cache` | verdict ∈ (mix-shift 계열) **AND** 신규방문자 유입 | lcp, fcp | Image & Video Manager, EdgeWorkers(LCP preload) |
| `prefetch_event_landing` | 신규방문자 유입 **AND** 랜딩 진입비중 > 70% | lcp, fcp, ttfb | EdgeWorkers(Early Hints), Adaptive Acceleration |
| `offload_regional_origin` | delivery = clean **AND** 자체저하 있음 | lcp, fcp, ttfb | Tiered Distribution, Cloud Wrapper |
| `delivery_investigate` | delivery = degraded | lcp, fcp, ttfb, tbt | mPulse + DataStream 2, 원본 health/offload |
| `alert_segmentation` | verdict ∈ (mix-shift 계열) | 전체 | mPulse 알림 분리(page-group/geo) |
| `third_party_release_audit` | 자체저하(within_regression) 있음 | 전체 | Script Management, 릴리스 변경 상관분석 |
| `reduce_main_thread_blocking` | verdict ∈ (mix-shift 계열 + segment_regression) | tbt | Script Management(defer/async), EdgeWorkers |

> *mix-shift 계열* = `traffic_mix_shift`, `multi_segment_mix_shift`,
> `mix_shift_with_local_regression`, `multi_segment_regression`.

### 예 (지난 테스트 실행)

findings가 `delivery = degraded`, `within_regression = true`, metric = lcp였고, 그 결과:

- **`delivery_investigate`** (조건 `delivery.verdict == degraded`) → "배달 경로 회귀
  조사: DataStream 2로 원본 응답·오프로드 상관분석"
- **`third_party_release_audit`** (조건 `within_regression == true`) → "최근 릴리스/
  서드파티 태그 변경 점검, 두 구간 리소스 워터폴 비교"

두 조치가 자동 선택됐습니다. 반면 `ivm_hero_cold_cache`·`alert_segmentation`은
`verdict ∈ (mix-shift 계열)`을 요구하는데 실제 verdict가 `no_action`이라 탈락했습니다.

## 6. 새 조치를 추가/수정하려면

코드 변경 없이 **`playbook.py`의 `AKAMAI_PLAYBOOK` 리스트에 항목 하나를 추가**하면
됩니다(조건 predicate + action 문장 + levers + scope_tag). 매칭·검증·폴백이 모두
데이터 기반이라 자동으로 반영됩니다.

## 7. Focus 섹션 선정과 materiality 게이트

판정과 리포트 본문은 **어느 페이지 섹션(page_group)이 변화를 일으켰나**에서
출발합니다. `top_movers()`가 트래픽 비중·내부 p75가 가장 많이 움직인 후보를
발굴하고, `select_focus_segments()`가 그중 **문제 섹션(focus)**을 고릅니다.

**자격 조건** (둘 중 하나면 후보):
- `gained_share`: 사이트보다 느린 섹션이 트래픽 비중을 의미 있게 늘림, 또는
- `self_regressed`: 내부 p75가 기준 이상 악화 + 비중이 사소하지 않음(≥1.5%).

후보는 **기여도 점수(contribution_score)**로 정렬됩니다 —
`(비중증가/100)×정상p75 + (이상비중/100)×p75악화분` — 즉 사이트 전체 p75 상승에
얼마나 기여했는지의 근사치입니다.

**materiality 게이트** (v6.9.1): 1위 섹션은 항상 유지하고, **2위 이하는**
`min_contribution_ratio`(기본 0.15, 1위 대비 비율)와 `min_contribution_abs`(기본
15.0, 절대값)를 **둘 다** 넘어야 헤드라인 focus로 남습니다. 미달 섹션은 버리지 않고
`additional_segments`(요약)로 강등합니다.

> **왜 필요한가**: 트래픽 2%짜리 섹션이 **내부적으로만** 크게 튀어도 사이트 전체
> 기여는 미미할 수 있습니다. 예) `smartphones` 기여 219 vs `unpacked` 기여 8.6(142ms
> 중 ~8.6ms). 게이트가 없으면 `unpacked`가 주범 옆에 **이유 없이 나란히** 헤드라인에
> 올라 혼동을 줍니다. 게이트가 이를 요약 항목으로 강등합니다.

focus 개수는 그대로 판정(`multi` 여부)에 반영됩니다(§3 참조).

## 8. 서술 문장(narrative facts) 생성과 스코프 규칙

리포트의 **모든 핵심 문장은 파이썬이 미리 작성**하고(`build_narrative_facts` /
`build_section_facts`) LLM은 그것을 각 섹션에 배치·재서술만 합니다. 목적은 LLM이
큰 JSON에서 숫자를 잘못 집어오는 것을 막는 것입니다.

### behavior(방문자) 신호 — 스코프와 게이트
- **스코프 = focus 섹션**: 방문자 신호는 사이트 전체가 아니라 **문제 섹션 내부**에서
  계산됩니다(`behavior_signals(focus_df)`). 그래서 섹션의 cache 적중률 중앙값은
  사이트 전체 중앙값과 **다릅니다**. 문장은 이를 혼동하지 않도록
  *"Within the '<섹션>' page section, …"*으로 **스코프를 명시**합니다(초점이 없어
  `scope=overall`이면 접두 없이 사이트 전체값).
- **캐시 적중률 표기**: 브라우저 캐시 적중률(`cacherate`)·CDN 캐시 적중률
  (`cdncacherate`)은 리포트에서 **%로 표기**합니다(`fmt_pct`). 업스트림이 캐시 적중률
  데이터를 **0–100(%) 스케일**로 전송한다는 전제이며, 이보다 큰 스케일(예 0–10000)이
  들어오면 값이 그대로 %로 붙어(예 "1,676%") 오표기되니 데이터 전환과 **함께** 배포해야
  합니다.
- **new_visitor_influx 게이트**: *"more first-time visitors"* 서술은 신호가 실제로
  발화했을 때만 나갑니다. 발화 조건 = 다음 3개 중 **2개 이상**: 진입(landing) 비중
  +2pp 이상, referrer 비중 −2pp 이상, cache 중앙값이 정상의 0.8배 미만. 미발화 시엔
  숫자 없이 *"audience composition is not a factor"*를 **What Did Not Change**에 둡니다.

> 두 규칙 모두 v6.9.1 수정입니다. 이전에는 이 문장이 **무조건** 생성되고 방향과
> 무관하게 "more first-time visitors"를 단정해, 진입 비중이 오히려 **줄어든** 창에서도
> 신규 방문자 증가를 주장하는 버그가 있었습니다.

### 절대 심각도 배지 (v6.9.1)
delta가 작아도 baseline 자체가 재앙일 수 있습니다(예 TBT p75 ~2,400ms는 'poor'
기준 600ms의 수 배). 메트릭 프로파일의 등급(good / needs-improvement / poor)을 읽어,
이상 창이 'poor'면 **"chronic baseline issue"** 문장을 Executive Summary에 추가합니다.
숫자를 넣지 않아 번호 검증기를 건드리지 않습니다.

### client-side 원인 귀속 (v6.9.1)
메인스레드 계열 메트릭(현재 `tbt`)에서 **자체 저하 + 배달(delivery) clean**이면,
추가 blocking이 네트워크가 아니라 **client-side(메인스레드 JS·서드파티 태그)**에서
온다는 문장을 추가합니다. 독자가 배달 구간을 헛짚지 않도록 방향을 잡아줍니다.

### 초점 섹션 하위 구성 분해 (v6.9.2)
초점 페이지 섹션이 커졌을 때, **그 안에서 무엇이 커졌는지**를 명시합니다.
`focus_breakdown()`이 초점 섹션 내부에서 **비중이 늘고(≥1pp) 초점 평균보다 무거운**
하위 세그먼트를 차원별(`paidmedia`, `deviceMemory`(버킷), `country`, `connectiontype`,
`deviceType`)로 찾아, 기여도 순 상위 3개를 `findings.focus_breakdown`에 담고
*"Within the '<섹션>' section, the growth is concentrated in India (46.8%→49.3%),
4GB-memory devices (33%→35%), paid-media entries (69.9%→72.7%) — all higher-TBT
sub-segments …"* 형태로 **What Changed**에 서술합니다. 하위 구성 이동이 있으면 앞의
*"audience composition is not a factor"* 문구는 모순되므로 자동 억제됩니다.

### 상호작용 mix/within 분해 (v6.9.2)
`quantile_decomposition`을 **단일 차원(page_group)** 대신 **상호작용 셀 키
`(page_group × device-memory-bucket × paidmedia)`**로 계산합니다. 이렇게 하면
"페이지그룹 내부에서 더 무거운 하위 세그먼트(유료·저사양)로의 이동"이 **MIX(구성)**로
집계되어, page_group 1차원 분해가 이를 **가짜 자체 회귀(WITHIN)로 오귀속**하던 문제를
바로잡습니다. 또한 `within_regression` 플래그를 이 구성-통제 materiality와 AND로 묶어,
구성 이동일 뿐인데 verdict가 `mix_shift_with_local_regression`이 되거나 client-side/
서드파티 회귀로 서술되는 과대주장을 막습니다.

> 검증(7-27 TBT): 분해가 **313ms 구성 / 5ms 자체**, verdict가
> `mix_shift_with_local_regression` → **`traffic_mix_shift`**로 정정, India·4GB·유료광고가
> 리포트에 명시됨.

### 페이지 유형별 자체-회귀 판정 (v6.9.4)
focus 페이지 유형의 "자체적으로 느려졌다" 역할 문구는 **원(raw) p75 변화가 아니라 그
유형의 구성-통제 분해**에 근거합니다. `focus_regression_split()`이 각 focus 유형의 자체
p75 변화를 `(country × mem_bucket × paidmedia)` 셀 DFL로 **mix(내부 구성 이동) vs
within(같은-오디언스 자체 저하)**으로 나누고, `genuine_regression = within이 material AND
within ≥ mix` 일 때만 "genuinely slowed"로 표기합니다. 그렇지 않으면 "무거운 자체
트래픽을 더 끌어들인 구성 효과, 페이지 자체는 느려지지 않음"으로 서술 → focus_breakdown
서술과 모순되지 않습니다. `select_verdict`의 multi-segment `any_self`도 이 플래그를 씁니다.

> 검증(7-28_1143 TBT): `smartphones` mix +476 / within +127 → **구성 효과**(자체 저하 아님),
> `watches` mix +872 / within +1,079 → **자체 회귀**(직접 조사). 이전에는 둘 다 raw p75
> 상승만으로 "자체 저하"로 잘못 표기돼 하위 구성 서술과 모순됐음.
>
> 고객 설명용 시각 문서(구성 vs 자체 분해, 위 예시 포함)는 아티팩트로 별도 제공됨.

### 번호 검증과의 관계
위 문장 중 숫자를 담는 것(headline·decomposition·audience·focus_breakdown 등)의 값은
findings에서 그대로 온 화이트리스트 숫자이며, 새로 추가한 심각도·client-side 문장은
**number-free**라 검증·critic·바인딩 체크를 통과합니다.
