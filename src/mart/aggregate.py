"""집계 마트 — 명세 7.1. **모든 수치는 여기서만 만들어진다.**

문장 계층(``briefing.py``)도 화면(``app/main.py``)도 계산하지 않는다.
여기서 나온 값을 읽어 쓸 뿐이다 (불변식 1).

**조인 금지(불변식 2)**: 상품(ITEM)과 결제(PAYMENT)는 grain이 다르다.
둘을 직접 조인해 금액을 합치면 영수증 1건이 여러 행으로 불어나 매출이 부풀려진다.
이 모듈은 원장마다 **따로** 집계한 뒤 일×점포 수준에서만 만난다.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
from sqlalchemy import Engine, Table, case, delete, func, select

from src.common.logger import get_logger
from src.load import schema

logger = get_logger(__name__)


def safe_avg_ticket(sale_amt: float, deal_cnt: float) -> float:
    """1인당 구매액을 구한다 — 거래 0건 방어 포함 (명세 7.1).

    Args:
        sale_amt: 매출액.
        deal_cnt: 거래 건수.

    Returns:
        거래가 없으면 0.0, 그 밖에는 ``sale_amt / deal_cnt``.
    """
    if not deal_cnt:
        return 0.0
    return sale_amt / deal_cnt


def _net_deal_count() -> object:
    """양수거래 − 취소거래를 세는 SQL 식을 만든다 (명세 7.1 ``DEAL_CNT``).

    Returns:
        SQLAlchemy 집계 식. 방언 종속 문법을 쓰지 않는다.
    """
    is_cancel = schema.FACT_RECEIPT.c.CANCELTYPE == "1"
    # CASE 식은 표준 SQL이다. SQLite의 iif() 같은 방언 함수를 쓰지 않는다 (명세 3장).
    signed = case((is_cancel, -1), else_=1)
    return func.coalesce(func.sum(signed), 0)


def _replace(
    engine: Engine,
    table: Table,
    frame: pd.DataFrame,
    from_date: str,
    to_date: str,
    dept_cds: Sequence[str] | None,
) -> int:
    """기간(과 지정 점포)의 마트를 지우고 다시 넣는다 — 멱등.

    Args:
        engine: 대상 엔진.
        table: 갱신할 마트 테이블.
        frame: 새로 넣을 행. 컬럼은 DDL과 일치해야 한다.
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.
        dept_cds: 대상 점포코드. None이면 전 점포.

    Returns:
        적재한 행수.
    """
    statement = delete(table).where(table.c.SALEDATE.between(from_date, to_date))
    if dept_cds is not None:
        statement = statement.where(table.c.DEPT_CD.in_(dept_cds))

    with engine.begin() as connection:
        connection.execute(statement)
        if not frame.empty:
            connection.execute(table.insert(), schema.to_records(frame))
    return len(frame)


def _scoped(statement, table: Table, from_date: str, to_date: str, dept_cds):  # type: ignore[no-untyped-def]
    """기간·점포 조건을 붙인다.

    Args:
        statement: 대상 SELECT.
        table: 조건을 걸 테이블.
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.
        dept_cds: 대상 점포코드. None이면 전 점포.

    Returns:
        조건이 붙은 SELECT.
    """
    statement = statement.where(table.c.SALEDATE.between(from_date, to_date))
    if dept_cds is not None:
        statement = statement.where(table.c.DEPT_CD.in_(dept_cds))
    return statement


def build_day_store(
    engine: Engine, from_date: str, to_date: str, dept_cds: Sequence[str] | None = None
) -> int:
    """``MART_DAY_STORE`` 를 재계산한다 (명세 7.1).

    영수증 원장에서 매출·거래건수를, 상품 원장에서 판매수량을 **각각 따로** 집계한 뒤
    일×점포 수준에서 합친다. 두 집계 모두 이미 일×점포 1행이므로 행이 증폭되지 않는다.

    Args:
        engine: 대상 엔진.
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.
        dept_cds: 대상 점포코드. None이면 전 점포.

    Returns:
        적재한 행수.
    """
    receipt = schema.FACT_RECEIPT
    receipt_query = _scoped(
        select(
            receipt.c.SALEDATE,
            receipt.c.DEPT_CD,
            func.coalesce(func.sum(receipt.c.DEALAMOUNT), 0).label("SALE_AMT"),
            _net_deal_count().label("DEAL_CNT"),
        ).group_by(receipt.c.SALEDATE, receipt.c.DEPT_CD),
        receipt,
        from_date,
        to_date,
        dept_cds,
    )

    item = schema.FACT_RECEIPT_ITEM
    quantity_query = _scoped(
        select(
            item.c.SALEDATE,
            item.c.DEPT_CD,
            func.coalesce(func.sum(item.c.QTY), 0).label("ITEM_QTY"),
        ).group_by(item.c.SALEDATE, item.c.DEPT_CD),
        item,
        from_date,
        to_date,
        dept_cds,
    )

    with engine.connect() as connection:
        totals = pd.read_sql(receipt_query, connection)
        quantities = pd.read_sql(quantity_query, connection)

    frame = totals.merge(quantities, on=["SALEDATE", "DEPT_CD"], how="left")
    frame["ITEM_QTY"] = frame["ITEM_QTY"].fillna(0).astype("int64")

    # 거래 0건인 날은 0으로 둔다 (명세 7.1 "0나눗셈 방어"). where 로 나눗셈 자체를 건너뛴다.
    amounts = frame["SALE_AMT"].to_numpy(dtype=float)
    counts = frame["DEAL_CNT"].to_numpy(dtype=float)
    frame["AVG_TICKET"] = np.divide(
        amounts, counts, out=np.zeros(len(frame), dtype=float), where=counts != 0
    )

    columns = [column.name for column in schema.MART_DAY_STORE.columns]
    return _replace(
        engine, schema.MART_DAY_STORE, frame[columns], from_date, to_date, dept_cds
    )


def build_hour_store(
    engine: Engine, from_date: str, to_date: str, dept_cds: Sequence[str] | None = None
) -> int:
    """``MART_HOUR_STORE`` 를 재계산한다 (명세 7.1: SALETIME 앞 2자리 기준).

    Args:
        engine: 대상 엔진.
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.
        dept_cds: 대상 점포코드. None이면 전 점포.

    Returns:
        적재한 행수.
    """
    receipt = schema.FACT_RECEIPT
    hour = func.substr(receipt.c.SALETIME, 1, 2).label("HOUR")

    query = _scoped(
        select(
            receipt.c.SALEDATE,
            receipt.c.DEPT_CD,
            hour,
            func.coalesce(func.sum(receipt.c.DEALAMOUNT), 0).label("SALE_AMT"),
            _net_deal_count().label("DEAL_CNT"),
        ).group_by(receipt.c.SALEDATE, receipt.c.DEPT_CD, hour),
        receipt,
        from_date,
        to_date,
        dept_cds,
    )

    with engine.connect() as connection:
        frame = pd.read_sql(query, connection)

    columns = [column.name for column in schema.MART_HOUR_STORE.columns]
    return _replace(
        engine, schema.MART_HOUR_STORE, frame[columns], from_date, to_date, dept_cds
    )


def build_day_store_item(
    engine: Engine, from_date: str, to_date: str, dept_cds: Sequence[str] | None = None
) -> int:
    """``MART_DAY_STORE_ITEM`` 을 재계산한다 (명세 7.1).

    상품 원장 하나만 읽는다. 다른 원장을 끌어들이지 않는 것이 이 함수의 규율이다.

    Args:
        engine: 대상 엔진.
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.
        dept_cds: 대상 점포코드. None이면 전 점포.

    Returns:
        적재한 행수.
    """
    item = schema.FACT_RECEIPT_ITEM
    query = _scoped(
        select(
            item.c.SALEDATE,
            item.c.DEPT_CD,
            item.c.PLU_CD,
            func.max(item.c.GOODS_NM).label("GOODS_NM"),
            func.max(item.c.ITEM_HEAD_NM).label("ITEM_HEAD_NM"),
            func.coalesce(func.sum(item.c.SALEAMOUNT), 0).label("SALE_AMT"),
            func.coalesce(func.sum(item.c.QTY), 0).label("QTY"),
        ).group_by(item.c.SALEDATE, item.c.DEPT_CD, item.c.PLU_CD),
        item,
        from_date,
        to_date,
        dept_cds,
    )

    with engine.connect() as connection:
        frame = pd.read_sql(query, connection)

    columns = [column.name for column in schema.MART_DAY_STORE_ITEM.columns]
    return _replace(
        engine, schema.MART_DAY_STORE_ITEM, frame[columns], from_date, to_date, dept_cds
    )


def build_marts(
    engine: Engine, from_date: str, to_date: str, dept_cds: Sequence[str] | None = None
) -> dict[str, int]:
    """마트 3종을 기간 단위로 재계산한다 (명세 8장 3단계).

    Args:
        engine: 대상 엔진.
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.
        dept_cds: 대상 점포코드. None이면 전 점포.

    Returns:
        마트 이름 → 적재 행수.
    """
    counts = {
        "MART_DAY_STORE": build_day_store(engine, from_date, to_date, dept_cds),
        "MART_HOUR_STORE": build_hour_store(engine, from_date, to_date, dept_cds),
        "MART_DAY_STORE_ITEM": build_day_store_item(engine, from_date, to_date, dept_cds),
    }
    logger.info(
        "마트 재계산 완료 %s ~ %s: 일 %s행 · 시간대 %s행 · 상품 %s행",
        from_date,
        to_date,
        f"{counts['MART_DAY_STORE']:,}",
        f"{counts['MART_HOUR_STORE']:,}",
        f"{counts['MART_DAY_STORE_ITEM']:,}",
    )
    return counts
