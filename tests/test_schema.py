"""스키마 정의 검증 — 명세 4장 DDL과 절대 불변식 5·6을 지키는지 확인한다.

이 테스트는 명세 4장 DDL의 전사본을 기대값으로 들고 있다.
`schema.py`를 고쳐 테스트가 깨지면, 고친 쪽이 틀린 것이다 (불변식 5: 실컬럼명 동결).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, inspect

from src.load import schema

# --- 명세 4장 DDL 전사 (기대값) -------------------------------------------------
# {테이블명: (컬럼명 튜플, PK 컬럼명 튜플)}
EXPECTED_TABLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "FACT_RECEIPT": (
        (
            "DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SALETIME", "DEALTYPE",
            "ITEMCNT", "TENDERCNT", "DEALAMOUNT", "CANCELTYPE",
            "ORGSALEDATE", "ORGPOSNO", "ORGDEALNO",
        ),
        ("DEPT_CD", "SALEDATE", "POSNO", "DEALNO"),
    ),
    "FACT_RECEIPT_ITEM": (
        (
            "DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SEQ",
            "PLU_CD", "GOODS_NM", "ITEM_HEAD", "ITEM_HEAD_NM",
            "SALEPRICE", "QTY", "SALEAMOUNT",
        ),
        ("DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SEQ"),
    ),
    "FACT_PAYMENT": (
        (
            "DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SEQ",
            "TENDERSECTION", "TENDERAMOUNT",
        ),
        ("DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SEQ"),
    ),
    "DIM_STORE": (
        ("DEPT_CD", "DEPT_NM", "SIZE_GRADE"),
        ("DEPT_CD",),
    ),
    "MART_DAY_STORE": (
        ("SALEDATE", "DEPT_CD", "SALE_AMT", "DEAL_CNT", "ITEM_QTY", "AVG_TICKET"),
        ("SALEDATE", "DEPT_CD"),
    ),
    "MART_HOUR_STORE": (
        ("SALEDATE", "DEPT_CD", "HOUR", "SALE_AMT", "DEAL_CNT"),
        ("SALEDATE", "DEPT_CD", "HOUR"),
    ),
    "MART_DAY_STORE_ITEM": (
        (
            "SALEDATE", "DEPT_CD", "PLU_CD", "GOODS_NM", "ITEM_HEAD_NM",
            "SALE_AMT", "QTY",
        ),
        ("SALEDATE", "DEPT_CD", "PLU_CD"),
    ),
    "BRIEFING_DAILY": (
        ("SALEDATE", "DEPT_CD", "PAYLOAD_JSON"),
        ("SALEDATE", "DEPT_CD"),
    ),
    "FEEDBACK_LOG": (
        ("TS", "SALEDATE", "DEPT_CD", "CARD_ID", "ACTION"),
        (),  # 명세 4장 DDL에 PRIMARY KEY 절이 없다 — 추가 로그이므로 중복을 허용한다
    ),
    "ETL_WATERMARK": (
        ("SOURCE", "LAST_LOADED_AT"),
        ("SOURCE",),
    ),
}

# 불변식 6: 개인정보성 컬럼은 스키마에도 없어야 한다.
# 컬럼명에 이 조각이 들어가면 개인정보 컬럼으로 간주한다.
FORBIDDEN_COLUMN_FRAGMENTS: tuple[str, ...] = (
    "CARD_NO", "CARDNO", "CARDNUM",
    "MEMBER", "MBR", "CUST", "CUSTOMER",
    "TEL", "PHONE", "HP_NO", "MOBILE",
    "EMAIL", "ADDR", "BIRTH", "SSN", "RRN",
    "NAME_KOR", "USER_NM", "CUST_NM",
)


@pytest.fixture()
def engine(tmp_path: Path) -> Engine:
    """테스트 전용 SQLite 파일 엔진을 만든다."""
    return create_engine(f"sqlite:///{tmp_path / 'test.db'}")


def test_schema_creates_all_tables(engine: Engine) -> None:
    """명세 10장: DDL의 전 테이블이 생성된다."""
    schema.create_all(engine)

    actual = set(inspect(engine).get_table_names())
    expected = set(EXPECTED_TABLES)

    assert expected <= actual, f"누락된 테이블: {sorted(expected - actual)}"
    assert actual <= expected, f"명세에 없는 테이블: {sorted(actual - expected)}"


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_schema_column_names_frozen(engine: Engine, table_name: str) -> None:
    """불변식 5: 컬럼명·순서가 명세 4장 DDL과 정확히 일치한다."""
    schema.create_all(engine)

    expected_cols, _ = EXPECTED_TABLES[table_name]
    actual_cols = tuple(c["name"] for c in inspect(engine).get_columns(table_name))

    assert actual_cols == expected_cols


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_schema_primary_keys(engine: Engine, table_name: str) -> None:
    """명세 4장 DDL의 PRIMARY KEY 구성이 그대로 반영된다."""
    schema.create_all(engine)

    _, expected_pk = EXPECTED_TABLES[table_name]
    actual_pk = tuple(inspect(engine).get_pk_constraint(table_name)["constrained_columns"])

    assert actual_pk == expected_pk


def test_schema_no_personal_columns(engine: Engine) -> None:
    """불변식 6: 카드번호·회원번호류 개인정보 컬럼이 스키마에 존재하지 않는다."""
    schema.create_all(engine)

    inspector = inspect(engine)
    offenders: list[str] = [
        f"{table}.{column['name']}"
        for table in inspector.get_table_names()
        for column in inspector.get_columns(table)
        if any(bad in column["name"].upper() for bad in FORBIDDEN_COLUMN_FRAGMENTS)
    ]

    assert offenders == [], f"개인정보성 컬럼 발견: {offenders}"


def test_schema_create_all_is_repeatable(engine: Engine) -> None:
    """create_all을 두 번 호출해도 예외가 나지 않는다 (CREATE TABLE IF NOT EXISTS 의미)."""
    schema.create_all(engine)
    schema.create_all(engine)

    assert set(inspect(engine).get_table_names()) == set(EXPECTED_TABLES)


def test_schema_exposes_table_objects() -> None:
    """후속 모듈이 방언 독립으로 질의하도록 Core Table 객체를 노출한다 (명세 3장)."""
    for table_name in EXPECTED_TABLES:
        table = schema.metadata.tables.get(table_name)
        assert table is not None, f"metadata에 {table_name} 없음"
        assert getattr(schema, table_name, None) is table, (
            f"schema.{table_name} 모듈 속성이 metadata의 Table과 다르다"
        )
