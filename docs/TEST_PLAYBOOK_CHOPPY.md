# 찹(Choppy) 장 테스트 운영 플레이북 (임시 1–2일)

> 목적: 신호가 희소한 장에서도 데이터 수집과 파이프라인 검증을 보장하되, 안전장치를 유지한다.

## 결정 요약
- RTH 데일리 캡은 "액션 가능한 신호"만 카운트(컷오프·리스크 통과 후) — 유지.
- 병목(컷오프/쿨다운/분당 토큰 몰림)을 1–2일 한정 완화 후, 명시된 조건에 따라 즉시 롤백.

## 임시 파라미터(테스트 한정)
- 컷오프 델타: RTH 동적 컷오프에 −0.03 적용.
- 쿨다운: 180s → 120s(스팸 발생 시 즉시 150s로).
- 방향 락: 동일 심볼 부호 반전 재진입 90s 금지.
- 캡: per-ticker(티어별) + 글로벌 캡 동시 적용(설계 반영).
- 토큰: 분 초 0–10s에 한해 Tier A→Reserve 폴백 허용(로그 기록).
- LLM: `LLM_MIN_SIGNAL_SCORE=0.6` 유지(테스트 종료 시 0.7 복원).

## 롤백 조건
- 1h 내 actionable ≥ 5 & stopout_rate ≥ 40% 또는 flip-flop ≥ 25% → cutoff_delta 완화 축소, lock 120s, cooldown 150s.
- 2h 내 LLM calls 급증 또는 Tier-A overflow ≥ 15%/분 → LLM_MIN_SIGNAL_SCORE 0.65, Reserve 폴백 중단.
- 테스트 종료(최대 2일) 시 전부 원복: cutoff_delta 0, cooldown 180, LLM 0.7, 캡 기본.

## 모니터링 KPI(시간당)
- signals_generated vs suppressed 분포
- strong-signal rate(|score| ≥ cutoff+0.2)
- flip-flop count(동일 심볼 10분 창)
- MAE/MFE 중앙값, fill ratio/슬리피지(페이퍼라도 계산)
- token overflow rate(분당), LLM calls(누적)
- direction_lock hit 수

## 운영 메모
- Slack "조용한 시장" 알림은 1시간 요약만. 실시간 스팸 금지.
- 로그 키 네이밍 제안: `cap:act:{sym}:{date}`, `lock:{sym}`, `overflow:{min}`, `kpi:flipflop:{sym}:{hour}`.
- 타임라인 예: 서울 22:00 임시 파라미터 투입 → 02:00/06:00 체크포인트 리포트 → 결과 양호 시 다음날 원복.

