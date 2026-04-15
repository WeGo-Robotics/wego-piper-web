"""
SmolVLA 추론 시각화 hook.
policy server에서 모델 로드 후 register_viz_hooks(policy)를 호출하면
추론마다 /tmp/piper_viz_*.jpg에 시각화 이미지를 저장한다.

시각화 종류:
1. 전처리된 입력 이미지 (모델이 보는 512x512)
2. Vision Encoder 특징맵 (PCA → RGB)
3. Attention 히트맵 (입력 이미지 위에 오버레이)
"""

import logging
import os
import tempfile
import threading
import time
from functools import partial

import cv2
import numpy as np
import torch

logger = logging.getLogger(__name__)

VIZ_DIR = "/dev/shm"  # tmpfs (RAM disk) — SSD에 쓰지 않음
_viz_lock = threading.Lock()
_last_viz_time = 0.0
VIZ_INTERVAL = 0.5  # 최소 0.5초 간격

# 캡처 저장소
_captured = {
    "preprocessed_images": None,  # list of (B, 3, H, W) tensors
    "vision_features_list": [],   # list of (B, num_patches, dim) tensors — 카메라별
    "attention_weights": None,    # (B, num_heads, seq, seq) tensor
    "image_token_range": None,    # (start, end) — attention에서 이미지 토큰 위치
    "_capture_prefix_attn": False,  # prefix 인코딩 중에만 True
}


def _save_jpg(img_np: np.ndarray, name: str) -> None:
    """numpy BGR 이미지를 atomic하게 /tmp에 저장."""
    path = os.path.join(VIZ_DIR, name)
    fd, tmp = tempfile.mkstemp(suffix=".jpg", dir=VIZ_DIR)
    try:
        cv2.imwrite(tmp, img_np, [cv2.IMWRITE_JPEG_QUALITY, 85])
        os.replace(tmp, path)
    except Exception:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _tensor_to_bgr(t: torch.Tensor) -> np.ndarray:
    """(3, H, W) 텐서 [-1,1] 또는 [0,1] → BGR uint8."""
    img = t.detach().cpu().float()
    if img.min() < 0:
        img = (img + 1.0) / 2.0  # [-1,1] → [0,1]
    img = img.clamp(0, 1).permute(1, 2, 0).numpy()  # (H, W, 3) RGB
    img = (img * 255).astype(np.uint8)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def _features_to_colormap(features: torch.Tensor, h: int, w: int) -> np.ndarray:
    """(num_patches, dim) → 패치별 활성도 히트맵 (JET colormap).
    각 패치의 L2 norm을 계산하여 어디에 반응하는지 시각화."""
    feat = features.detach().cpu().float().numpy()  # (N, D)
    n_patches = feat.shape[0]
    side = int(np.sqrt(n_patches))
    if side * side != n_patches:
        side = int(np.ceil(np.sqrt(n_patches)))

    # 패치별 L2 norm (활성도)
    norms = np.linalg.norm(feat, axis=1)  # (N,)

    # percentile 정규화 (대비 강화)
    p5, p95 = np.percentile(norms, 5), np.percentile(norms, 95)
    if p95 - p5 > 1e-8:
        norms = np.clip((norms - p5) / (p95 - p5), 0, 1)
    else:
        mn, mx = norms.min(), norms.max()
        norms = (norms - mn) / (mx - mn + 1e-8)

    # spatial reshape + 패딩
    if n_patches < side * side:
        norms = np.pad(norms, (0, side * side - n_patches))
    heatmap = norms[:side * side].reshape(side, side)

    # resize + JET colormap
    heatmap_resized = cv2.resize(heatmap.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    heatmap_color = cv2.applyColorMap((heatmap_resized * 255).astype(np.uint8), cv2.COLORMAP_JET)
    return heatmap_color


def _attention_to_heatmap(attn: torch.Tensor, img_range: tuple, bg_img: np.ndarray) -> np.ndarray:
    """attention weights → 이미지 토큰에 대한 attention → 히트맵 오버레이.
    attn: (num_heads, seq, seq) — softmax 후 확률
    img_range: (start, end) 이미지 토큰 범위
    bg_img: (H, W, 3) BGR 배경 이미지
    """
    h, w = bg_img.shape[:2]
    start, end = img_range
    n_img_tokens = end - start

    attn_np = attn.detach().cpu().float().numpy()  # (heads, seq, seq)
    n_heads, seq_len, _ = attn_np.shape

    # 이미지 이후의 토큰(언어+상태+액션)이 각 이미지 패치에 attend하는 정도
    # query: 이미지 이후 토큰들 (end ~ seq_len)
    # key: 이미지 토큰 (start ~ end)
    if end < seq_len:
        # 이미지 이후 토큰 → 이미지 토큰 attention
        cross_attn = attn_np[:, end:, start:end]  # (heads, non_img, n_img_tokens)
        # 헤드별 max → 헤드 평균 (mean보다 특징적)
        img_attn = cross_attn.max(axis=1).mean(axis=0)  # (n_img_tokens,)
    else:
        # fallback: 전체 self-attention의 이미지 열
        img_attn = attn_np.max(axis=1).mean(axis=0)[start:end]  # (n_img_tokens,)

    # spatial reshape
    side = int(np.ceil(np.sqrt(n_img_tokens)))
    if len(img_attn) < side * side:
        img_attn = np.pad(img_attn, (0, side * side - len(img_attn)))
    heatmap = img_attn[:side * side].reshape(side, side)

    # 정규화 — percentile로 대비 강화
    p5, p95 = np.percentile(heatmap, 5), np.percentile(heatmap, 95)
    if p95 - p5 > 1e-8:
        heatmap = np.clip((heatmap - p5) / (p95 - p5), 0, 1)
    else:
        mn, mx = heatmap.min(), heatmap.max()
        if mx - mn > 1e-8:
            heatmap = (heatmap - mn) / (mx - mn)
        else:
            heatmap = np.zeros_like(heatmap)

    heatmap_resized = cv2.resize(heatmap.astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    heatmap_color = cv2.applyColorMap((heatmap_resized * 255).astype(np.uint8), cv2.COLORMAP_JET)

    # 오버레이
    overlay = cv2.addWeighted(bg_img, 0.4, heatmap_color, 0.6, 0)
    return overlay


# ── Hook 함수들 ──

def _hook_prepare_images(policy, orig_fn, batch, *args, **kwargs):
    """SmolVLAPolicy.prepare_images를 감싸서 vision_features_list 초기화."""
    _captured["vision_features_list"] = []  # 새 추론 시작 시 리스트 초기화
    _captured["attention_weights_list"] = []  # attention 리스트도 초기화
    result = orig_fn(batch, *args, **kwargs)
    # preprocessed_images는 wrapper에서 직접 설정하므로 여기서 덮어쓰지 않음
    return result


def _hook_embed_image(module, input, output):
    """SmolVLMWithExpertModel.embed_image의 forward hook."""
    # input[0] = pixel_values (B, 3, H, W)
    # output = image_hidden_states (B, num_patches, dim)
    _captured["vision_features"] = output.detach().clone()


def _hook_attention(module, input, output):
    """eager_attention_forward는 메서드라 직접 hook 못 건다.
    대신 forward_attn_layer의 마지막 layer에서 캡처."""
    pass  # 아래 별도 구현


def _generate_viz(snap: dict):
    """캡처된 데이터로 시각화 이미지 생성 (백그라운드 스레드)."""
    try:
        imgs = snap.get("preprocessed_images")
        feats_list = snap.get("vision_features_list", [])
        attn = snap.get("attention_weights")

        _dbg = open("/dev/shm/piper_viz_debug.log", "a")
        _dbg.write(f"  [v6] _generate_viz: imgs={len(imgs) if imgs else 0} feats={len(feats_list)} attn={attn is not None}\n")

        if not imgs:
            _dbg.write(f"  [v6] NO imgs, skipping\n")
            _dbg.close()
            return

        for i, img_tensor in enumerate(imgs):
            while img_tensor.dim() > 3:
                img_tensor = img_tensor[0]
            bgr = _tensor_to_bgr(img_tensor)
            _dbg.write(f"  [v6] cam{i}: tensor={img_tensor.shape} bgr={bgr.shape}\n")

            # features
            feat_ok = i < len(feats_list) and feats_list[i] is not None
            if feat_ok:
                feat = feats_list[i]
                _dbg.write(f"  [v6] cam{i} feat: shape={feat.shape} dim={feat.dim()} hash={str(feat.sum().item())[:10]}\n")
                if feat.dim() == 3:
                    h, w = bgr.shape[:2]
                    try:
                        feat_img = _features_to_colormap(feat[0], h, w)
                        _save_jpg(feat_img, f"piper_viz_features_{i}.jpg")
                        _dbg.write(f"  [v6] cam{i} feat SAVED\n")
                    except Exception as e:
                        _dbg.write(f"  [v6] cam{i} feat ERROR: {e}\n")
                else:
                    _dbg.write(f"  [v6] cam{i} feat SKIP: dim={feat.dim()} != 3\n")
            else:
                _dbg.write(f"  [v6] cam{i} NO feat: i={i} len={len(feats_list)} is_none={feats_list[i] is None if i < len(feats_list) else 'OOB'}\n")

            # attention
            if attn is not None and feat_ok:
                try:
                    n_tokens_before = sum(f.shape[1] for f in feats_list[:i]) if i > 0 else 0
                    n_img_tokens = feats_list[i].shape[1]
                    img_range = (n_tokens_before, n_tokens_before + n_img_tokens)
                    _dbg.write(f"  [v6] cam{i} attn: shape={attn.shape} img_range={img_range}\n")
                    heatmap = _attention_to_heatmap(attn[0], img_range, bgr)
                    _save_jpg(heatmap, f"piper_viz_attention_{i}.jpg")
                    _dbg.write(f"  [v6] cam{i} attn SAVED\n")
                except Exception as e:
                    _dbg.write(f"  [v6] cam{i} attn ERROR: {e}\n")
            else:
                _dbg.write(f"  [v6] cam{i} attn SKIP: attn={attn is not None} feat_ok={feat_ok}\n")

        _dbg.close()
    except Exception as e:
        with open("/dev/shm/piper_viz_debug.log", "a") as _f:
            _f.write(f"  [v6] FATAL: {e}\n")


def _hook_predict_action_chunk(policy, orig_fn, batch, *args, **kwargs):
    """predict_action_chunk를 감싸서 추론 중 attention 캡처 + 시각화 트리거."""
    _captured["_capture_prefix_attn"] = True
    result = orig_fn(batch, *args, **kwargs)
    _captured["_capture_prefix_attn"] = False

    # 디버그 v7
    _dbg = open("/dev/shm/piper_viz_debug.log", "a")
    imgs = _captured.get("preprocessed_images", [])
    feats = _captured.get("vision_features_list", [])
    attn_list = _captured.get("attention_weights_list", [])
    feats_hash = [str(f.sum().item())[:10] for f in feats] if feats else []
    _dbg.write(f"{time.time():.1f} [v7] predict | imgs={len(imgs)} feats={len(feats)} feats_hash={feats_hash} attn={len(attn_list)}\n")

    try:
        # features/attention을 predict 스레드에서 직접 동기 저장 (백그라운드 스레드 지연 방지)
        for i, feat in enumerate(feats):
            if feat is not None and feat.dim() == 3:
                try:
                    feat_cpu = feat.detach().cpu()
                    feat_img = _features_to_colormap(feat_cpu[0], 480, 640)
                    _save_jpg(feat_img, f"piper_viz_features_{i}.jpg")
                    _dbg.write(f"  feat_{i} SAVED (sync)\n")
                except Exception as e:
                    _dbg.write(f"  feat_{i} ERROR: {e}\n")
        # attention 히트맵 (카메라별)
        for i in range(len(feats)):
            if i < len(attn_list) and attn_list[i] is not None and feats[i] is not None:
                try:
                    attn_i = attn_list[i]
                    n_tokens = feats[i].shape[1]
                    img_range = (0, n_tokens)
                    if i < len(imgs):
                        img_t = imgs[i]
                        while img_t.dim() > 3:
                            img_t = img_t[0]
                        bgr = _tensor_to_bgr(img_t.detach().cpu())
                    else:
                        bgr = np.zeros((480, 640, 3), dtype=np.uint8)
                    attn_cpu = attn_i.detach().cpu()
                    heatmap = _attention_to_heatmap(attn_cpu[0], img_range, bgr)
                    _save_jpg(heatmap, f"piper_viz_attention_{i}.jpg")
                    _dbg.write(f"  attn_{i} SAVED (sync)\n")
                except Exception as e:
                    _dbg.write(f"  attn_{i} ERROR: {e}\n")
    except Exception as e:
        _dbg.write(f"  SYNC SAVE ERROR: {e}\n")
    _dbg.close()
    return result


def _hook_embed_prefix(model, orig_fn, images, img_masks, lang_tokens, lang_masks, state=None):
    """embed_prefix를 감싸서 이미지 토큰 범위 추적 + prefix 중 attention 캡처."""
    _captured["_capture_prefix_attn"] = True
    result = orig_fn(images, img_masks, lang_tokens, lang_masks, state)
    _captured["_capture_prefix_attn"] = False
    return result



def register_viz_hooks(policy) -> None:
    """SmolVLA policy에 시각화 hook을 등록한다."""
    logger.info("Registering visualization hooks...")

    # 1. prepare_images 감싸기
    if hasattr(policy, 'prepare_images'):
        orig_prepare = policy.prepare_images
        policy.prepare_images = partial(_hook_prepare_images, policy, orig_prepare)
        logger.info("  - prepare_images hook registered")

    # 2. predict_action_chunk 감싸기 (시각화 트리거)
    if hasattr(policy, 'predict_action_chunk'):
        orig_predict = policy.predict_action_chunk
        policy.predict_action_chunk = partial(_hook_predict_action_chunk, policy, orig_predict)
        logger.info("  - predict_action_chunk hook registered")

    # 3. Vision encoder forward hook
    try:
        vlm_expert = policy.model.vlm_with_expert
        vision_model = vlm_expert.get_vlm_model().vision_model
        vision_model.register_forward_hook(
            lambda m, inp, out: _captured["vision_features_list"].append(out.last_hidden_state.detach().clone())
        )
        logger.info("  - vision_model forward hook registered")
    except Exception as e:
        logger.warning("  - vision_model hook failed: %s", e)

    # 4. embed_prefix 감싸기 (이미지 토큰 범위 추적)
    try:
        model = policy.model
        orig_embed_prefix = model.embed_prefix
        model.embed_prefix = partial(_hook_embed_prefix, model, orig_embed_prefix)
        logger.info("  - embed_prefix hook registered")
    except Exception as e:
        logger.warning("  - embed_prefix hook failed: %s", e)

    # 5. Attention weight 캡처 — vision encoder 마지막 self_attn の forward を wrap
    try:
        target = None
        for name, mod in policy.named_modules():
            if "vision_model.encoder.layers" in name and name.endswith(".self_attn") \
               and not name.endswith((".q_proj", ".k_proj", ".v_proj", ".out_proj")):
                target = (name, mod)
        if target:
            attn_mod = target[1]
            _orig_attn_forward = attn_mod.forward

            def _wrapped_attn_forward(*args, **kwargs):
                result = _orig_attn_forward(*args, **kwargs)
                try:
                    hs = kwargs.get("hidden_states", args[0] if args else None)
                    if hs is not None:
                        B, S, _ = hs.shape
                        q = attn_mod.q_proj(hs).view(B, S, attn_mod.num_heads, attn_mod.head_dim).transpose(1, 2)
                        k = attn_mod.k_proj(hs).view(B, S, attn_mod.num_heads, attn_mod.head_dim).transpose(1, 2)
                        scores = torch.matmul(q, k.transpose(-2, -1)) * (attn_mod.head_dim ** -0.5)

                        probs = torch.nn.functional.softmax(scores, dim=-1, dtype=torch.float32)
                        # 카메라별 attention을 리스트로 축적
                        if "attention_weights_list" not in _captured:
                            _captured["attention_weights_list"] = []
                        _captured["attention_weights_list"].append(probs.detach())
                except Exception:
                    pass
                return result

            attn_mod.forward = _wrapped_attn_forward
            logger.info("  - attention forward wrapped on: %s", target[0])
        else:
            logger.warning("  - no vision encoder self_attn found")
    except Exception as e:
        logger.warning("  - attention hook failed: %s", e)

    logger.info("Visualization hooks registered. Output dir: %s", VIZ_DIR)
