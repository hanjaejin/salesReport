"""SQLite 스키마 정의 — 명세 4장 DDL의 전사본 (불변식 5: 실컬럼명 동결).

여기의 테이블·컬럼명은 기간계 원본(TB_POD208/207/210)과 같은 이름이다.
**한 글자도 바꾸지 않는다.** 이름이 같아야 Oracle 데이터가 나중에 그대로 들어온다.

SQLAlchemy Core ``Table`` 객체로 정의하는 이유는 후속 모듈(pipeline·aggregate)이
방언 종속 SQL 없이 질의를 조립하게 하기 위해서다 (명세 3장: PostgreSQL 교체 대비).

개인정보성 컬럼(카드번호·회원번호류)은 **스키마에도 넣지 않는다** (불변식 6).
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import (
    Column,
    Engine,
    Float,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
)

metadata = MetaData()

# --- 원장 3종 (grain 분리 — 불변식 2) --------------------------------------

#: 영수증 헤더. 1행 = 영수증 1건.
FACT_RECEIPT = Table(
    "FACT_RECEIPT",
    metadata,
    Column("DEPT_CD", Text, nullable=False),  # 점포코드
    Column("SALEDATE", Text, nullable=False),  # YYYYMMDD
    Column("POSNO", Text, nullable=False),  # '1'~'9'
    Column("DEALNO", Text, nullable=False),  # '0001'~
    Column("SALETIME", Text, nullable=False),  # HHMMSS
    Column("DEALTYPE", Text, nullable=False),  # '0'=정상판매 (데모는 0만 집계)
    Column("ITEMCNT", Integer, nullable=False),
    Column("TENDERCNT", Integer, nullable=False),
    Column("DEALAMOUNT", Integer, nullable=False),  # 상품 SALEAMOUNT 합과 일치해야 함
    Column("CANCELTYPE", Text),  # NULL=정상, '1'=취소거래(음수 금액)
    Column("ORGSALEDATE", Text),
    Column("ORGPOSNO", Text),
    Column("ORGDEALNO", Text),
    PrimaryKeyConstraint("DEPT_CD", "SALEDATE", "POSNO", "DEALNO"),
)

#: 상품 명세. 1행 = 영수증의 상품 1줄.
FACT_RECEIPT_ITEM = Table(
    "FACT_RECEIPT_ITEM",
    metadata,
    Column("DEPT_CD", Text, nullable=False),
    Column("SALEDATE", Text, nullable=False),
    Column("POSNO", Text, nullable=False),
    Column("DEALNO", Text, nullable=False),
    Column("SEQ", Integer, nullable=False),
    Column("PLU_CD", Text, nullable=False),
    Column("GOODS_NM", Text, nullable=False),
    Column("ITEM_HEAD", Text, nullable=False),  # 대분류 코드
    Column("ITEM_HEAD_NM", Text, nullable=False),  # 대분류명
    Column("SALEPRICE", Integer, nullable=False),
    Column("QTY", Integer, nullable=False),
    Column("SALEAMOUNT", Integer, nullable=False),
    PrimaryKeyConstraint("DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SEQ"),
)

#: 결제 내역. 1행 = 결제수단 1건 (분할결제면 영수증당 여러 행).
FACT_PAYMENT = Table(
    "FACT_PAYMENT",
    metadata,
    Column("DEPT_CD", Text, nullable=False),
    Column("SALEDATE", Text, nullable=False),
    Column("POSNO", Text, nullable=False),
    Column("DEALNO", Text, nullable=False),
    Column("SEQ", Integer, nullable=False),
    Column("TENDERSECTION", Text, nullable=False),  # '01'현금 '02'카드 '03'간편결제
    Column("TENDERAMOUNT", Integer, nullable=False),
    PrimaryKeyConstraint("DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SEQ"),
)

# --- 차원 -----------------------------------------------------------------

#: 점포 마스터 (명세 6.1 · ADR-0003).
DIM_STORE = Table(
    "DIM_STORE",
    metadata,
    Column("DEPT_CD", Text, nullable=False),
    Column("DEPT_NM", Text, nullable=False),
    Column("SIZE_GRADE", Text, nullable=False),  # L/M/S
    PrimaryKeyConstraint("DEPT_CD"),
)

# --- 집계 마트 3종 (명세 7.1) ----------------------------------------------

#: 일 × 점포.
MART_DAY_STORE = Table(
    "MART_DAY_STORE",
    metadata,
    Column("SALEDATE", Text, nullable=False),
    Column("DEPT_CD", Text, nullable=False),
    Column("SALE_AMT", Integer, nullable=False),
    Column("DEAL_CNT", Integer, nullable=False),
    Column("ITEM_QTY", Integer, nullable=False),
    Column("AVG_TICKET", Float, nullable=False),
    PrimaryKeyConstraint("SALEDATE", "DEPT_CD"),
)

#: 시간대 × 점포. HOUR는 SALETIME 앞 2자리 ('05'~'23').
MART_HOUR_STORE = Table(
    "MART_HOUR_STORE",
    metadata,
    Column("SALEDATE", Text, nullable=False),
    Column("DEPT_CD", Text, nullable=False),
    Column("HOUR", Text, nullable=False),
    Column("SALE_AMT", Integer, nullable=False),
    Column("DEAL_CNT", Integer, nullable=False),
    PrimaryKeyConstraint("SALEDATE", "DEPT_CD", "HOUR"),
)

#: 일 × 점포 × 상품. ITEM에서만 만든다 (PAYMENT와 조인 금지 — 불변식 2).
MART_DAY_STORE_ITEM = Table(
    "MART_DAY_STORE_ITEM",
    metadata,
    Column("SALEDATE", Text, nullable=False),
    Column("DEPT_CD", Text, nullable=False),
    Column("PLU_CD", Text, nullable=False),
    Column("GOODS_NM", Text, nullable=False),
    Column("ITEM_HEAD_NM", Text, nullable=False),
    Column("SALE_AMT", Integer, nullable=False),
    Column("QTY", Integer, nullable=False),
    PrimaryKeyConstraint("SALEDATE", "DEPT_CD", "PLU_CD"),
)

# --- 브리핑·피드백 ---------------------------------------------------------

#: 배치가 만든 완성된 3줄과 계산 JSON (불변식 7: 서버측 생성).
BRIEFING_DAILY = Table(
    "BRIEFING_DAILY",
    metadata,
    Column("SALEDATE", Text, nullable=False),
    Column("DEPT_CD", Text, nullable=False),
    Column("PAYLOAD_JSON", Text, nullable=False),  # 명세 7.2 계산 JSON 전체
    PrimaryKeyConstraint("SALEDATE", "DEPT_CD"),
)

#: 화면의 [확인했어요]/[괜찮아요] 기록. 추가 전용 로그라 PK를 두지 않는다
#: (같은 사람이 같은 카드를 여러 번 눌러도 전부 남아야 채택률의 원료가 된다).
FEEDBACK_LOG = Table(
    "FEEDBACK_LOG",
    metadata,
    Column("TS", Text, nullable=False),
    Column("SALEDATE", Text, nullable=False),
    Column("DEPT_CD", Text, nullable=False),
    Column("CARD_ID", Text, nullable=False),
    Column("ACTION", Text, nullable=False),  # 'ACCEPT'/'DECLINE'
)

#: 데모 미사용. Oracle 증분 적재 대비 자리 (명세 4장).
ETL_WATERMARK = Table(
    "ETL_WATERMARK",
    metadata,
    Column("SOURCE", Text, nullable=False),
    Column("LAST_LOADED_AT", Text),
    PrimaryKeyConstraint("SOURCE"),
)


def create_all(engine: Engine) -> None:
    """정의된 전 테이블을 생성한다 (이미 있으면 건너뛴다).

    Args:
        engine: 대상 SQLAlchemy 엔진.

    Note:
        ``checkfirst=True`` 가 명세 DDL의 ``CREATE TABLE IF NOT EXISTS`` 에 해당한다.
        방언 종속 문법을 직접 쓰지 않고 Core에 맡긴다 (명세 3장).
    """
    metadata.create_all(engine, checkfirst=True)


def drop_all(engine: Engine) -> None:
    """정의된 전 테이블을 삭제한다 (테스트·초기화용).

    Args:
        engine: 대상 SQLAlchemy 엔진.
    """
    metadata.drop_all(engine, checkfirst=True)


def to_records(frame: pd.DataFrame) -> list[dict[str, object]]:
    """DataFrame을 Core INSERT가 바인딩할 수 있는 순수 파이썬 값으로 바꾼다.

    numpy 스칼라(``int64`` 등)는 SQLite 드라이버가 그대로 받지 못한다.
    ``Series.tolist()`` 가 컬럼 단위로 파이썬 기본형으로 되돌려 준다 (행 단위 반복 없음).
    결측(NaN/NaT/pd.NA)은 전부 ``None`` 으로 정규화한다 — TEXT 컬럼에 ``nan`` 문자열이
    들어가는 사고를 막기 위해서다.

    Args:
        frame: 변환할 프레임. 컬럼명이 대상 테이블과 같아야 한다.

    Returns:
        ``executemany`` 에 넘길 딕셔너리 리스트.
    """
    columns = list(frame.columns)
    cleaned = frame.astype(object).where(pd.notna(frame), None)
    column_values = [cleaned[column].tolist() for column in columns]
    return [dict(zip(columns, row, strict=True)) for row in zip(*column_values, strict=True)]
