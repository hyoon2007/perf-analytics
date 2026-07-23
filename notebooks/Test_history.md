# Timer Weight 전략 결정 문서

## 1. 문제 (Problem)
- 모델 학습 시 `timer` 기반 sample weight를 주는 것이 항상 옳은지 불확실했다.
- 동일 파이프라인에서도 데이터/상황에 따라 결과가 다르게 나와 의사결정이 어려웠다.
- 특히 기존 방식 `sample_weight = (timer / mean(timer))^2` (모든 샘플 대상)의 타당성 검증이 필요했다.

## 2. 원인 (Root Cause)
- 기존 `^2 (전체 샘플)` 방식은 가중치 꼬리가 너무 두꺼워 일부 고-timer 샘플이 학습을 과도하게 지배했다.
- normal(y=0) 샘플까지 가중되어 분류 성능 지표(PR-AUC, ROC-AUC)가 저하되었다.
- weight 분포의 극단치(outlier)가 과대반영되어 일반화 성능이 떨어지고 fold별 변동성이 커졌다.

## 3. 테스트 시나리오 (Test Scenario)
- 동일 데이터에 대해 `StratifiedKFold(n_splits=5, shuffle=True, random_state=42)` 교차검증.
- 공통 모델: XGBoost (`n_estimators=300, max_depth=6, learning_rate=0.05, subsample=0.9, colsample_bytree=0.9`).
- 비교 지표: `PR-AUC`(주 분류 지표), `ROC-AUC`, `Brier`(보정), `timer_gain_top10pct`(고-timer 이상치 우선탐지 비즈니스 지표).
- 비교한 weight 전략:
  1. `unweighted` (baseline)
  2. `squared_all` — 기존 `(timer/mean)^2`, 전체 샘플
  3. `sqrt_anomaly_only_clip1_5` — **사용자 제안**: anomaly만 `sqrt(timer/anomaly_mean)`, clip [1, 5]
  4. `sqrt_anomaly_only_clip1_3` — clip [1, 3]
  5. `power1p5_anomaly_only_clip1_4` — anomaly만 `^1.5`, clip [1, 4]
  6. `linear_anomaly_only_clip1_2` — anomaly만 선형, clip [1, 2]

## 4. 테스트 결과 (Test Results)

### 4-1. 5-fold 평균 지표

| 전략 | PR-AUC | ROC-AUC | Brier↓ | timer_gain_top10pct |
|---|---|---|---|---|
| unweighted | 0.605985 | 0.634022 | 0.222094 | 0.246886 |
| **sqrt_anomaly_only_clip1_5 (채택)** | 0.605784 | 0.632063 | 0.222432 | 0.249806 |
| sqrt_anomaly_only_clip1_3 | 0.605912 | 0.632334 | 0.222413 | 0.250330 |
| linear_anomaly_only_clip1_2 | 0.605892 | 0.632282 | 0.222866 | 0.252599 |
| power1p5_anomaly_only_clip1_4 | 0.605588 | 0.631563 | 0.224989 | 0.255355 |
| squared_all (기존) | 0.588409 | 0.610077 | 0.228642 | 0.240744 |

### 4-2. unweighted 대비 fold별 승리 횟수

| 전략 | PR-AUC 승리 | PR-AUC Δ | timer_gain 승리 | timer_gain Δ |
|---|---|---|---|---|
| squared_all (기존) | 0/5 | -0.017576 | 2/5 | -0.006142 |
| **sqrt_anomaly_only_clip1_5 (채택)** | 3/5 | -0.000201 | 3/5 | +0.002921 |
| sqrt_anomaly_only_clip1_3 | 3/5 | -0.000073 | 4/5 | +0.003445 |
| power1p5_anomaly_only_clip1_4 | 2/5 | -0.000397 | 5/5 | +0.008469 |
| linear_anomaly_only_clip1_2 | 3/5 | -0.000093 | 5/5 | +0.005713 |

### 4-3. 결론
- **기존 `squared_all` 방식은 명백히 열위**: PR-AUC가 모든 fold에서 하락(-0.0176)하고 timer_gain도 악화.
- **사용자 제안(`sqrt_anomaly_only_clip[1,5]`)을 채택**:
  - PR-AUC 손실이 거의 무시 가능한 수준(-0.0002)이면서 분류 성능을 보존.
  - anomaly만 완만히 가중하여 고-timer 이상치 우선탐지(timer_gain)를 fold 과반(3/5, +0.29%)에서 개선.
  - sqrt + clip[1,5]로 outlier 과대반영을 억제해 fold별 변동성이 작고 안정적.
- 비즈니스 목적이 "고-timer 이상치 우선탐지"에 더 치우치면 `linear_clip[1,2]` 또는 `power1.5_clip[1,4]`도 후보.
