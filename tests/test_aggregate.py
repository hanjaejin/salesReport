"""집계 마트 검증 — 명세 7.1과 불변식 2.

여기서 계산된 숫자가 브리핑·화면·보고서 전부의 원천이다.
틀리면 세 곳이 함께 틀리므로 마트 단계에서 잡는다.
"""

from __future__ import annotations

import inspect as inspect_module
import re
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine, select

from src.common.config import get_engine
from src.extract.sample import SampleExtractor
from src.load import pipeline, schema
from src.mart import aggregate

FROM_DATE = "20260701"
TO_DATE = "20260707"


@pytest.fixture(scope="module")
def loaded_engine(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    """FACT 적재와 마트 계산까지 마친 엔진 (모듈 1회)."""
    db_path: Path = tmp_path_factory.mktemp("mart") / "mart.db"
    engine = get_engine(db_path)

    pipeline.load_period(SampleExtractor(), FROM_DATE, TO_DATE, engine=engine)
    aggregate.build_marts(engine, FROM_DATE, TO_DATE)
    return engine


def _read(engine: Engine, table: object) -> pd.DataFrame:
    """마트 테이블 전체를 읽는다.

    Args:
        engine: 대상 엔진.
        table: 읽을 Core 테이블.

    Returns:
        DataFrame.
    """
    with engine.connect() as connection:
        return pd.read_sql(select(table), connection)  # type: ignore[arg-type]


def _read_facts(engine: Engine) -> tuple[pd.DataFrame, pd.DataFrame]:
    """원장(영수증·상품)을 읽는다 — 마트를 원장으로 다시 검산하기 위해.

    Args:
        engine: 대상 엔진.

    Returns:
        ``(영수증, 상품)`` 프레임.
    """
    with engine.connect() as connection:
        receipts = pd.read_sql(select(schema.FACT_RECEIPT), connection)
        items = pd.read_sql(select(schema.FACT_RECEIPT_ITEM), connection)
    return receipts, items


# --- 불변식 2: 조인 금지 ---------------------------------------------------


def test_mart_no_item_payment_join() -> None:
    """명세 10장(정적 검사): aggregate 소스에 ITEM·PAYMENT 동시 조인이 없다.

    grain이 다른 두 원장을 조인해 금액을 합치면 매출이 부풀려진다 (불변식 2).
    코드 리뷰가 아니라 테스트가 이 금지를 지킨다.
    """
    source = Path(inspect_module.getfile(aggregate)).read_text(encoding="utf-8")

    # 주석·docstring을 걷어내고 실행되는 코드만 본다 (설명문에 등장하는 이름은 무해하다).
    code_only = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines()
    )
    code_only = re.sub(r'"""(?:.|\n)*?"""', "", code_only)

    statements = [
        segment
        for segment in re.split(r"\n(?=\S)", code_only)
        if "FACT_RECEIPT_ITEM" in segment and "FACT_PAYMENT" in segment
    ]

    assert statements == [], (
        "ITEM과 PAYMENT를 같은 구문에서 다루고 있다 — 조인으로 금액이 부풀려질 수 있다:\n"
        + "\n---\n".join(statements)
    )


# --- 명세 7.1 계산 정의 ----------------------------------------------------


def test_mart_day_store_matches_facts(loaded_engine: Engine) -> None:
    """명세 7.1: SALE_AMT=ΣDEALAMOUNT, DEAL_CNT=양수거래−취소거래, ITEM_QTY=ΣQTY."""
    receipts, items = _read_facts(loaded_engine)
    mart = _read(loaded_engine, schema.MART_DAY_STORE)

    key = ["SALEDATE", "DEPT_CD"]
    expected_amount = receipts.groupby(key)["DEALAMOUNT"].sum()
    is_cancel = receipts["CANCELTYPE"] == "1"
    expected_count = (
        receipts.assign(NET=(~is_cancel).astype(int) - is_cancel.astype(int))
        .groupby(key)["NET"]
        .sum()
    )
    expected_qty = items.groupby(key)["QTY"].sum()

    indexed = mart.set_index(key).sort_index()

    pd.testing.assert_series_equal(
        indexed["SALE_AMT"], expected_amount.sort_index(), check_names=False
    )
    pd.testing.assert_series_equal(
        indexed["DEAL_CNT"], expected_count.sort_index(), check_names=False, check_dtype=False
    )
    pd.testing.assert_series_equal(
        indexed["ITEM_QTY"], expected_qty.sort_index(), check_names=False
    )


def test_avg_ticket_is_amount_over_count(loaded_engine: Engine) -> None:
    """명세 7.1: AVG_TICKET = SALE_AMT / DEAL_CNT."""
    mart = _read(loaded_engine, schema.MART_DAY_STORE)

    assert (mart["DEAL_CNT"] > 0).all(), "표본에 거래 0건인 날이 없어야 이 검사가 의미 있다"
    expected = mart["SALE_AMT"] / mart["DEAL_CNT"]

    pd.testing.assert_series_equal(mart["AVG_TICKET"], expected, check_names=False)


def test_avg_ticket_guards_zero_division() -> None:
    """명세 7.1: 거래 0건인 날에도 0으로 나누지 않는다."""
    assert aggregate.safe_avg_ticket(0, 0) == 0.0
    assert aggregate.safe_avg_ticket(1000, 0) == 0.0
    assert aggregate.safe_avg_ticket(1000, 4) == 250.0


def test_mart_hour_store_uses_saletime_prefix(loaded_engine: Engine) -> None:
    """명세 7.1: 시간대는 SALETIME 앞 2자리 기준이다."""
    receipts, _ = _read_facts(loaded_engine)
    mart = _read(loaded_engine, schema.MART_HOUR_STORE)

    expected = (
        receipts.assign(HOUR=receipts["SALETIME"].str[:2])
        .groupby(["SALEDATE", "DEPT_CD", "HOUR"])["DEALAMOUNT"]
        .sum()
        .sort_index()
    )
    actual = mart.set_index(["SALEDATE", "DEPT_CD", "HOUR"])["SALE_AMT"].sort_index()

    pd.testing.assert_series_equal(actual, expected, check_names=False)


def test_hour_mart_sums_to_day_mart(loaded_engine: Engine) -> None:
    """시간대 마트의 합이 일 마트와 정확히 같다 (화면 차트와 요약이 어긋나지 않게)."""
    day = _read(loaded_engine, schema.MART_DAY_STORE)
    hour = _read(loaded_engine, schema.MART_HOUR_STORE)

    key = ["SALEDATE", "DEPT_CD"]
    rolled = hour.groupby(key)[["SALE_AMT", "DEAL_CNT"]].sum().sort_index()
    expected = day.set_index(key)[["SALE_AMT", "DEAL_CNT"]].sort_index()

    pd.testing.assert_frame_equal(rolled, expected)


def test_mart_day_store_item_comes_from_items_only(loaded_engine: Engine) -> None:
    """명세 7.1: 상품 마트는 ITEM에서만 만든다 (PAYMENT와 조인 금지)."""
    _, items = _read_facts(loaded_engine)
    mart = _read(loaded_engine, schema.MART_DAY_STORE_ITEM)

    key = ["SALEDATE", "DEPT_CD", "PLU_CD"]
    expected = items.groupby(key)[["SALEAMOUNT", "QTY"]].sum().sort_index()
    actual = (
        mart.set_index(key)[["SALE_AMT", "QTY"]]
        .rename(columns={"SALE_AMT": "SALEAMOUNT"})
        .sort_index()
    )

    pd.testing.assert_frame_equal(actual, expected)


def test_item_mart_sums_to_day_mart(loaded_engine: Engine) -> None:
    """상품 마트의 매출 합이 일 마트와 같다 (취소가 상품 단위로도 상계된다)."""
    day = _read(loaded_engine, schema.MART_DAY_STORE)
    item = _read(loaded_engine, schema.MART_DAY_STORE_ITEM)

    key = ["SALEDATE", "DEPT_CD"]
    rolled = item.groupby(key)["SALE_AMT"].sum().sort_index()
    expected = day.set_index(key)["SALE_AMT"].sort_index()

    pd.testing.assert_series_equal(rolled, expected, check_names=False)


def test_cancel_is_netted_in_marts(loaded_engine: Engine) -> None:
    """취소가 마트에서 상계된다 — 거래 건수가 원장 행수보다 적다."""
    receipts, _ = _read_facts(loaded_engine)
    mart = _read(loaded_engine, schema.MART_DAY_STORE)

    assert (receipts["CANCELTYPE"] == "1").any(), "표본에 취소가 없다"
    assert mart["DEAL_CNT"].sum() < len(receipts)


# --- 멱등 -----------------------------------------------------------------


def test_build_marts_is_idempotent(tmp_path: Path) -> None:
    """마트를 두 번 계산해도 결과가 같다 (행이 늘지 않는다)."""
    engine = get_engine(tmp_path / "idem.db")
    pipeline.load_period(SampleExtractor(dept_cds=["901003"]), FROM_DATE, TO_DATE, engine=engine)

    aggregate.build_marts(engine, FROM_DATE, TO_DATE)
    first = _read(engine, schema.MART_DAY_STORE).sort_values(["SALEDATE", "DEPT_CD"])

    aggregate.build_marts(engine, FROM_DATE, TO_DATE)
    second = _read(engine, schema.MART_DAY_STORE).sort_values(["SALEDATE", "DEPT_CD"])

    pd.testing.assert_frame_equal(
        first.reset_index(drop=True), second.reset_index(drop=True)
    )


def test_build_marts_only_touches_target_period(tmp_path: Path) -> None:
    """구간 재계산이 기간 밖 마트를 건드리지 않는다."""
    engine = get_engine(tmp_path / "scope.db")
    pipeline.load_period(SampleExtractor(dept_cds=["901003"]), FROM_DATE, TO_DATE, engine=engine)
    aggregate.build_marts(engine, FROM_DATE, TO_DATE)

    before = _read(engine, schema.MART_DAY_STORE)
    outside_before = before[before["SALEDATE"] > "20260703"].sort_values("SALEDATE")

    aggregate.build_marts(engine, FROM_DATE, "20260703")

    after = _read(engine, schema.MART_DAY_STORE)
    outside_after = after[after["SALEDATE"] > "20260703"].sort_values("SALEDATE")

    assert not outside_before.empty
    pd.testing.assert_frame_equal(
        outside_before.reset_index(drop=True), outside_after.reset_index(drop=True)
    )


def test_marts_have_exact_ddl_columns(loaded_engine: Engine) -> None:
    """마트 3종의 컬럼이 명세 4장 DDL과 이름·순서까지 같다 (불변식 5)."""
    for table in (
        schema.MART_DAY_STORE,
        schema.MART_HOUR_STORE,
        schema.MART_DAY_STORE_ITEM,
    ):
        frame = _read(loaded_engine, table)
        assert list(frame.columns) == [column.name for column in table.columns]
        assert not frame.empty
