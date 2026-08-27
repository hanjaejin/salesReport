"""Oracle Extractor 스텁 — 데모 범위 밖의 "미래의 소켓" (명세 5장 · 흐름도 FLOW 03).

여기는 **비워 두는 것이 설계**다. 계약(`ReceiptExtractor`)만 지키면
`SampleExtractor` 자리에 이 클래스를 꽂는 것으로 기간계 연동이 끝나도록,
파이프라인 쪽에 원천 종류가 새어 나가지 않게 막아 두는 자리다.

구현하지 않은 채 계약만 선언해 두는 이유는 명세 5장이 요구한 그대로다:
*"클래스 선언 + NotImplementedError + docstring에 향후 구현 항목 기재만."*
"""

from __future__ import annotations

from collections.abc import Iterator

import pandas as pd

from src.extract.base import ReceiptExtractor

_NOT_IMPLEMENTED_MSG = (
    "OracleExtractor는 데모 범위 밖입니다. "
    "기간계 연동 단계에서 구현하세요 (모듈 docstring의 구현 항목 참조)."
)


class OracleExtractor(ReceiptExtractor):
    """기간계 Oracle에서 영수증·상품·결제를 읽어 오는 Extractor (미구현).

    향후 구현 시 다뤄야 할 항목:

    1. **파티션 SELECT**: ``TB_POD208``/``207``/``210`` 은 ``SALEDATE`` 기준
       파티션이므로, 기간 조건을 파티션 프루닝이 걸리는 형태로 작성한다.
       ``SELECT *`` 금지 — 명세 4장 DDL이 요구하는 컬럼만 명시한다.
    2. **fetch size**: 커서 왕복을 줄이도록 ``arraysize`` 를 청크 크기
       (``config.CHUNK_SIZE`` = 50,000)에 맞춘다.
    3. **스로틀**: 기간계는 운영 DB다. 청크 사이에 대기를 넣어 야간 배치가
       업무 시간대 조회를 밀어내지 않게 한다.
    4. **문자셋·자료형**: ``NUMBER`` → ``int`` 변환에서 유실이 없는지,
       한글 컬럼이 ``NVARCHAR2`` 인지 확인한다.
    5. **워터마크**: ``ETL_WATERMARK`` 테이블에 마지막 적재 시각을 남겨
       증분 적재로 전환할 수 있게 한다 (데모에서는 미사용 자리).
    6. **익일 취소**: 데모는 동일자 취소만 다룬다(명세 6.5). 실데이터는
       익일 이후 취소가 있으므로 안전 중첩 7일 재생성이 필요하다
       (정식 설계서 v1.3의 영역).
    """

    def extract_receipts(self, from_date: str, to_date: str) -> Iterator[pd.DataFrame]:
        """미구현.

        Args:
            from_date: 시작일 ``YYYYMMDD``.
            to_date: 종료일 ``YYYYMMDD``.

        Raises:
            NotImplementedError: 항상.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def extract_items(self, from_date: str, to_date: str) -> Iterator[pd.DataFrame]:
        """미구현.

        Args:
            from_date: 시작일 ``YYYYMMDD``.
            to_date: 종료일 ``YYYYMMDD``.

        Raises:
            NotImplementedError: 항상.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def extract_payments(self, from_date: str, to_date: str) -> Iterator[pd.DataFrame]:
        """미구현.

        Args:
            from_date: 시작일 ``YYYYMMDD``.
            to_date: 종료일 ``YYYYMMDD``.

        Raises:
            NotImplementedError: 항상.
        """
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)
