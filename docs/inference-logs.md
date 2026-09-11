# 추론 CSV 로그

gRPC 추론을 돌리면 매 스텝을 CSV 로 남긴다. 웹 [로그 → 파일] 탭에서 내려받고 차트를 볼 수
있고, 아래 도구로 오프라인 분석도 한다. (README 에 있던 절 — 설치와 무관해 여기로 옮겼다.)

- **파일 위치**: `/tmp/piper_inference_YYYYMMDD_HHMMSS.csv` — `/tmp` 라 오래 두지 않는다
- **종료 시** WebSocket 으로 파일 경로를 알린다 (추론 페이지 하단에 내려받기 링크)

## 기록 컬럼

| 구분 | 컬럼 | 설명 |
|------|------|------|
| 메타 | `timestamp`, `step`, `fps`, `queue_size` | 시간, 스텝, FPS, 액션 큐 잔량 |
| 정책 출력 | `target_{joint}.pos` | 필터 적용 전 원본 액션 |
| 전송값 | `filtered_{joint}.pos` | 속도 제한 + 저역통과 등 필터 후 실제 전송 |
| 로봇 위치 | `actual_{joint}.pos` | 로봇이 보고한 현재 관절 위치 |
| 기타 | `task`, `paused` | 태스크 문자열, 일시정지 여부 |

## API

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /api/logs` | 로그 파일 목록 (CSV·차트) |
| `GET /api/logs/download/{filename}` | 파일 다운로드 |
| `GET /api/logs/chart/{filename}` | CSV → 차트 HTML |

## 분석 도구

`tools/analyze_inference_log.py` — pandas + matplotlib 기반 시각화.

```bash
# 통계 + 그래프 (GUI)
python tools/analyze_inference_log.py /tmp/piper_inference_20260404_213500.csv

# 특정 관절만
python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --joint joint1

# PNG 저장 (GUI 없이, 서버 환경)
python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --save

# 통계만 출력
python tools/analyze_inference_log.py /tmp/piper_inference_*.csv --no-plot
```

### 출력 예시

```
============================================================
Duration: 45.2s | Steps: 1356 | Avg FPS: 30.0
Queue empty: 226/1356 (16.7%)
============================================================
Joint               |target-actual| mean        max        std
------------------------------------------------------------
joint1.pos                         1.234      5.678      0.891
...

Joint               |target-filtered| mean        max
----------------------------------------------------
joint1.pos                           0.456      2.345
...
```

### 생성 파일 (`--save`)

| 파일 | 내용 |
|------|------|
| `*_overview.png` | 관절별 target / filtered / actual 비교 |
| `*_error.png` | 관절별 추적 오차 (target - actual) |
| `*_fps_queue.png` | FPS + 액션 큐 사이즈 시계열 |
