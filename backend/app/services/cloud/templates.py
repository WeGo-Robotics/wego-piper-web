"""학습 템플릿 사양 — **계정마다 자기 것을 만든다.**

## ⚠ 템플릿은 계정 밖으로 안 보인다

Vast 템플릿은 만든 계정의 것이고, 조회도 `search templates private=true` 다. 그래서 다른
계정으로 들어가면 `piper-train full cu126` 이 **아예 안 보인다**(사용자 보고 2026-10-07).

길이 셋이었다:

| | 왜 안 골랐나 |
|---|---|
| 템플릿을 공개로 (`--public`) | 조회가 `private=true` 라 남의 화면에는 여전히 안 뜬다. 그리고 공개로 두면 우리가 고칠 때 남의 작업에 영향이 간다 — `update template` 은 **전체 치환**이고 `hash_id` 가 바뀐다 |
| 템플릿을 안 쓴다 (`create instance --image …`) | 된다(CLI 확인). 다만 `--onstart` 는 **파일명**이라 스크립트를 올리는 단계가 늘고, 템플릿이 들고 있던 검색 필터를 코드가 대신 들어야 한다. 중복된 진실을 없애는 길이라 **장기적으로는 이쪽이 맞다** |
| **계정마다 만든다** | 조회 코드가 그대로 맞는 말이 된다. 각자 소유라 고치고 지우는 것도 자유롭다 ← 이걸 골랐다 |

## ⚠ 중복 판정은 **계정을 넘는다** — 내용이 같으면 남의 것에 붙는다

"계정마다 만든다" 는 Vast 가 계정별로 따로 만들어 줄 때만 맞는 말이었다. 실측(2026-10-10):
같은 내용의 `create template` 은 새로 만들지 않고 **먼저 만든 계정의 템플릿**을 가리킨다.

```
success: false
msg: "Existing Template Found: 754853. User relationship added."
template: {"creator_id": 711347, "id": 754853, "hash_id": "a54d…"}   # ← 내 계정이 아니다
```

- 내 계정 id(698077)가 아니라 **다른 계정(711347)** 것이었다. 그러면 `search templates
  private=true` 는 "내 템플릿만" 이라 그것을 안 돌려주고, 화면은 계속 "템플릿 없음" 이다
- CLI 는 `success: false` 라 `msg` 만 찍고 **`hash_id` 를 버린다**
- 지워도 소용없다 — 다시 만들면 같은 곳에 또 붙는다
- 붙어 써도 안 된다 — 그 계정이 수정·삭제하면 우리 hash 가 조용히 낡는다(`update template`
  은 전체 치환이고 `hash_id` 가 바뀐다). 위에서 공개 템플릿을 피한 이유가 비공개에서도 생긴다

판정은 `desc` 까지 본다(실측: `desc` 한 줄에 계정 id 를 붙이자 `Template Created
Successfully` 였다). 그래서 **`desc` 에 계정 id 를 넣는다** — 계정마다 달라 남의 것에 안
붙고, 같은 계정이 다시 눌러도 같은 문자열이라 자기 것에 맞는다. 이름·이미지·태그는 그대로다.

## ⚠ 이미지는 이미 공개다

막는 것은 템플릿뿐이다 — `ghcr.io/wego-robotics/piper-train` 은 익명 pull 이 되는 것을
확인했다(2026-10-07, `full-cu126`·`slim-cu126` 둘 다 HTTP 200). 그래서 템플릿만 만들면
다른 계정에서도 그대로 돈다.

## ⚠ 태그는 **굴러가는 것**을 쓴다

살아 있는 두 템플릿은 날짜 태그(`full-0.5.0-cu126-20260918`)를 들고 있다. 여기 그걸 박으면
이미지를 다시 구울 때마다 **코드가 조용히 낡는다** — 새 계정만 옛 이미지를 받게 된다.
`build-train.sh` 가 구울 때마다 `full-cu126`·`slim-cu126` 을 같이 밀어 주므로 그쪽을 쓴다.

## ⚠ 검색 필터는 문자열 하나로 들어간다

`--search_params` 가 저장된 `extra_filters` 를 **글자 하나까지 재현**하는 것을 버리는
템플릿으로 두 번 예행해 확인했다. 손으로 JSON 을 만들지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

#: 이미지는 하나, 태그로 갈린다.
IMAGE = "ghcr.io/wego-robotics/piper-train"

#: 부팅 스크립트 — 이미지 안에 있다(`deploy/train/Dockerfile`).
ONSTART = "/opt/piper/bootstrap.sh"

#: 이미지 전개까지 들어가는 크기. 기본값으로 뜨면 pull 이 실패한다.
DISK_GB = 40

#: 오퍼 검색 필터. 살아 있는 템플릿의 `extra_filters` 와 같은 결과를 낸다(실측).
#:
#: ⚠ `cuda_max_good>=12.4` 는 **호스트 드라이버**가 보고하는 최대 CUDA 다 — 이미지가
#:   cu126 이라 그 아래로는 못 돈다.
SEARCH_PARAMS = (
    'verified=true external=false rentable=true gpu_name="RTX 4090" '
    "cuda_max_good>=12.4 reliability>0.98 inet_down>200"
)


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    tag: str
    desc: str


#: 만들 템플릿들. **이름이 곧 열쇠다** — 이미 있는지 이름으로 본다.
SPECS: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        name="piper-train full cu126",
        tag="full-cu126",
        desc="LeRobot 0.5.0 + act_aux · 스택 포함, 부팅 즉시 학습",
    ),
    TemplateSpec(
        name="piper-train slim cu126",
        tag="slim-cu126",
        desc="LeRobot 0.5.0 + act_aux · 부팅 때 bootstrap.sh 가 스택 설치",
    ),
)


def missing(existing: list) -> list[TemplateSpec]:
    """이 계정에 없는 것만. ⚠ **있는 것을 다시 만들지 않는다** — 같은 이름이 둘이면
    사람이 어느 것을 고를지 알 수 없고, `hash_id` 도 갈린다."""
    have = {getattr(t, "name", "") for t in existing}
    return [s for s in SPECS if s.name not in have]


def desc_for(spec: TemplateSpec, account_id: int) -> str:
    """계정마다 달라지는 설명. ⚠ **이게 중복 판정을 벗어나는 열쇠다**(머리말).

    같은 계정은 늘 같은 문자열이라 다시 눌러도 자기 템플릿에 맞는다. 계정 id 를 모르면
    만들지 않는다 — 표식 없이 보내면 남의 템플릿에 붙어 목록에 안 보이는 처음 증상이 된다.
    """
    if not account_id:
        raise ValueError("계정 id 를 모르면 템플릿을 만들 수 없습니다")
    return f"{spec.desc} · 계정 {account_id}"
