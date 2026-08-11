# 3. wrapper 부트스트랩 40줄 복붙 ×3 (B급)

## 문제

`lerobot.policies.__init__` 우회(Python 3.13 groot 호환) 부트스트랩이 wrapper 3개 파일에
거의 그대로 복사되어 있다.

| 파일 | 범위 |
|---|---|
| [start_record.py:14-37](../wrapper/start_record.py#L14-L37) | 더미 패키지 등록 + `_config_imports` + groot 더미 |
| [start_policy_server.py:14-37](../wrapper/start_policy_server.py#L14-L37) | 동일 + groot config 직접 로드 |
| [grpc_wrapper.py:20-47](../wrapper/grpc_wrapper.py#L20-L47) | 동일 (alias만 `_il`/`_os`/`_types`로 다름) |

`_config_imports` dict 6줄은 세 파일에서 **완전히 동일**하다:

```python
_config_imports = {
    "ACTConfig": "lerobot.policies.act.configuration_act",
    "DiffusionConfig": "lerobot.policies.diffusion.configuration_diffusion",
    "PI0Config": "lerobot.policies.pi0.configuration_pi0",
    "PI05Config": "lerobot.policies.pi05.configuration_pi05",
    "SmolVLAConfig": "lerobot.policies.smolvla.configuration_smolvla",
    "VQBeTConfig": "lerobot.policies.vqbet.configuration_vqbet",
}
```

LeRobot이 정책을 추가하면 세 파일을 다 고쳐야 한다. `lerobot_wrapper.py`의
`POLICY_IMPORTS`(#2 참조)와도 목록이 겹친다.

## 차이점 (추출 시 보존해야 할 것)

- `start_policy_server.py`: groot config만 직접 로드하는 블록이 뒤에 추가로 있음
- `start_policy_server.py`: 정책별 프로세서 등록(`processor_smolvla` 등) 블록이 있음
- `start_record.py`: `register_third_party_plugins()` 호출 + 웹 프리뷰 탭 후킹
- `grpc_wrapper.py`: import alias가 `_types`/`_il`/`_os` (이름 충돌 회피용으로 보임)

즉 **공통부는 "더미 패키지 등록 + `_config_imports` 적용 + groot 더미 등록"까지**이고,
그 뒤는 파일마다 다르다.

## 해결안

`wrapper/lerobot_bootstrap.py` 신규:

```python
"""lerobot.policies.__init__ 우회 (Python 3.13 groot 호환).

다른 lerobot import보다 먼저 import해야 한다:
    import lerobot_bootstrap  # noqa: F401  (side-effect import)
"""
```

- 모듈 최상단에서 side-effect로 부트스트랩 수행 (import 순서가 중요하므로 함수 호출보다
  side-effect import가 기존 동작에 가깝다)
- groot config 직접 로드처럼 일부만 필요한 것은 `load_groot_config()` 같은 선택 함수로 노출

3개 파일은 기존 블록을 지우고 `import lerobot_bootstrap  # noqa: F401` 한 줄로 대체.

### 주의

- wrapper는 백엔드와 **다른 파이썬**에서 실행된다 (`settings.local_python` vs
  `settings.grpc_python`, [config.py](../backend/app/core/config.py) 참조). 따라서
  백엔드 패키지를 import하면 안 되고, `wrapper/` 안에 두어야 한다.
- 실행 방식이 `python /경로/wrapper/xxx.py`이므로 `sys.path[0]`가 wrapper 디렉터리다 →
  `import lerobot_bootstrap`이 그대로 동작한다.
  ([parking_controller.py:22](../wrapper/parking_controller.py#L22)의 `from arm_controller import ...`가
  같은 방식으로 이미 동작 중이다.)
- import 순서에 민감하다. 부트스트랩이 다른 lerobot import보다 먼저 실행되는지 반드시 확인.

## 검증

동작 변화가 없어야 한다. 세 경로를 각각 한 번씩 실제로 돌린다:

- 레코딩 시작 (`start_record.py`)
- 정책 서버 시작 (`start_policy_server.py`)
- gRPC 추론 시작 (`grpc_wrapper.py`)

각각 기동 로그에 정책 import 관련 예외가 없는지 확인.

## 상태

☐ 미착수
