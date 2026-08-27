"""합성 데이터 Extractor — 명세 5장 ``SampleExtractor``.

``synth.py`` 가 만든 하루치를 청크(기본 50,000행)로 잘라 흘린다.
파이프라인은 이 클래스가 합성인지 Oracle인지 모른 채 기간만 넘긴다.

**일자 캐시**: 세 메서드(receipts·items·payments)가 같은 하루를 각각 요구하므로,
캐시가 없으면 하루를 세 번 생성하게 된다. 흐름도 FLOW 04의 "loop 날짜별"처럼
파이프라인이 날짜 단위로 세 메서드를 부르면, 작은 캐시 하나로 생성이 1회로 준다.
생성은 결정적이라(불변식 4) 캐시가 있든 없든 결과는 같다 — 속도만 다르다.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator

import pandas as pd

from src.common.config import CHUNK_SIZE
from src.common.dateutil import date_range
from src.extract.base import ReceiptExtractor
from src.generate import synth

#: 캐시에 담아 둘 하루치 개수. 점포 수의 2배면 날짜별 루프에서 전부 적중한다.
_DAY_CACHE_SIZE = len(synth.STORES) * 2


class SampleExtractor(ReceiptExtractor):
    """합성 생성기를 원천으로 쓰는 Extractor (명세 5장).

    Args:
        dept_cds: 대상 점포코드. None이면 명세 6.1의 3곳 전부.
        chunk_size: 한 번에 흘릴 최대 행수 (명세 5장: 50,000행).

    Raises:
        ValueError: ``chunk_size`` 가 1 미만일 때.
    """

    def __init__(
        self, dept_cds: Iterable[str] | None = None, chunk_size: int = CHUNK_SIZE
    ) -> None:
        if chunk_size < 1:
            raise ValueError(f"chunk_size는 1 이상이어야 합니다: {chunk_size}")

        self._dept_cds: list[str] | None = list(dept_cds) if dept_cds is not None else None
        self._chunk_size = chunk_size
        self._cache: OrderedDict[tuple[str, str], synth.DayData] = OrderedDict()

    # --- 계약 구현 --------------------------------------------------------

    def extract_receipts(self, from_date: str, to_date: str) -> Iterator[pd.DataFrame]:
        """기간의 영수증 헤더를 청크로 흘린다.

        Args:
            from_date: 시작일 ``YYYYMMDD`` (포함).
            to_date: 종료일 ``YYYYMMDD`` (포함).

        Yields:
            ``FACT_RECEIPT`` 컬럼을 가진 DataFrame 청크.
        """
        yield from self._stream(from_date, to_date, lambda day: day.receipts)

    def extract_items(self, from_date: str, to_date: str) -> Iterator[pd.DataFrame]:
        """기간의 상품 명세를 청크로 흘린다.

        Args:
            from_date: 시작일 ``YYYYMMDD`` (포함).
            to_date: 종료일 ``YYYYMMDD`` (포함).

        Yields:
            ``FACT_RECEIPT_ITEM`` 컬럼을 가진 DataFrame 청크.
        """
        yield from self._stream(from_date, to_date, lambda day: day.items)

    def extract_payments(self, from_date: str, to_date: str) -> Iterator[pd.DataFrame]:
        """기간의 결제 내역을 청크로 흘린다.

        Args:
            from_date: 시작일 ``YYYYMMDD`` (포함).
            to_date: 종료일 ``YYYYMMDD`` (포함).

        Yields:
            ``FACT_PAYMENT`` 컬럼을 가진 DataFrame 청크.
        """
        yield from self._stream(from_date, to_date, lambda day: day.payments)

    # --- 부가 ------------------------------------------------------------

    def extract_stock(self, from_date: str, to_date: str) -> Iterator[pd.DataFrame]:
        """기간의 재고 스냅샷을 청크로 흘린다 (부록 A.2).

        계약(``ReceiptExtractor``)에 없는 부가 메서드다. Oracle 연동 시 재고는
        ``TB_SBL202``(매장 발주 기초데이터)에서 오므로 영수증과 원천이 다르다.

        Args:
            from_date: 시작일 ``YYYYMMDD`` (포함).
            to_date: 종료일 ``YYYYMMDD`` (포함).

        Yields:
            ``FACT_STOCK_SNAPSHOT`` 컬럼을 가진 DataFrame 청크.
        """
        stores = self._target_stores()
        buffer: list[pd.DataFrame] = []
        buffered_rows = 0

        for saledate in date_range(from_date, to_date):
            for store in stores:
                frame = synth.generate_stock_snapshot(store, saledate)
                buffer.append(frame)
                buffered_rows += len(frame)

                while buffered_rows >= self._chunk_size:
                    merged = pd.concat(buffer, ignore_index=True)
                    yield merged.iloc[: self._chunk_size].reset_index(drop=True)
                    leftover = merged.iloc[self._chunk_size :].reset_index(drop=True)
                    buffer = [leftover] if not leftover.empty else []
                    buffered_rows = len(leftover)

        if buffer:
            yield pd.concat(buffer, ignore_index=True)

    def extract_stores(self) -> pd.DataFrame:
        """점포 마스터를 돌려준다 (``DIM_STORE`` 적재용).

        계약(``ReceiptExtractor``)에는 없는 부가 메서드다. Oracle 연동 시
        점포 마스터는 기간계 ``TB_HBM001`` 에서 오므로 원천이 다르다.

        Returns:
            ``DIM_STORE`` DDL 컬럼 순서의 프레임. 대상 점포만 포함한다.
        """
        frame = synth.store_dim_frame()
        if self._dept_cds is None:
            return frame
        return frame[frame["DEPT_CD"].isin(self._dept_cds)].reset_index(drop=True)

    # --- 내부 ------------------------------------------------------------

    def _day(self, dept_cd: str, saledate: str) -> synth.DayData:
        """하루치를 캐시에서 꺼내거나 새로 만든다.

        Args:
            dept_cd: 점포코드.
            saledate: ``YYYYMMDD``.

        Returns:
            하루치 ``DayData``.
        """
        key = (dept_cd, saledate)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        store = next(store for store in synth.STORES if store.dept_cd == dept_cd)
        day = synth.generate_day(store, saledate)

        self._cache[key] = day
        if len(self._cache) > _DAY_CACHE_SIZE:
            self._cache.popitem(last=False)
        return day

    def _target_stores(self) -> tuple[synth.StoreProfile, ...]:
        """대상 점포 목록을 정한다.

        Returns:
            생성 대상 점포 프로파일 튜플.

        Raises:
            ValueError: 알 수 없는 점포코드가 지정됐을 때.
        """
        if self._dept_cds is None:
            return synth.STORES

        known = {store.dept_cd for store in synth.STORES}
        unknown = sorted(set(self._dept_cds) - known)
        if unknown:
            raise ValueError(f"알 수 없는 점포코드: {unknown} (가능: {sorted(known)})")
        return tuple(store for store in synth.STORES if store.dept_cd in set(self._dept_cds))

    def _stream(
        self,
        from_date: str,
        to_date: str,
        pick: Callable[[synth.DayData], pd.DataFrame],
    ) -> Iterator[pd.DataFrame]:
        """날짜×점포를 돌며 원하는 프레임을 청크 크기로 잘라 흘린다.

        Args:
            from_date: 시작일 ``YYYYMMDD``.
            to_date: 종료일 ``YYYYMMDD``.
            pick: ``DayData`` 에서 흘릴 프레임을 고르는 함수.

        Yields:
            최대 ``chunk_size`` 행의 DataFrame. 마지막 청크만 더 작을 수 있다.
        """
        stores = self._target_stores()
        buffer: list[pd.DataFrame] = []
        buffered_rows = 0

        for saledate in date_range(from_date, to_date):
            for store in stores:
                frame = pick(self._day(store.dept_cd, saledate))
                if frame.empty:
                    continue

                buffer.append(frame)
                buffered_rows += len(frame)

                while buffered_rows >= self._chunk_size:
                    merged = pd.concat(buffer, ignore_index=True)
                    yield merged.iloc[: self._chunk_size].reset_index(drop=True)

                    leftover = merged.iloc[self._chunk_size :].reset_index(drop=True)
                    buffer = [leftover] if not leftover.empty else []
                    buffered_rows = len(leftover)

        if buffer:
            yield pd.concat(buffer, ignore_index=True)
