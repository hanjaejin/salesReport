"""공통 로거 — 이 프로젝트에서 화면 출력은 전부 이 모듈을 거친다.

`print` 사용은 사내 코딩 표준으로 금지돼 있다(명세 문서정보 · doc/CLAUDE.md).
배치(pipeline)와 화면(Streamlit)이 같은 포맷으로 기록되도록 핸들러 설정을
한 곳에서만 수행한다.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

_LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _force_utf8(stream: TextIO) -> None:
    """출력 스트림 인코딩을 UTF-8로 고정한다.

    Windows 기본 콘솔 인코딩(cp949)으로는 로그의 한국어가 UTF-8 터미널·파일에서
    깨져 읽힌다. 배포 대상인 Streamlit Community Cloud(Linux)가 UTF-8이므로
    개발 환경도 UTF-8로 맞춰 두 환경의 로그가 같게 보이도록 한다.

    Args:
        stream: 재설정할 텍스트 스트림.

    Note:
        ``reconfigure``는 Python 3.7+ 의 ``io.TextIOWrapper`` 에만 있다.
        Streamlit이 표준 스트림을 자체 객체로 바꿔 두면 속성이 없으므로,
        그때는 조용히 건너뛴다 (기존 인코딩 유지 — 로그 자체는 계속 남는다).
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    reconfigure(encoding="utf-8", errors="backslashreplace")


def _configure_root() -> None:
    """루트 로거에 stderr 핸들러를 한 번만 설치한다.

    Streamlit은 스크립트를 반복 재실행하므로, 핸들러가 중복 부착되면
    같은 줄이 여러 번 찍힌다. 모듈 수준 플래그로 1회만 설정한다.
    """
    global _configured
    if _configured:
        return

    _force_utf8(sys.stderr)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger("pos_briefing")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.propagate = False

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """모듈 전용 로거를 반환한다.

    Args:
        name: 보통 호출 모듈의 ``__name__``.

    Returns:
        ``pos_briefing`` 네임스페이스 아래에 매달린 로거.
    """
    _configure_root()
    return logging.getLogger(f"pos_briefing.{name}")
