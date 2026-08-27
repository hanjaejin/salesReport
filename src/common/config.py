"""프로젝트 전역 설정 — 명세가 확정한 상수를 한 곳에 모은다.

여기 있는 값은 대부분 명세(`doc/30초매장브리핑_바이브코딩_구현설계서_v1.3.1.md`)가
확정한 것이라 임의로 바꿀 수 없다. 각 상수에 근거 절을 주석으로 남긴다.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Final

import numpy as np
from sqlalchemy import Engine, create_engine

# --- 경로 -----------------------------------------------------------------
#: 프로젝트 루트 (이 파일 기준 2단계 상위 — src/common/config.py → src/ → 루트)
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: 데이터·산출물 디렉토리 (명세 4장)
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"

#: 데모용 SQLite 파일 (명세 3장)
DB_PATH: Final[Path] = DATA_DIR / "pos_mockup.db"

#: 연결 URL을 덮어쓰는 환경변수. 원격 PostgreSQL(Supabase)로 옮길 때 쓴다 (ADR-0011).
DB_URL_ENV: Final[str] = "POS_BRIEFING_DB_URL"

#: 씨앗에서 추출해 동결한 상품 사전 (ADR-0002)
SEED_CATALOG_PATH: Final[Path] = PROJECT_ROOT / "src" / "generate" / "seed_catalog.json"

# --- 생성 파라미터 ---------------------------------------------------------
#: 기본 시드. 여기서 (날짜, 점포) 파생 시드를 만든다 (명세 3장 · 불변식 4)
BASE_SEED: Final[int] = 20260723

#: 합성 데이터 기간 (명세 6.2) — YYYYMMDD
PERIOD_FROM: Final[str] = "20250701"
PERIOD_TO: Final[str] = "20260731"

#: 영업시간 (명세 6.2) — 05시~23시
OPEN_HOUR: Final[int] = 5
CLOSE_HOUR: Final[int] = 23

#: Extractor가 yield 하는 청크 크기 (명세 5장)
CHUNK_SIZE: Final[int] = 50_000

# --- 브리핑 임계값 (명세 7.3) ----------------------------------------------
#: G4 구조 카드: abs(prev_diff_pct) >= 5.0
G4_THRESHOLD_PCT: Final[float] = 5.0

#: G2 시간대 카드: peak_block.share_pct >= 25.0
G2_THRESHOLD_PCT: Final[float] = 25.0

#: 1줄 문장 분기 임계 (명세 7.4) — dow_diff_pct 가 ±3 밖이면 증감 문구
LINE1_THRESHOLD_PCT: Final[float] = 3.0

#: 요일 기준선 표본 하한 (명세 7.4 폴백) — 직전 4주 동일 요일
DOW_BASELINE_WEEKS: Final[int] = 4


def resolve_database_url(target: Path | str | None = None) -> str:
    """대상 DB의 연결 URL을 정한다.

    우선순위: 인자 → 환경변수 ``POS_BRIEFING_DB_URL`` → 로컬 SQLite 기본값.

    명세 3장이 "추후 PostgreSQL/Supabase 교체 대비"를 요구했으므로, 코드를 고치지 않고
    **URL만 바꿔** 원격 PostgreSQL로 옮길 수 있게 한다 (ADR-0011).

    Args:
        target: 파일 경로 또는 ``postgresql://`` 같은 연결 URL. None이면 환경변수·기본값.

    Returns:
        SQLAlchemy 연결 URL.
    """
    if target is None:
        target = os.environ.get(DB_URL_ENV) or DB_PATH

    text = str(target)
    if "://" in text:
        return text
    return f"sqlite:///{Path(text)}"


def get_engine(target: Path | str | None = None, *, echo: bool = False) -> Engine:
    """DB 엔진을 만든다 (SQLite·PostgreSQL 공통).

    Args:
        target: 파일 경로 또는 연결 URL. None이면 환경변수·기본값 (``resolve_database_url``).
        echo: True면 SQLAlchemy가 실행 SQL을 로깅한다 (디버깅용).

    Returns:
        SQLAlchemy Core 엔진.

    Note:
        SQLite면 상위 디렉토리를 만든다 — 첫 실행에서 ``data/`` 가 비어 있어도
        파이프라인이 바로 돌아야 하기 때문이다.

        원격 DB에는 ``pool_pre_ping`` 을 켠다. 유휴 연결이 끊긴 뒤 첫 질의가
        실패하는 것을 막는다 — 발표 도중 화면이 비는 사고를 예방하는 장치다.
    """
    url = resolve_database_url(target)

    if url.startswith("sqlite"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url, echo=echo)

    return create_engine(url, echo=echo, pool_pre_ping=True, pool_recycle=300)


def is_sqlite(engine: Engine) -> bool:
    """엔진이 SQLite인지 알려준다.

    방언마다 다르게 다뤄야 하는 지점(예: ``VACUUM``)에서만 쓴다.
    질의 자체는 방언에 의존하지 않는다 (명세 3장).

    Args:
        engine: 검사할 엔진.

    Returns:
        SQLite면 True.
    """
    return engine.dialect.name == "sqlite"


def derive_seed(dept_cd: str, saledate: str) -> int:
    """(기본 시드, 점포, 날짜)에서 그 날·그 점포 전용 시드를 만든다 (명세 3장 · 불변식 4).

    명세는 이 규칙을 ``hash((20260723, DEPT_CD, SALEDATE))`` 로 적었지만,
    파이썬 내장 ``hash`` 는 문자열에 대해 **프로세스마다 다른 값**을 돌려준다
    (PEP 456 해시 랜덤화). 그대로 쓰면 재실행마다 데이터가 달라져
    불변식 4가 요구하는 "어떤 부분 구간을 재생성해도 동일 데이터"가 성립하지 않는다.
    그래서 같은 뜻을 갖는 **안정 해시(blake2b)** 로 구현한다 (ADR-0005).

    Args:
        dept_cd: 점포코드.
        saledate: ``YYYYMMDD`` 매출일자.

    Returns:
        0 이상 2**63 미만의 결정적 정수 시드.
    """
    payload = f"{BASE_SEED}|{dept_cd}|{saledate}".encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False) >> 1


def derive_rng(dept_cd: str, saledate: str) -> np.random.Generator:
    """(점포, 날짜) 전용 독립 난수 생성기를 만든다 (불변식 4).

    전역 순차 난수를 쓰면 앞선 날짜의 소비량이 뒤 날짜 결과를 바꿔
    부분 재생성이 달라진다. 날짜·점포마다 생성기를 새로 만들어 격리한다.

    Args:
        dept_cd: 점포코드.
        saledate: ``YYYYMMDD`` 매출일자.

    Returns:
        해당 날짜·점포에 대해 항상 같은 수열을 내는 ``numpy`` 생성기.
    """
    return np.random.default_rng(derive_seed(dept_cd, saledate))
