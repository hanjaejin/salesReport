"""읽기 모델 발행 — 로컬에서 만든 결과를 원격 DB(Supabase)로 옮긴다 (ADR-0011).

명세 3장이 "추후 PostgreSQL/Supabase 교체 대비"로 방언 종속 SQL을 금지했고,
그 규율 덕분에 **연결 URL만 바꾸면** 같은 코드가 원격에서 돈다.

옮기는 것은 **화면이 읽는 테이블뿐**이다 (ADR-0009의 읽기 모델).
원장(FACT 3종)은 보내지 않는다 — 화면 경로에 없고, 파생 시드가 결정적이라
필요하면 언제든 로컬에서 되살릴 수 있기 때문이다.

실행:
    # 대상 URL은 환경변수로 주는 것을 권한다 (명령행 이력에 비밀번호가 남지 않는다)
    export POS_BRIEFING_TARGET_URL="postgresql://...:...@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    python -m src.load.publish
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Sequence
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
from sqlalchemy import Engine, Table, delete, func, select

from src.common.config import get_engine, resolve_database_url
from src.common.logger import get_logger
from src.load import schema

logger = get_logger(__name__)

#: 대상 DB URL을 담는 환경변수. 비밀번호가 셸 이력에 남지 않도록 이 경로를 권한다.
TARGET_URL_ENV = "POS_BRIEFING_TARGET_URL"

#: 한 번에 보낼 행수. 원격 왕복을 줄이되 한 요청이 너무 커지지 않게 잡는다.
DEFAULT_BATCH_SIZE = 2_000

#: 발행 대상 — 화면·보고서가 읽는 테이블만. 원장은 여기 없다 (ADR-0009).
READ_MODEL_TABLES: tuple[Table, ...] = (
    schema.DIM_STORE,
    schema.MART_DAY_STORE,
    schema.MART_HOUR_STORE,
    schema.MART_DAY_STORE_ITEM,
    schema.BRIEFING_DAILY,
)


def mask_url(url: str) -> str:
    """연결 URL에서 자격 증명을 가린다 — 로그에 비밀번호가 남지 않게.

    Args:
        url: 연결 URL.

    Returns:
        사용자·비밀번호를 ``***`` 로 바꾼 URL. 자격 증명이 없으면 원본 그대로.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        return url

    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    if parts.username or parts.password:
        host = f"***@{host}"

    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _row_count(engine: Engine, table: Table) -> int:
    """테이블 행수를 센다.

    Args:
        engine: 대상 엔진.
        table: Core 테이블.

    Returns:
        행수.
    """
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _copy_table(
    source: Engine, target: Engine, table: Table, batch_size: int
) -> int:
    """테이블 하나를 통째로 옮긴다 (대상은 먼저 비운다).

    원본을 배치로 나눠 읽어 그대로 넣는다. 전량을 메모리에 올리지 않으므로
    상품 마트처럼 큰 테이블도 안전하다.

    Args:
        source: 원본 엔진.
        target: 대상 엔진.
        table: 옮길 테이블.
        batch_size: 한 번에 보낼 행수.

    Returns:
        옮긴 행수.
    """
    with target.begin() as connection:
        connection.execute(delete(table))

    columns = [column.name for column in table.columns]
    order = list(table.primary_key.columns) or [table.c[columns[0]]]

    moved = 0
    with source.connect() as reader:
        statement = select(table).order_by(*order)
        for frame in pd.read_sql(statement, reader, chunksize=batch_size):
            if frame.empty:
                continue
            with target.begin() as writer:
                writer.execute(table.insert(), schema.to_records(frame[columns]))
            moved += len(frame)
            logger.debug("%s %d행 전송 (누적 %d)", table.name, len(frame), moved)

    return moved


def publish(
    source: Engine,
    target: Engine,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """읽기 모델을 원본에서 대상으로 옮긴다.

    대상에 스키마가 없으면 만든다 — 사전 준비 없이 명령 한 줄로 끝난다.
    원장 테이블도 **생성만** 하고 비워 둔다. 화면의 관리자 재생성 버튼이
    그 자리를 채울 수 있어야 하기 때문이다.

    Args:
        source: 구축이 끝난 원본 엔진.
        target: 발행할 대상 엔진.
        batch_size: 한 번에 보낼 행수.

    Returns:
        테이블명 → 옮긴 행수.

    Raises:
        ValueError: 배치 크기가 1 미만이거나, 원본과 대상이 같거나, 원본이 비었을 때.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size는 1 이상이어야 합니다: {batch_size}")
    if str(source.url) == str(target.url):
        raise ValueError("원본과 대상이 같은 DB입니다 — 발행을 중단합니다.")

    if _row_count(source, schema.BRIEFING_DAILY) == 0:
        raise ValueError(
            "원본의 브리핑이 비어 있습니다. 먼저 구축하세요: "
            "python -m src.load.pipeline --from 20250701 --to 20260731"
        )

    started = time.perf_counter()
    schema.create_all(target)

    counts: dict[str, int] = {}
    for table in READ_MODEL_TABLES:
        counts[table.name] = _copy_table(source, target, table, batch_size)
        logger.info("%-22s %8s행 발행", table.name, f"{counts[table.name]:,}")

    logger.info(
        "발행 완료: %s행 (%d개 테이블, %.1f초) → %s",
        f"{sum(counts.values()):,}",
        len(counts),
        time.perf_counter() - started,
        mask_url(str(target.url)),
    )
    return counts


# --- CLI -------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 파싱한다.

    Args:
        argv: 인자 목록. None이면 ``sys.argv``.

    Returns:
        ``source``·``target``·``batch_size`` 를 가진 네임스페이스.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.load.publish",
        description=(
            "읽기 모델(마트·브리핑·점포)을 원격 DB로 발행한다. 원장은 보내지 않는다."
        ),
        epilog=(
            f"대상 URL은 {TARGET_URL_ENV} 환경변수로 주는 것을 권합니다 — "
            "명령행에 적으면 셸 이력에 비밀번호가 남습니다."
        ),
    )
    parser.add_argument(
        "--source", default=None, help="원본 DB 경로 또는 URL (기본: 로컬 data/pos_mockup.db)"
    )
    parser.add_argument(
        "--target", default=None, help=f"대상 DB URL (기본: 환경변수 {TARGET_URL_ENV})"
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="한 번에 보낼 행수"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 진입점.

    Args:
        argv: 인자 목록. None이면 ``sys.argv``.

    Returns:
        종료 코드. 0이면 성공.
    """
    args = parse_args(argv)

    target_url = args.target or os.environ.get(TARGET_URL_ENV)
    if not target_url:
        logger.error(
            "대상 DB URL이 없습니다. --target 을 주거나 %s 환경변수를 설정하세요.",
            TARGET_URL_ENV,
        )
        return 2

    source = get_engine(resolve_database_url(args.source))
    target = get_engine(target_url)
    logger.info("발행 대상: %s", mask_url(str(target.url)))

    try:
        publish(source, target, batch_size=args.batch_size)
    except ValueError as error:
        logger.error("발행 실패: %s", error)
        return 2
    except Exception as error:  # noqa: BLE001 - 원격 연결 실패를 사용자에게 그대로 알린다
        logger.error("발행 실패 (연결·권한을 확인하세요): %s", error)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
