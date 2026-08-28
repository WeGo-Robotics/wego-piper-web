# 구운 기구학 지오메트리

각 파일은 URDF + 충돌 메시에서 `tools/build_arm_geometry.py` 가 만든다.
런타임에는 URDF 를 안 읽는다 — robotd 는 호스트에 가볍게 배포되고 배포 절차에
서브모듈 단계가 없다. 드리프트는 `backend/tests/test_arm_geometry.py` 가 잡는다.

| 파일 | 팔 | 자유도 | 말단 | 출처 |
|---|---|---|---|---|
| `arm_geometry.npz` | Piper | 6 | `link6` | `vendor/agx_arm_urdf` 서브모듈 (AgileX) |
| `so101_geometry.npz` | SO-101 | 5 | `gripper_frame_link` | [SO-ARM100](https://github.com/TheRobotStudio/SO-ARM100) `Simulation/SO101/so101_new_calib.urdf`, Apache-2.0 |

## 다시 굽기

```bash
# Piper (서브모듈 필요)
python3 tools/build_arm_geometry.py --cell 0.005

# SO-101 — 저장소를 받아서
git clone --depth 1 https://github.com/TheRobotStudio/SO-ARM100 /tmp/so-arm100
python3 tools/build_arm_geometry.py --cell 0.005 \
    --urdf /tmp/so-arm100/Simulation/SO101/so101_new_calib.urdf \
    --tip gripper_frame_link \
    --out robot/piper_robot/data/so101_geometry.npz
```

⚠ `--tip` 을 빼면 사슬 끝(`moving_jaw…`)이 말단이 된다. SO-101 의 TCP 는
메시가 없는 좌표계 링크 `gripper_frame_link` 다.

## 새 팔

`--urdf` 와 `--tip` 만 주면 된다. 사슬은 `--base` 에서 위상 순서로 잇는다 —
**URDF 문서 순서를 믿지 않는다** (SO-101 은 말단부터 적혀 있다). 관절 한계와
말단 링크 이름도 같이 굽는다. IK 가 둘 다 쓴다.
