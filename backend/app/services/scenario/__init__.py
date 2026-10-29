"""시나리오 설계기 — 템플릿으로 수집 계획을 짠다 (feature/scenario-designer.md).

- `board`     작업판: 격자·칸 이름·구역·로봇 자리
- `balance`   공통 균형기: 적게 쓴 쪽부터, 동률은 시드 난수
- `templates` 템플릿 YAML 로더와 값 검증
- `plan`      `generate()` — 템플릿 생성기를 부르고 균형 요약을 붙인다
- `sort`      분류 템플릿 생성기
- `printing`  작업판 PDF (A4·A3 분할)

전부 순수 함수다. 장치·버스·파일을 건드리지 않으므로 같은 입력이면 같은 계획이 나오고,
로봇 없이 테스트한다.
"""


class ScenarioError(ValueError):
    """잘못된 설계 값. 라우터가 400 으로 바꾼다 — 문장이 그대로 화면에 뜬다."""
