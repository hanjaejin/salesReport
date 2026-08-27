"""Extractor 계약 — Oracle 교체 지점 (명세 5장).

이 인터페이스가 고정돼 있으면 기간계 연동은 "재작성"이 아니라 "부품 교체"가 된다.
`pipeline.load_period()` 는 구현체가 무엇인지 모른 채 기간만 넘긴다.

세 메서드가 반환하는 DataFrame의 컬럼은 명세 4장 DDL의 실컬럼명과 같아야 한다
(불변식 5). 파이프라인은 컬럼명을 그대로 INSERT에 쓴다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

import pandas as pd


class ReceiptExtractor(ABC):
    """원천에서 기간 단위 데이터를 청크(DataFrame) 반복자로 제공한다.

    Note:
        전량을 한 번에 반환하지 않고 청크로 흘리는 것이 계약의 핵심이다.
        데모 데이터는 작지만, 실데이터(연 1.2억 행)에서 터지는 코드를
        처음부터 만들지 않기 위해서다 (흐름도 FLOW 04).
    """

    @abstractmethod
    def extract_receipts(self, from_date: str, to_date: str) -> Iterator[pd.DataFrame]:
        """기간의 영수증 헤더를 청크로 흘린다.

        Args:
            from_date: 시작일 ``YYYYMMDD`` (포함).
            to_date: 종료일 ``YYYYMMDD`` (포함).

        Yields:
            ``FACT_RECEIPT`` 컬럼을 가진 DataFrame 청크.
        """

    @abstractmethod
    def extract_items(self, from_date: str, to_date: str) -> Iterator[pd.DataFrame]:
        """기간의 상품 명세를 청크로 흘린다.

        Args:
            from_date: 시작일 ``YYYYMMDD`` (포함).
            to_date: 종료일 ``YYYYMMDD`` (포함).

        Yields:
            ``FACT_RECEIPT_ITEM`` 컬럼을 가진 DataFrame 청크.
        """

    @abstractmethod
    def extract_payments(self, from_date: str, to_date: str) -> Iterator[pd.DataFrame]:
        """기간의 결제 내역을 청크로 흘린다.

        Args:
            from_date: 시작일 ``YYYYMMDD`` (포함).
            to_date: 종료일 ``YYYYMMDD`` (포함).

        Yields:
            ``FACT_PAYMENT`` 컬럼을 가진 DataFrame 청크.
        """
