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

#: 기준일의 재고 상태 스냅샷 (부록 A.2).
#: 재고 6개 컬럼은 기간계 TB_SBL202(매장 발주 기초데이터)의 실컬럼명을 그대로 쓴다.
#: 키 3개는 명세 4장이 확정한 이름을 따른다 — 다른 FACT·MART와 기간 조건을 통일하기 위해서다.
FACT_STOCK_SNAPSHOT = Table(
    "FACT_STOCK_SNAPSHOT",
    metadata,
    Column("SALEDATE", Text, nullable=False),
    Column("DEPT_CD", Text, nullable=False),
    Column("PLU_CD", Text, nullable=False),
    Column("GOODS_NM", Text, nullable=False),
    Column("ITEM_HEAD_NM", Text, nullable=False),
    Column("RUNNING_STOCK_QTY", Integer, nullable=False),  # 운영재고수량
    Column("IPGO_QTY", Integer, nullable=False),  # 입고예정수량
    Column("SALE_AVERAGE_QTY", Float, nullable=False),  # 매출평균수량(1일)
    Column("PROPER_STOCK_QTY", Integer, nullable=False),  # 적정재고수량
    Column("ADVICE_ORDER_QTY", Integer, nullable=False),  # 권고발주수량 — 표시 금지(부록 A.5)
    Column("LEAD_TM", Integer, nullable=False),  # 리드타임(일)
    PrimaryKeyConstraint("SALEDATE", "DEPT_CD", "PLU_CD"),
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

#: 그날 그 매장의 신호를 **집계 가능한 모양으로** 옮겨 적은 마트 (부록 B.13 결정 1).
#:
#: 진실의 원천은 여전히 ``BRIEFING_DAILY`` 의 카드다. 다만 "최근 30일 중 재고가
#: 며칠 부족했나"를 세려면 JSON을 전부 열어야 하는데, 매장이 1,300개면 화면을
#: 열 때마다 39,000건을 파싱하게 된다. 컬럼으로 두면 GROUP BY 한 번으로 끝난다.
MART_DAY_STORE_SIGNAL = Table(
    "MART_DAY_STORE_SIGNAL",
    metadata,
    Column("SALEDATE", Text, nullable=False),
    Column("DEPT_CD", Text, nullable=False),
    Column("STATUS", Text, nullable=False),  # STOCK · PEAK · CALM (부록 B.5)
    Column("RISK_COUNT", Integer, nullable=False),
    PrimaryKeyConstraint("SALEDATE", "DEPT_CD"),
)

#: 여러 매장을 함께 보는 관리자 화면이 읽는 요약 (부록 B.3).
#: 점포 단위가 아니므로 ``DEPT_CD`` 가 없다 — 날짜 하나에 행 하나다.
#: 합계도 배치가 만들어 여기 저장한다. 화면이 매장을 더하면 그 합계는
#: "화면이 만든 숫자"가 되어 불변식 1·7을 깬다 (부록 B.2).
BRIEFING_DAILY_GROUP = Table(
    "BRIEFING_DAILY_GROUP",
    metadata,
    Column("SALEDATE", Text, nullable=False),
    Column("PAYLOAD_JSON", Text, nullable=False),  # 부록 B.5 그룹 요약 JSON
    PrimaryKeyConstraint("SALEDATE"),
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
