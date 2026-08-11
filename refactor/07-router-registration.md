# 7. 라우터 등록 2중 (C급)

## 문제

라우터를 추가하려면 [main.py](../backend/app/main.py) 두 곳을 고쳐야 한다.

- [main.py:20](../backend/app/main.py#L20) — 17개 모듈을 한 줄에 import
- [main.py:63-79](../backend/app/main.py#L63-L79) — `app.include_router()` 17줄

`frontend/src/config/pages.ts`로 해결한 것과 정확히 같은 구조의 문제다.
다만 **어긋나도 조용히 잘못되지 않는다** — import만 하고 등록을 빠뜨리면 라우트가 404가 되고,
등록만 하면 `NameError`로 즉시 죽는다. 그래서 C급.

## 해결안

```python
from app.routers import (
    health, ws, estop, params, models, datasets, hub, inference, eval_log,
    robots, cameras, logs, debug_logs, training, recording, policy_server, encoder,
)

ROUTERS = [
    health, ws, estop, params, models, datasets, hub, inference, eval_log,
    robots, cameras, logs, debug_logs, training, recording, policy_server, encoder,
]

for _router_module in ROUTERS:
    app.include_router(_router_module.router)
```

여전히 이름이 두 번 나오지만(import 목록 + `ROUTERS`), 등록 누락은 구조적으로 불가능해진다.
완전한 단일 소스를 원하면 `pkgutil.iter_modules`로 `app.routers`를 순회해 `router` 속성이 있는
모듈을 자동 등록할 수도 있지만, **등록 순서가 암묵적이 되고 import 부작용이 숨는다.**
명시 리스트 쪽을 권장한다.

## 주의

- 현재 `main.py:20`의 import가 로깅 필터 설정([line 10-19](../backend/app/main.py#L10-L19)) **뒤에**
  일부러 배치되어 있다. `uvicorn.access` 필터가 라우터 import보다 먼저 붙어야 하는 것으로 보이므로
  import 위치를 위로 옮기지 말 것.
- 라우터 등록 순서가 경로 매칭에 영향을 주는 경우가 있는지 확인 (현재는 prefix가 모두 달라
  문제 없어 보인다).

## 검증

- 서버 기동 후 `GET /health` 200
- `GET /openapi.json`의 경로 목록이 변경 전과 동일한지 비교 (가장 확실)

## 상태

☐ 미착수
