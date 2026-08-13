"""세그먼트 발행 — 데몬 안에서 프레임을 `/dev/shm` 으로 내보낸다.

`ShmPublisher` 를 게이트웨이에서 그대로 들고 오지 않은 이유: 데몬은 자기 카메라만
발행하므로 훨씬 단순하고, 게이트웨이 서비스에 의존하면 분리한 의미가 없다.
"""

import logging

import numpy as np
from piper_shm import Publisher, segment_for_camera, unlink

logger = logging.getLogger(__name__)

_pubs: dict[str, Publisher] = {}


def publish_frame(cam_id: str, frame: np.ndarray) -> bool:
    """실패해도 예외를 올리지 않는다 — 캡처 루프에서 불린다."""
    if frame is None or frame.ndim != 3:
        return False
    name = segment_for_camera(cam_id)
    h, w, ch = frame.shape
    try:
        pub = _pubs.get(name)
        # ⚠ 치수뿐 아니라 **파일이 살아 있는지**도 본다. 누가 세그먼트를 지우면
        # 열린 fd 로는 계속 써져서, 발행자는 멀쩡한데 소비자는 열 수 없는 상태가
        # 조용히 이어진다 (실제로 D435 가 이 상태로 며칠 갔다).
        if pub is not None and pub.orphaned:
            logger.warning("세그먼트가 사라져 다시 만든다: %s", name)
            pub.close(unlink=False)
            pub = None
        if pub is None or pub.layout.height != h or pub.layout.width != w:
            if pub is not None:
                logger.info("세그먼트 재생성 %s: %dx%d", name, w, h)
                pub.close()
            pub = Publisher(name, width=w, height=h, channels=ch)
            _pubs[name] = pub
        pub.publish(np.ascontiguousarray(frame))
        return True
    except Exception as exc:
        logger.warning("발행 실패 (%s): %s", name, exc)
        return False


def stop(cam_id: str, unlink_segment: bool = True) -> None:
    """발행 중지.

    `unlink_segment=False` 는 **스캔 썸네일**용이다 — probe 가 스트림을 잠깐 켜서
    한 장 얻고 되돌릴 때, 세그먼트를 지우면 화면에 아무것도 안 남는다.
    마지막 프레임은 남기되 발행은 멈춘다.

    ⚠ 그 세그먼트는 발행자가 없으므로 **정지 화면**이다. 소비자(녹화·추론)는
    `prepare_cameras` 가 카메라를 연결한 뒤에 붙으므로 그때는 살아 있는 세그먼트를 본다.
    """
    pub = _pubs.pop(segment_for_camera(cam_id), None)
    if pub is not None:
        pub.close(unlink=unlink_segment)


def stop_all() -> None:
    """⚠ 세그먼트를 남기면 소비자가 **멈춘 화면**을 본다 — 종료 시 반드시 부른다."""
    for name, pub in list(_pubs.items()):
        try:
            pub.close()
        except Exception:
            unlink(name)
    _pubs.clear()
