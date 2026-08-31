# 제3자 구성요소 고지

이 저장소가 배포하는 컨테이너 이미지에 포함된 제3자 패키지 목록이다.

⚠ **이 문서는 손으로 쓴 것이 아니다.** 실제 이미지에서 설치된 배포판의 메타데이터를
읽어 만들었다(총 **160개**). 의존성이 바뀌면 다시 뽑아야 한다:

```bash
docker run --rm --entrypoint python piper-web-backend:latest -c '
import importlib.metadata as md
for d in md.distributions():
    m = d.metadata
    print(m["Name"], m["Version"], m.get("License-Expression") or m.get("License"))'
```

## 요약

| 구분 | 개수 | 우리 배포에 지우는 의무 |
|---|---|---|
| 허용형 (MIT · BSD · Apache-2.0 · ISC · PSF 등) | 146 | 저작권·라이선스 고지 유지 |
| LGPL | 4 | 소스 제공(또는 제공 의사) · **교체 가능성 보장** |
| MPL-2.0 | 2 | 해당 파일의 소스 공개 (파일 단위 카피레프트) |
| GPL | 1 | ⚠ 아래 별도 항목 참조 |
| NVIDIA 독점 | 7 | CUDA EULA 의 재배포 조건 준수 |
| AGPL | **0** | — |

**AGPL 은 0개다.** 예전에는 `ultralytics`(AGPL-3.0)가 검출기로 들어 있었고, 그것과
결합한 배포물 전체가 AGPL 이 됐다. RT-DETR(`transformers` 구현 + `PekingU/*`
가중치, 둘 다 Apache-2.0)로 바꾸면서 없앴다.

## ⚠ GPL — 확인이 필요한 항목

| `pyyaml-include` | 1.4.1 | GNU General Public License v3 or later (GPLv3+) |

`draccus`(MIT, LeRobot 설정 파서)의 `parsers/yaml_loader.py` 가 `yamlinclude` 를
import 한다. **우리 실행 경로에서는 로드되지 않는다** — 확인한 결과:

| import | `yamlinclude` 로드 |
|---|---|
| `draccus` | 아니오 |
| `lerobot` | 아니오 |
| `lerobot.configs.parser` (우리가 쓰는 경로) | 아니오 |
| `draccus.parsers.yaml_loader` 직접 | 예 |

우리는 CLI 인자로 설정을 넘기고 YAML 파일 경로를 쓰지 않는다. 그래도 **이미지에는
동봉된다.** 단순 동봉(GPL §5 mere aggregation)인지 결합저작물인지는 법률 판단
사안이다 — 공개 배포 전에 검토가 필요하다.

## LGPL

| `num2words` | 0.5.14 | GNU Library or Lesser General Public License (LGPL) |
| `pynput` | 1.8.2 | GNU Lesser General Public License v3 (LGPLv3) |
| `python-can` | 4.6.1 | LGPL-3.0-only |
| `python-xlib` | 0.33 | GNU Lesser General Public License v2 or later (LGPLv2+) |

LGPL 은 독점·제한 라이선스 소프트웨어에서 쓰는 것을 허용한다. 조건은 셋이다.

1. 사용자가 그 라이브러리를 **교체할 수 있어야** 한다 — 파이썬 패키지는 런타임
   import 라 자연히 충족된다(`pip install` 로 갈아끼울 수 있다)
2. 그 라이브러리의 **소스를 제공**하거나 제공 의사를 밝힐 것
3. 라이선스 고지 포함

⚠ **우리는 이 넷 중 어느 것도 고쳐 쓰지 않는다.** 고치면 그 수정분은 LGPL 로
공개해야 한다. `python-can` 은 `piper_robot` 이 직접 쓰지만 pip 의존일 뿐이다.

## MPL-2.0

| `certifi` | 2026.7.22 | Mozilla Public License 2.0 (MPL 2.0) |
| `tqdm` | 4.70.0 | MPL-2.0 AND MIT |

파일 단위 카피레프트다. 해당 파일을 고치지 않는 한 우리 코드에 영향이 없다.

## NVIDIA 독점

| `cuda-bindings` | 13.3.1 | LicenseRef-NVIDIA-SOFTWARE-LICENSE |
| `nvidia-cublas` | 13.1.0.3 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cuda-runtime` | 13.0.96 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cudnn-cu13` | 9.19.0.56 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-cusparselt-cu13` | 0.8.0 | NVIDIA Proprietary Software |
| `nvidia-nccl-cu13` | 2.28.9 | LicenseRef-NVIDIA-Proprietary |
| `nvidia-nvshmem-cu13` | 3.4.5 | LicenseRef-NVIDIA-Proprietary |

`torch`(cu130)가 끌고 오며 이미지에서 약 3.9GB 를 차지한다. 재배포는 CUDA EULA 를
따른다 — GPU 추론을 하는 한 뺄 수 없다.

## 허용형 (146개)

| `accelerate` | 1.14.0 | Apache Software License |
| `aiohappyeyeballs` | 2.7.1 | Python Software Foundation License |
| `aiohttp` | 3.14.3 | Apache-2.0 AND MIT |
| `aiosignal` | 1.4.0 | Apache Software License |
| `annotated-doc` | 0.0.5 | MIT |
| `annotated-types` | 0.8.0 | MIT |
| `anthropic` | 1.2.0 | MIT License |
| `anyio` | 4.14.2 | MIT |
| `attrs` | 26.1.0 | MIT |
| `av` | 15.1.0 | BSD-3-Clause |
| `charset-normalizer` | 3.5.1 | MIT |
| `click` | 8.5.0 | BSD-3-Clause |
| `cloudpickle` | 3.1.2 | BSD License |
| `cmake` | 4.1.3 | Apache Software License |
| `cuda-pathfinder` | 1.8.0 | Apache-2.0 |
| `cuda-toolkit` | 13.0.2 | ? |
| `datasets` | 4.8.5 | Apache Software License |
| `deepdiff` | 8.6.2 | MIT License |
| `diffusers` | 0.35.2 | Apache Software License |
| `dill` | 0.4.1 | BSD License |
| `docopt` | 0.6.2 | MIT License |
| `docstring_parser` | 0.18.0 | MIT License |
| `draccus` | 0.10.0 | MIT License |
| `einops` | 0.8.2 | MIT License |
| `evdev` | 2.0.0 | BSD-3-Clause |
| `Farama-Notifications` | 0.0.6 | MIT License |
| `fastapi` | 0.141.1 | MIT |
| `filelock` | 3.32.4 | MIT |
| `frozenlist` | 1.8.0 | Apache-2.0 |
| `fsspec` | 2026.2.0 | BSD-3-Clause |
| `gitdb` | 4.0.12 | BSD License |
| `GitPython` | 3.1.60 | BSD-3-Clause |
| `grpcio` | 1.83.0 | Apache-2.0 |
| `gymnasium` | 1.3.0 | MIT License |
| `h11` | 0.16.0 | MIT License |
| `hf-xet` | 1.6.0 | Apache-2.0 |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `httpcore2` | 2.12.0 | BSD-3-Clause |
| `httptools` | 0.8.0 | MIT |
| `httpx` | 0.28.1 | BSD License |
| `httpx2` | 2.12.0 | BSD-3-Clause |
| `huggingface_hub` | 1.29.0 | Apache Software License |
| `idna` | 3.19 | BSD-3-Clause |
| `ImageIO` | 2.37.4 | BSD-2-Clause |
| `imageio-ffmpeg` | 0.6.0 | BSD License |
| `importlib_metadata` | 9.0.0 | Apache-2.0 |
| `Jinja2` | 3.1.6 | BSD License |
| `jiter` | 0.16.0 | MIT |
| `jsonlines` | 4.0.0 | BSD License |
| `lerobot` | 0.5.0 | Apache Software License |
| `lerobot_camera_pipershm` | 0.1.0 | ? |
| `lerobot_policy_act_aux` | 0.1.0 | ? |
| `lerobot_robot_piper` | 0.1.0 | Apache-2.0 |
| `lerobot_robot_pipershm` | 0.1.0 | ? |
| `lightning-utilities` | 0.15.3 | Apache-2.0 |
| `markdown-it-py` | 4.2.0 | MIT License |
| `MarkupSafe` | 3.0.3 | BSD-3-Clause |
| `mdurl` | 0.1.2 | MIT License |
| `mergedeep` | 1.3.4 | MIT License |
| `mpmath` | 1.3.0 | BSD License |
| `multidict` | 6.7.1 | Apache License 2.0 |
| `multiprocess` | 0.70.19 | BSD License |
| `mypy_extensions` | 1.1.0 | MIT |
| `narwhals` | 2.25.0 | MIT |
| `networkx` | 3.6.1 | BSD-3-Clause |
| `numpy` | 2.2.6 | BSD License |
| `nvidia-cuda-cupti` | 13.0.85 | Other/Proprietary License |
| `nvidia-cuda-nvrtc` | 13.0.88 | Other/Proprietary License |
| `nvidia-cufft` | 12.0.0.61 | Other/Proprietary License |
| `nvidia-cufile` | 1.15.1.6 | Other/Proprietary License |
| `nvidia-curand` | 10.4.0.35 | Other/Proprietary License |
| `nvidia-cusolver` | 12.0.4.66 | Other/Proprietary License |
| `nvidia-cusparse` | 12.6.3.3 | Other/Proprietary License |
| `nvidia-nvjitlink` | 13.0.88 | Other/Proprietary License |
| `nvidia-nvtx` | 13.0.85 | Other/Proprietary License |
| `opencv-python-headless` | 4.12.0.88 | Apache Software License |
| `orderly-set` | 5.5.0 | MIT License |
| `packaging` | 25.0 | Apache Software License |
| `pandas` | 3.0.5 | BSD License |
| `pillow` | 12.3.0 | MIT-CMU |
| `pip` | 26.2.1 | MIT |
| `piper-robot` | 0.1.0 | ? |
| `piper-sdk` | 0.6.1 | MIT License |
| `piper-shm` | 0.1.0 | ? |
| `piper-web-backend` | 0.1.0 | ? |
| `piper-web-backend` | 0.1.0 | ? |
| `piper_bus` | 0.1.0 | ? |
| `piper_phase` | 0.1.0 | ? |
| `platformdirs` | 4.11.5 | MIT |
| `plotly` | 7.0.0 | MIT |
| `propcache` | 0.5.2 | Apache Software License |
| `protobuf` | 6.33.6 | 3-Clause BSD License |
| `psutil` | 7.2.2 | BSD-3-Clause |
| `pyarrow` | 25.0.1 | Apache-2.0 |
| `pycocotools` | 2.0.11 | FreeBSD |
| `pydantic` | 2.13.4 | MIT |
| `pydantic-settings` | 2.15.0 | MIT |
| `pydantic_core` | 2.46.4 | MIT |
| `Pygments` | 2.21.0 | BSD-2-Clause |
| `pyrealsense2` | 2.58.4.10922 | Apache Software License |
| `pyserial` | 3.5 | BSD License |
| `python-dateutil` | 2.9.0.post0 | BSD License |
| `python-dotenv` | 1.2.3 | BSD-3-Clause |
| `PyYAML` | 6.0.3 | MIT License |
| `pyzmq` | 27.2.0 | BSD-3-Clause |
| `redis` | 8.1.0 | MIT |
| `regex` | 2026.7.19 | Apache-2.0 AND CNRI-Python |
| `requests` | 2.34.2 | Apache Software License |
| `rerun-sdk` | 0.26.2 | MIT OR Apache-2.0 |
| `rich` | 15.0.0 | MIT License |
| `safetensors` | 0.8.0 | Apache Software License |
| `scipy` | 1.18.1 | BSD License |
| `sentry-sdk` | 2.68.1 | MIT |
| `setuptools` | 80.10.2 | MIT |
| `shellingham` | 1.5.4 | ISC License (ISCL) |
| `six` | 1.17.0 | MIT License |
| `smmap` | 5.0.3 | BSD License |
| `sniffio` | 1.3.1 | MIT License |
| `starlette` | 1.6.0 | BSD-3-Clause |
| `sympy` | 1.14.0 | BSD License |
| `termcolor` | 3.3.0 | MIT |
| `tokenizers` | 0.22.2 | Apache Software License |
| `toml` | 0.10.2 | MIT License |
| `torch` | 2.11.0+cu130 | BSD-3-Clause |
| `torchcodec` | 0.11.0+cu130 | BSD 3-Clause License |
| `torchmetrics` | 1.9.0 | Apache Software License |
| `torchvision` | 0.26.0+cu130 | BSD |
| `transformers` | 5.3.0 | Apache 2.0 License |
| `triton` | 3.6.0 | MIT License |
| `truststore` | 0.10.4 | MIT |
| `typer` | 0.27.1 | MIT |
| `typing-inspect` | 0.9.0 | MIT License |
| `typing-inspection` | 0.4.4 | MIT |
| `typing_extensions` | 4.16.0 | PSF-2.0 |
| `urllib3` | 2.7.0 | MIT |
| `uvicorn` | 0.52.4 | BSD-3-Clause |
| `uvloop` | 0.22.1 | Apache Software License |
| `wandb` | 0.24.2 | MIT License |
| `watchfiles` | 1.2.0 | MIT License |
| `websockets` | 17.1 | BSD-3-Clause |
| `wego_piper` | 0.0.2 | Apache-2.0 |
| `wheel` | 0.48.0 | MIT |
| `wrapt` | 1.17.3 | BSD License |
| `xxhash` | 4.0.1 | BSD-2-Clause |
| `yarl` | 1.24.5 | Apache-2.0 |
| `zipp` | 4.1.0 | MIT |
