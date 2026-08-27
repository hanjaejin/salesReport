"""적재 파이프라인 — 명세 8장.

일일 배치·수동 재처리·초기 구축이 전부 ``load_period()`` 하나의 인자 차이다.
코드가 하나이므로 검증도 한 번이면 된다 (흐름도 FLOW 04).

**멱등(불변식 3)**: 기간의 FACT 3종을 날짜 단위로 지우고 다시 넣는다.
몇 번을 돌려도 결과가 같다 — 명세 12장의 라이브 시연 항목이다.

실행:
    python -m src.load.pipeline --from 20250701 --to 20260731 [--stores 901001,901002]
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, Table, delete, func, select

from src.common.config import DB_PATH, get_engine
from src.common.dateutil import date_range, parse_date
from src.common.logger import get_logger
from src.extract.base import ReceiptExtractor
from src.load import schema

logger = get_logger(__name__)


@dataclass(frozen=True)
class LoadResult:
    """적재 결과 요약 (명세 8장 반환값).

    Attributes:
        receipts: 적재한 영수증 행수.
        items: 적재한 상품 행수.
        payments: 적재한 결제 행수.
        days: 처리한 일수.
        elapsed_sec: 소요 시간(초).
    """

    receipts: int
    items: int
    payments: int
    days: int
    elapsed_sec: float

    @property
    def total_rows(self) -> int:
        """적재한 전체 행수."""
        return self.receipts + self.items + self.payments


def _validate_period(from_date: str, to_date: str) -> list[str]:
    """기간 인자를 검사하고 날짜 목록으로 편다.

    Args:
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.

    Returns:
        오름차순 날짜 목록.

    Raises:
        ValueError: 날짜 형식이 잘못됐거나 시작일이 종료일보다 뒤일 때.
    """
    try:
        start, end = parse_date(from_date), parse_date(to_date)
    except ValueError as error:
        raise ValueError(f"날짜는 YYYYMMDD 형식이어야 합니다: {from_date} ~ {to_date}") from error

    if start > end:
        raise ValueError(f"기간이 거꾸로입니다: {from_date} ~ {to_date}")

    return date_range(from_date, to_date)


def _insert_chunks(
    engine: Engine, table: Table, chunks: Iterator[pd.DataFrame], label: str
) -> int:
    """청크를 차례로 INSERT 한다.

    Args:
        engine: 대상 엔진.
        table: 적재할 Core 테이블.
        chunks: DataFrame 청크 반복자.
        label: 로그에 쓸 이름.

    Returns:
        적재한 총 행수.
    """
    total = 0
    for chunk in chunks:
        if chunk.empty:
            continue
        with engine.begin() as connection:
            connection.execute(table.insert(), schema.to_records(chunk))
        total += len(chunk)
        logger.debug("%s 청크 적재 %d행 (누적 %d행)", label, len(chunk), total)
    return total


def _delete_period(
    engine: Engine, dates: Sequence[str], dept_cds: Sequence[str] | None
) -> None:
    """기간(그리고 지정 점포)의 FACT 3종을 지운다 — 멱등의 앞단.

    Args:
        engine: 대상 엔진.
        dates: 삭제할 날짜 목록.
        dept_cds: 대상 점포코드. None이면 전 점포.
    """
    tables = (schema.FACT_RECEIPT, schema.FACT_RECEIPT_ITEM, schema.FACT_PAYMENT)

    with engine.begin() as connection:
        for table in tables:
            statement = delete(table).where(table.c.SALEDATE.in_(dates))
            if dept_cds is not None:
                statement = statement.where(table.c.DEPT_CD.in_(dept_cds))
            connection.execute(statement)


def _load_stores(engine: Engine, extractor: ReceiptExtractor) -> int:
    """점포 마스터를 DELETE→INSERT 로 갱신한다.

    ``extract_stores`` 는 ``ReceiptExtractor`` 계약에 없는 **선택 메서드**다.
    Oracle 연동 시 점포 마스터는 ``TB_HBM001`` 이라는 다른 원천에서 오므로
    계약에 넣지 않고, 제공하는 Extractor에 대해서만 적재한다.

    Args:
        engine: 대상 엔진.
        extractor: 원천 Extractor.

    Returns:
        적재한 점포 수 (제공하지 않는 Extractor면 0).
    """
    extract_stores = getattr(extractor, "extract_stores", None)
    if extract_stores is None:
        return 0

    frame: pd.DataFrame = extract_stores()
    if frame.empty:
        return 0

    with engine.begin() as connection:
        connection.execute(
            delete(schema.DIM_STORE).where(
                schema.DIM_STORE.c.DEPT_CD.in_(frame["DEPT_CD"].tolist())
            )
        )
        connection.execute(schema.DIM_STORE.insert(), schema.to_records(frame))
    return len(frame)


def reconcile(engine: Engine, from_date: str, to_date: str) -> pd.DataFrame:
    """일자별 대사(품질 게이트)를 수행한다 (흐름도 FLOW 07).

    세 가지를 맞춰 본다:
        - ``ITEMCNT`` 합 = 상품 행수
        - ``TENDERCNT`` 합 = 결제 행수
        - ``DEALAMOUNT`` 합 = 상품 금액 합 = 결제 금액 합

    ITEM과 PAYMENT를 **조인하지 않는다** — 각각 따로 집계해 영수증 헤더와 맞춘다
    (불변식 2: 조인하면 행이 증폭돼 금액이 부풀려진다).

    Args:
        engine: 대상 엔진.
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.

    Returns:
        일자별 집계와 통과 여부(``ITEMCNT_OK``·``TENDERCNT_OK``·``AMOUNT_OK``) 프레임.
    """
    receipt = schema.FACT_RECEIPT
    item = schema.FACT_RECEIPT_ITEM
    payment = schema.FACT_PAYMENT

    def by_date(table: Table, *aggregates):  # type: ignore[no-untyped-def]
        """해당 테이블을 기간으로 걸러 일자별로 집계한다.

        Args:
            table: 집계할 Core 테이블.
            *aggregates: 일자 옆에 붙일 집계 컬럼들.

        Returns:
            일자별 집계 SELECT 문.
        """
        return (
            select(table.c.SALEDATE, *aggregates)
            .where(table.c.SALEDATE.between(from_date, to_date))
            .group_by(table.c.SALEDATE)
        )

    receipt_query = by_date(
        receipt,
        func.count().label("DEAL_ROWS"),
        func.coalesce(func.sum(receipt.c.ITEMCNT), 0).label("ITEMCNT_SUM"),
        func.coalesce(func.sum(receipt.c.TENDERCNT), 0).label("TENDERCNT_SUM"),
        func.coalesce(func.sum(receipt.c.DEALAMOUNT), 0).label("DEAL_AMOUNT"),
    )
    item_query = by_date(
        item,
        func.count().label("ITEM_ROWS"),
        func.coalesce(func.sum(item.c.SALEAMOUNT), 0).label("ITEM_AMOUNT"),
    )
    payment_query = by_date(
        payment,
        func.count().label("PAYMENT_ROWS"),
        func.coalesce(func.sum(payment.c.TENDERAMOUNT), 0).label("PAYMENT_AMOUNT"),
    )

    with engine.connect() as connection:
        receipts = pd.read_sql(receipt_query, connection)
        items = pd.read_sql(item_query, connection)
        payments = pd.read_sql(payment_query, connection)

    report = receipts.merge(items, on="SALEDATE", how="left").merge(
        payments, on="SALEDATE", how="left"
    )
    numeric = ["ITEM_ROWS", "ITEM_AMOUNT", "PAYMENT_ROWS", "PAYMENT_AMOUNT"]
    report[numeric] = report[numeric].fillna(0).astype("int64")

    report["ITEMCNT_OK"] = report["ITEMCNT_SUM"] == report["ITEM_ROWS"]
    report["TENDERCNT_OK"] = report["TENDERCNT_SUM"] == report["PAYMENT_ROWS"]
    report["AMOUNT_OK"] = (report["DEAL_AMOUNT"] == report["ITEM_AMOUNT"]) & (
        report["DEAL_AMOUNT"] == report["PAYMENT_AMOUNT"]
    )

    return report.sort_values("SALEDATE").reset_index(drop=True)


def _log_reconciliation(report: pd.DataFrame) -> None:
    """대사 결과를 일자별로 남긴다 (명세 8장: "일자별 적재 건수·대사 결과").

    Args:
        report: ``reconcile`` 이 만든 프레임.
    """
    for row in report.itertuples(index=False):
        passed = row.ITEMCNT_OK and row.TENDERCNT_OK and row.AMOUNT_OK
        logger.log(
            20 if passed else 40,  # INFO / ERROR
            "%s 영수증 %5d건 상품 %5d행 결제 %5d행 매출 %11s원 대사 %s",
            row.SALEDATE,
            row.DEAL_ROWS,
            row.ITEM_ROWS,
            row.PAYMENT_ROWS,
            f"{row.DEAL_AMOUNT:,}",
            "OK" if passed else "불일치",
        )

    failed = report[~(report["ITEMCNT_OK"] & report["TENDERCNT_OK"] & report["AMOUNT_OK"])]
    if not failed.empty:
        logger.error("대사 불일치 %d일: %s", len(failed), ", ".join(failed["SALEDATE"]))


def load_period(
    extractor: ReceiptExtractor,
    from_date: str,
    to_date: str,
    engine: Engine | None = None,
) -> LoadResult:
    """[삭제→적재→집계→브리핑]을 날짜 단위 멱등으로 수행한다 (명세 8장).

    1) FACT 3종에서 기간 DELETE
    2) 청크 INSERT
    3) 기간의 MART 3종 재계산
    4) 기간의 BRIEFING_DAILY 재생성

    브리핑 문장까지 여기서 완성돼 DB에 저장된다. 화면은 저장된 글자를 표시만 한다
    (불변식 7 — 서버측 생성 원칙).

    Args:
        extractor: 원천 Extractor (합성이든 Oracle이든 계약만 지키면 된다).
        from_date: 시작일 ``YYYYMMDD`` (포함).
        to_date: 종료일 ``YYYYMMDD`` (포함).
        engine: 대상 엔진. None이면 ``config.DB_PATH`` 의 기본 엔진.

    Returns:
        적재 결과 요약.

    Raises:
        ValueError: 기간 인자가 잘못됐을 때.
    """
    from src.mart import aggregate, briefing
    started = time.perf_counter()
    dates = _validate_period(from_date, to_date)
    engine = engine if engine is not None else get_engine()

    schema.create_all(engine)

    dept_cds = getattr(extractor, "_dept_cds", None)
    logger.info(
        "적재 시작 %s ~ %s (%d일, 점포 %s)",
        from_date,
        to_date,
        len(dates),
        ",".join(dept_cds) if dept_cds else "전체",
    )

    store_count = _load_stores(engine, extractor)
    if store_count:
        logger.info("점포 마스터 %d곳 갱신", store_count)

    _delete_period(engine, dates, dept_cds)

    receipts = _insert_chunks(
        engine, schema.FACT_RECEIPT, extractor.extract_receipts(from_date, to_date), "영수증"
    )
    items = _insert_chunks(
        engine, schema.FACT_RECEIPT_ITEM, extractor.extract_items(from_date, to_date), "상품"
    )
    payments = _insert_chunks(
        engine, schema.FACT_PAYMENT, extractor.extract_payments(from_date, to_date), "결제"
    )

    _log_reconciliation(reconcile(engine, from_date, to_date))

    aggregate.build_marts(engine, from_date, to_date, dept_cds)
    briefing.build_briefings(engine, from_date, to_date, dept_cds)

    result = LoadResult(
        receipts=receipts,
        items=items,
        payments=payments,
        days=len(dates),
        elapsed_sec=round(time.perf_counter() - started, 3),
    )
    logger.info(
        "적재 완료: 영수증 %s건 상품 %s행 결제 %s행 (%d일, %.1f초)",
        f"{result.receipts:,}",
        f"{result.items:,}",
        f"{result.payments:,}",
        result.days,
        result.elapsed_sec,
    )
    return result


# --- CLI -------------------------------------------------------------------


def slim_for_deploy(engine: Engine) -> None:
    """배포용으로 원장(FACT 3종)을 비우고 파일을 줄인다 (ADR-0009).

    화면이 읽는 것은 마트와 브리핑뿐이다. 원장은 마트를 만드는 재료이며,
    파생 시드가 결정적이라 같은 명령을 ``--deploy`` 없이 돌리면 그대로 복원된다.

    **구축을 마친 뒤 마지막에** 호출해야 한다. 원장이 빈 상태에서 마트를 다시 만들면
    마트가 비어 버린다.

    Args:
        engine: 대상 엔진.
    """
    from sqlalchemy import text

    tables = (schema.FACT_RECEIPT, schema.FACT_RECEIPT_ITEM, schema.FACT_PAYMENT)

    with engine.begin() as connection:
        for table in tables:
            connection.execute(delete(table))

    # VACUUM은 트랜잭션 밖에서만 실행할 수 있다.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM"))

    logger.info(
        "배포용 정리 완료: 원장 3종을 비웠습니다. "
        "화면이 읽는 마트·브리핑은 그대로입니다 (복원: 같은 명령을 --deploy 없이 재실행)."
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 파싱한다 (명세 8장 형식).

    Args:
        argv: 인자 목록. None이면 ``sys.argv``.

    Returns:
        ``from_date``·``to_date``·``stores``·``db`` 를 가진 네임스페이스.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.load.pipeline",
        description="30초 매장 브리핑 — 기간 데이터 구축 (삭제→적재→대사, 멱등)",
    )
    parser.add_argument("--from", dest="from_date", required=True, help="시작일 YYYYMMDD")
    parser.add_argument("--to", dest="to_date", required=True, help="종료일 YYYYMMDD")
    parser.add_argument(
        "--stores",
        type=lambda value: [code.strip() for code in value.split(",") if code.strip()],
        default=None,
        help="점포코드 목록 (쉼표 구분). 생략하면 전 점포",
    )
    parser.add_argument(
        "--db", default=None, help=f"SQLite 파일 경로 (기본: {DB_PATH})"
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="구축 후 원장(FACT)을 비워 배포용으로 줄인다. 마지막에만 쓸 것 (ADR-0009)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 진입점.

    Args:
        argv: 인자 목록. None이면 ``sys.argv``.

    Returns:
        종료 코드. 0이면 성공.
    """
    from src.extract.sample import SampleExtractor

    args = parse_args(argv)
    engine = get_engine(Path(args.db)) if args.db else get_engine()

    try:
        load_period(
            SampleExtractor(dept_cds=args.stores), args.from_date, args.to_date, engine=engine
        )
        if args.deploy:
            slim_for_deploy(engine)
    except ValueError as error:
        logger.error("적재 실패: %s", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
