#!/usr/bin/env python3
"""이미지 엔코더 특징 추출 (오프라인 진단용).

추론 루프와 완전히 분리된 1회성 프로세스. 이미지 한 장을 정책의 **이미지 엔코더에만**
통과시켜 패치 특징을 .npy 로 저장하고 바로 종료한다. 액션은 계산하지 않는다.

이 분리 덕에 백엔드는 torch/lerobot 을 import 하지 않고 저장된 배열을 numpy 로만
다룰 수 있다(PCA / 코사인 유사도 / k-means). 모델은 요청당 한 번만 GPU를 잡았다 놓는다.

사용:
  python encoder_probe.py --policy-type smolvla --image in.jpg --out /tmp/probe1
  python encoder_probe.py --policy-type act --checkpoint <ckpt> --image-key top \
      --image in.jpg --out /tmp/probe2

출력(--out 디렉터리):
  features.npy  (N, D) float32 — 패치 토큰 특징
  input.jpg     모델이 실제로 보는 이미지(리사이즈/패딩 반영)
  meta.json     격자 크기, 유효 영역, 인코더 가중치 통계 등
meta 는 stdout 으로도 한 줄 JSON 으로 출력한다.
"""

import argparse
import glob
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

from policy_registry import SPECS as _SPECS
from policy_registry import default_tap, probe_taps, tap_keys


def probe_policies() -> list[str]:
    """`encoder_probe: true` 인 정책만. 목록을 두 벌 두지 않는다."""
    return sorted(n for n, d in _SPECS.items()
                  if (d.get("capabilities") or {}).get("encoder_probe"))

VLM_DEFAULT = "HuggingFaceTB/SmolVLM2-500M-Video-Instruct"
# SmolVLA 체크포인트 내부의 비전 타워 / 커넥터 가중치 접두사
VIS_PREFIX = "model.vlm_with_expert.vlm.model.vision_model."
CONN_PREFIX = "model.vlm_with_expert.vlm.model.connector."
# LeRobot 이미지 기본 정규화 통계(ImageNet)
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _load_image(path: str) -> np.ndarray:
    import cv2

    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise SystemExit(f"이미지를 읽을 수 없습니다: {path}")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def _save_jpg(path: Path, rgb: np.ndarray) -> None:
    import cv2

    cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])


def _pick_device(requested: str) -> str:
    import torch

    if requested == "cuda" and not torch.cuda.is_available():
        _log("CUDA 사용 불가 — CPU로 폴백")
        return "cpu"
    return requested


# ── SmolVLA (SigLIP) ──────────────────────────────────────────────────────────


def _encoder_weight_stats(vision_model) -> dict:
    """비전 인코더가 사전학습 가중치인지 랜덤 초기화인지 판별한다.

    HF 기본 초기화는 LayerNorm weight 가 정확히 전부 1.0(std=0)이고 q_proj std≈0.02.
    사전학습 SigLIP 은 post_layernorm mean≈2.60, q_proj std≈0.0298 이다.
    LeRobot 의 `load_vlm_weights=False` 로 처음부터 학습하면 VLM 전체가 랜덤 초기화되고,
    `freeze_vision_encoder=True` 와 겹치면 학습조차 되지 않으므로 여기서 잡아준다.
    """
    try:
        pl = vision_model.post_layernorm.weight.detach().float()
        q = vision_model.encoder.layers[0].self_attn.q_proj.weight.detach().float()
        ln_mean, ln_std = float(pl.mean()), float(pl.std())
        return {
            "post_layernorm_mean": round(ln_mean, 5),
            "post_layernorm_std": round(ln_std, 6),
            "q_proj_std": round(float(q.std()), 5),
            "random_init": bool(ln_std < 1e-6 and abs(ln_mean - 1.0) < 1e-6),
        }
    except Exception as exc:  # 구조가 다른 백본이면 판정 생략
        _log(f"인코더 가중치 통계 실패: {exc}")
        return {}


def _load_ckpt_vision(checkpoint: str, vision_model, connector) -> bool:
    """체크포인트의 model.safetensors 에서 비전 타워/커넥터 가중치만 덮어쓴다.

    500M VLM + 액션 엑스퍼트를 통째로 로드하지 않기 위함. 키 개수가 맞지 않으면
    덮어쓰지 않고 False 를 반환해 베이스 가중치를 그대로 쓴다.
    """
    import torch
    from safetensors.torch import load_file

    files = sorted(glob.glob(str(Path(checkpoint) / "*.safetensors")))
    vis_sd, conn_sd = {}, {}
    for f in files:
        if "normalizer" in Path(f).name or "unnormalizer" in Path(f).name:
            continue
        for k, v in load_file(f).items():
            if k.startswith(VIS_PREFIX):
                vis_sd[k[len(VIS_PREFIX):]] = v
            elif k.startswith(CONN_PREFIX):
                conn_sd[k[len(CONN_PREFIX):]] = v
    if not vis_sd:
        _log("체크포인트에서 비전 가중치를 찾지 못했습니다 — 베이스 가중치 사용")
        return False
    expected = len(vision_model.state_dict())
    if len(vis_sd) != expected:
        _log(f"비전 키 개수 불일치 ({len(vis_sd)} != {expected}) — 베이스 가중치 사용")
        return False
    vision_model.load_state_dict({k: v.to(torch.float32) for k, v in vis_sd.items()})
    if conn_sd:
        connector.load_state_dict({k: v.to(torch.float32) for k, v in conn_sd.items()})
    return True


def run_smolvla(args, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict]:
    import torch
    from transformers import AutoModelForImageTextToText

    from lerobot.policies.smolvla.modeling_smolvla import resize_with_pad

    size = (512, 512)  # (width, height)
    vlm_name = VLM_DEFAULT
    if args.checkpoint:
        cfg = json.loads((Path(args.checkpoint) / "config.json").read_text())
        r = cfg.get("resize_imgs_with_padding") or size
        size = (int(r[0]), int(r[1]))
        vlm_name = cfg.get("vlm_model_name") or VLM_DEFAULT

    _log(f"VLM 로드: {vlm_name}")
    model = AutoModelForImageTextToText.from_pretrained(vlm_name, dtype=torch.float32)
    inner = getattr(model, "model", model)
    vision_model, connector = inner.vision_model, inner.connector
    try:  # 텍스트 모델은 쓰지 않으므로 즉시 해제
        del inner.text_model
    except Exception:
        pass

    encoder_source = "base"
    if args.checkpoint and _load_ckpt_vision(args.checkpoint, vision_model, connector):
        encoder_source = "checkpoint"
        _log("체크포인트 비전 가중치 적용")

    device = _pick_device(args.device)
    vision_model.to(device).eval()
    connector.to(device).eval()

    h, w = rgb.shape[:2]
    x = torch.from_numpy(rgb.copy()).permute(2, 0, 1).float().div(255.0).unsqueeze(0)
    # SmolVLAPolicy.prepare_images 와 동일: 종횡비 유지 리사이즈 + 위/왼쪽 패딩 + [-1,1]
    padded = resize_with_pad(x, size[0], size[1], pad_value=0)
    model_view = (padded[0].permute(1, 2, 0).numpy() * 255).astype(np.uint8)

    t0 = time.monotonic()
    with torch.inference_mode():
        hidden = vision_model(pixel_values=(padded * 2.0 - 1.0).to(device)).last_hidden_state
        if args.tap == "connector":
            hidden = connector(hidden)
    feat = hidden[0].float().cpu().numpy()
    elapsed = (time.monotonic() - t0) * 1000

    n_patches = feat.shape[0]
    side = int(round(math.sqrt(n_patches)))
    patch_px = size[0] / side  # 모델 입력(512) 기준 패치 한 변

    # 패딩은 위/왼쪽에만 들어간다(resize_with_pad). 패딩에 조금이라도 걸치는
    # 패치는 유효 영역에서 제외해야 히트맵이 원본과 어긋나지 않는다.
    ratio = max(w / size[0], h / size[1])
    pad_w = max(0, size[0] - int(w / ratio))
    pad_h = max(0, size[1] - int(h / ratio))

    meta = {
        "grid_h": side,
        "grid_w": side,
        "model_w": size[0],
        "model_h": size[1],
        "valid_row0": int(math.ceil(pad_h / patch_px)),
        "valid_col0": int(math.ceil(pad_w / patch_px)),
        "patch_px_model": round(patch_px, 2),
        "patch_px_orig_x": round(patch_px * ratio, 2),
        "patch_px_orig_y": round(patch_px * ratio, 2),
        "encoder_source": encoder_source,
        "encoder_stats": _encoder_weight_stats(vision_model),
        "elapsed_ms": round(elapsed, 1),
        "device": device,
        "image_keys": [],
    }
    return feat, model_view, meta


# ── ACT (ResNet) ──────────────────────────────────────────────────────────────


def _act_image_stats(checkpoint: str, full_key: str):
    """체크포인트 preprocessor 의 정규화 통계에서 해당 카메라의 mean/std 를 읽는다."""
    import torch
    from safetensors.torch import load_file

    pattern = str(Path(checkpoint) / "policy_preprocessor_step_*_normalizer_processor.safetensors")
    for f in sorted(glob.glob(pattern)):
        data = load_file(f)
        if f"{full_key}.mean" in data and f"{full_key}.std" in data:
            return data[f"{full_key}.mean"].float(), data[f"{full_key}.std"].float()
    _log(f"{full_key} 통계를 찾지 못해 ImageNet 기본값 사용")
    return (
        torch.tensor(IMAGENET_MEAN).view(3, 1, 1),
        torch.tensor(IMAGENET_STD).view(3, 1, 1),
    )


def _act_base_backbone():
    """ACT 의 **학습 전 백본**. LeRobot 이 만드는 것과 같은 방식이어야 한다.

    다르게 만들면 비교가 성립하지 않는다 — `FrozenBatchNorm2d` 하나만 빠져도
    통계가 달라진다. `modeling_act.ACT.__init__` 의 구성을 그대로 따른다.
    """
    import torchvision
    from torchvision.models._utils import IntermediateLayerGetter

    from lerobot.policies.act.configuration_act import ACTConfig
    from lerobot.policies.act.modeling_act import FrozenBatchNorm2d

    c = ACTConfig()
    _log(f"ACT 베이스 백본: {c.vision_backbone} / {c.pretrained_backbone_weights}")
    model = getattr(torchvision.models, c.vision_backbone)(
        replace_stride_with_dilation=[False, False, c.replace_final_stride_with_dilation],
        weights=c.pretrained_backbone_weights,
        norm_layer=FrozenBatchNorm2d,
    )
    model.eval()
    backbone = IntermediateLayerGetter(model, return_layers={"layer4": "feature_map"})
    return backbone, {
        "vision_backbone": c.vision_backbone,
        "pretrained_backbone_weights": c.pretrained_backbone_weights,
    }


def run_act(args, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict]:
    import torch

    from lerobot.policies.act.modeling_act import ACTPolicy

    # ⚠ **체크포인트 없이도 돌아야 한다.** ACT 의 백본은 무작위에서 시작하지 않고
    # ImageNet 사전학습 ResNet-18 로 초기화된다(`ACTConfig` 기본값). 그 시작점을
    # 못 보면 "학습이 엔코더를 좋게 만들었나"를 판단할 기준이 없다 —
    # 비교 대상 없이 체크포인트만 들여다보는 셈이다.
    if args.checkpoint:
        _log(f"ACT 로드: {args.checkpoint}")
        policy = ACTPolicy.from_pretrained(args.checkpoint)
        policy.eval()
        backbone = policy.model.backbone
        image_keys = [
            k.split("observation.images.", 1)[1]
            for k in policy.config.input_features
            if k.startswith("observation.images.")
        ]
        key = args.image_key or (image_keys[0] if image_keys else "")
        if not key:
            raise SystemExit("모델에 카메라 입력이 없습니다")
        cfg = json.loads((Path(args.checkpoint) / "config.json").read_text())
        mean, std = _act_image_stats(args.checkpoint, f"observation.images.{key}")
        encoder_source = "checkpoint"
    else:
        backbone, cfg = _act_base_backbone()
        # 베이스에는 모델이 없으니 카메라 키 목록도 없다 — **빈 목록이 사실이다.**
        # UI 의 "카메라 키" 드롭다운은 그때 자동만 남는다.
        image_keys, key = [], args.image_key or ""
        # 데이터셋 통계가 없다 — ImageNet 표준값을 쓴다. 체크포인트 쪽과 정규화가
        # 달라지지만, **그게 사실이다**: 학습 전에는 데이터셋 통계가 존재하지 않는다.
        import torch as _t

        mean = _t.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
        std = _t.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
        encoder_source = "base"

    device = _pick_device(args.device)
    backbone.to(device)

    h, w = rgb.shape[:2]
    x = torch.from_numpy(rgb.copy()).permute(2, 0, 1).float().div(255.0).unsqueeze(0)
    # ACT 는 리사이즈 없이 원본 해상도를 그대로 백본에 넣는다(VISUAL=MEAN_STD 정규화만).
    x = (x - mean) / std

    t0 = time.monotonic()
    with torch.inference_mode():
        fmap = backbone(x.to(device))["feature_map"]  # (1, C, h', w')
    elapsed = (time.monotonic() - t0) * 1000

    _, c, gh, gw = fmap.shape
    # ACT 본체와 동일한 순서: (h w) 로 평탄화 → index = row * gw + col
    feat = fmap[0].reshape(c, gh * gw).T.float().cpu().numpy()

    meta = {
        "grid_h": gh,
        "grid_w": gw,
        "model_w": w,
        "model_h": h,
        "valid_row0": 0,
        "valid_col0": 0,
        "patch_px_model": round(w / gw, 2),
        "patch_px_orig_x": round(w / gw, 2),
        "patch_px_orig_y": round(h / gh, 2),
        "encoder_source": encoder_source,
        "encoder_stats": {
            "vision_backbone": cfg.get("vision_backbone"),
            "pretrained_backbone_weights": cfg.get("pretrained_backbone_weights"),
            "random_init": cfg.get("pretrained_backbone_weights") is None,
        },
        "elapsed_ms": round(elapsed, 1),
        "device": device,
        "image_keys": image_keys,
        "image_key": key,
    }
    return feat, rgb, meta


def main() -> None:
    p = argparse.ArgumentParser(description="이미지 엔코더 특징 추출")
    # ⚠ 선택지를 손으로 안 적는다 — 프로브 되는 정책은 `policies/*.yaml` 이 정한다
    p.add_argument("--policy-type", required=True, choices=probe_policies())
    p.add_argument("--checkpoint", default="", help="체크포인트 경로 (ACT 필수)")
    p.add_argument("--image", required=True)
    p.add_argument("--image-key", default="", help="ACT 카메라 키 (정규화 통계 선택)")
    # 기본값을 여기서 못 정한다 — 정책마다 다르고 `--policy-type` 을 파싱한 뒤에야
    # 안다. 그래서 빈 값으로 받고 아래에서 스펙의 `default: true` tap 으로 채운다.
    p.add_argument("--tap", default="", choices=[""] + tap_keys(),
                   help="추출 지점. 비우면 정책 스펙의 기본 tap (policies/<type>.yaml)")
    p.add_argument("--out", required=True)
    p.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    args = p.parse_args()
    # ⚠ 정책 이름으로 갈라 적지 않는다. 예전에는 `args.tap if policy_type ==
    # "smolvla" else "backbone"` 이라 정책이 늘 때마다 이 줄도 늘었다.
    if args.tap not in {t["key"] for t in probe_taps(args.policy_type)}:
        args.tap = default_tap(args.policy_type)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rgb = _load_image(args.image)
    orig_h, orig_w = rgb.shape[:2]

    started = time.monotonic()
    # ⚠ 이 갈림은 **스펙으로 안 옮긴다.** 아키텍처마다 특징을 뽑는 코드가 다르고
    # 그건 데이터가 아니라 로직이다 (feature/policy-ui-spec.md: "표현이 안 되면
    # 그건 진짜 로직이라는 신호"). 옮길 수 있었던 것 — 선택지·기본 tap·라벨 —
    # 은 이미 `policies/*.yaml` 로 갔다.
    if args.policy_type == "smolvla":
        feat, view, meta = run_smolvla(args, rgb)
    else:
        feat, view, meta = run_act(args, rgb)

    meta.update({
        "policy_type": args.policy_type,
        "tap": args.tap,
        "checkpoint": args.checkpoint,
        "orig_w": orig_w,
        "orig_h": orig_h,
        "n_patches": int(feat.shape[0]),
        "dim": int(feat.shape[1]),
        "total_ms": round((time.monotonic() - started) * 1000, 1),
    })
    meta.setdefault("image_key", args.image_key)

    np.save(out / "features.npy", feat.astype(np.float32))
    _save_jpg(out / "input.jpg", view)
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta), flush=True)


if __name__ == "__main__":
    main()
