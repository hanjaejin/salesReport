"""SampleExtractor 검증 — 명세 5장 Extractor 계약과 청크 규약.

청크 규약이 깨지면 실데이터(연 1.2억 행)에서 메모리가 터진다.
데모 데이터가 작아도 규약 자체를 테스트로 고정한다.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.extract.base import ReceiptExtractor
from src.extract.sample import SampleExtractor
from src.generate import synth
from src.load import schema

FROM_DATE = "20260701"
TO_DATE = "20260707"


@pytest.fixture()
def extractor() -> SampleExtractor:
    """기본 설정(점포 3곳 전부)의 Extractor."""
    return SampleExtractor()


def test_sample_extractor_fulfills_contract(extractor: SampleExtractor) -> None:
    """명세 5장 계약을 실제로 구현한다."""
    assert isinstance(extractor, ReceiptExtractor)


def test_chunks_never_exceed_chunk_size() -> None:
    """어떤 청크도 지정한 크기를 넘지 않는다 (명세 5장: 50,000행)."""
    extractor = SampleExtractor(chunk_size=300)

    chunks = list(extractor.extract_items(FROM_DATE, TO_DATE))

    assert len(chunks) > 1, "청크가 쪼개지지 않아 규약을 검증할 수 없다"
    assert all(len(chunk) <= 300 for chunk in chunks)
    # 마지막을 뺀 모든 청크는 정확히 가득 차 있어야 한다 (조각내기 낭비 방지)
    assert all(len(chunk) == 300 for chunk in chunks[:-1])


def test_chunks_reassemble_to_full_period() -> None:
    """청크를 다시 이으면 기간 전체 데이터와 같다 (분할이 데이터를 잃지 않는다)."""
    whole = pd.concat(
        list(SampleExtractor(chunk_size=10**9).extract_receipts(FROM_DATE, TO_DATE)),
        ignore_index=True,
    )
    split = pd.concat(
        list(SampleExtractor(chunk_size=137).extract_receipts(FROM_DATE, TO_DATE)),
        ignore_index=True,
    )

    pd.testing.assert_frame_equal(whole, split)


@pytest.mark.parametrize(
    ("method_name", "table"),
    [
        ("extract_receipts", schema.FACT_RECEIPT),
        ("extract_items", schema.FACT_RECEIPT_ITEM),
        ("extract_payments", schema.FACT_PAYMENT),
    ],
)
def test_chunk_columns_match_ddl(
    extractor: SampleExtractor, method_name: str, table: object
) -> None:
    """모든 청크의 컬럼이 명세 4장 DDL과 이름·순서까지 같다 (불변식 5)."""
    expected = [column.name for column in table.columns]  # type: ignore[attr-defined]

    for chunk in getattr(extractor, method_name)(FROM_DATE, FROM_DATE):
        assert list(chunk.columns) == expected


def test_extract_covers_exact_date_range(extractor: SampleExtractor) -> None:
    """기간 양끝을 포함하고 밖의 날짜는 내보내지 않는다."""
    receipts = pd.concat(list(extractor.extract_receipts(FROM_DATE, TO_DATE)), ignore_index=True)

    dates = set(receipts["SALEDATE"])
    assert dates == {f"202607{day:02d}" for day in range(1, 8)}


def test_extract_filters_stores() -> None:
    """지정한 점포만 내보낸다 (CLI --stores 대비)."""
    extractor = SampleExtractor(dept_cds=["901003"])

    receipts = pd.concat(list(extractor.extract_receipts(FROM_DATE, TO_DATE)), ignore_index=True)

    assert set(receipts["DEPT_CD"]) == {"901003"}
    assert set(extractor.extract_stores()["DEPT_CD"]) == {"901003"}


def test_unknown_store_is_rejected() -> None:
    """알 수 없는 점포코드는 조용히 무시하지 않고 실패한다."""
    extractor = SampleExtractor(dept_cds=["999999"])

    with pytest.raises(ValueError, match="알 수 없는 점포코드"):
        list(extractor.extract_receipts(FROM_DATE, FROM_DATE))


def test_invalid_chunk_size_is_rejected() -> None:
    """청크 크기 0 이하는 만들어질 때 바로 실패한다."""
    with pytest.raises(ValueError, match="chunk_size"):
        SampleExtractor(chunk_size=0)


def test_empty_range_yields_nothing(extractor: SampleExtractor) -> None:
    """시작일이 종료일보다 뒤면 아무것도 흘리지 않는다 (예외가 아니라 빈 결과)."""
    assert list(extractor.extract_receipts("20260702", "20260701")) == []


def test_extract_is_repeatable(extractor: SampleExtractor) -> None:
    """같은 기간을 두 번 뽑으면 결과가 같다 (불변식 4 · 멱등의 전제)."""
    first = pd.concat(list(extractor.extract_items(FROM_DATE, FROM_DATE)), ignore_index=True)
    second = pd.concat(list(extractor.extract_items(FROM_DATE, FROM_DATE)), ignore_index=True)

    pd.testing.assert_frame_equal(first, second)


def test_day_cache_avoids_regenerating_same_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """날짜별로 세 메서드를 부르면 하루를 한 번만 생성한다."""
    calls: list[tuple[str, str]] = []
    original = synth.generate_day

    def counting_generate_day(store: synth.StoreProfile, saledate: str) -> synth.DayData:
        calls.append((store.dept_cd, saledate))
        return original(store, saledate)

    monkeypatch.setattr(synth, "generate_day", counting_generate_day)

    extractor = SampleExtractor(dept_cds=["901003"])
    for method in ("extract_receipts", "extract_items", "extract_payments"):
        list(getattr(extractor, method)(FROM_DATE, FROM_DATE))

    assert calls == [("901003", FROM_DATE)], f"하루를 {len(calls)}번 생성했다"


def test_stores_frame_matches_ddl(extractor: SampleExtractor) -> None:
    """점포 마스터 프레임이 DIM_STORE DDL과 일치한다."""
    frame = extractor.extract_stores()

    assert list(frame.columns) == [column.name for column in schema.DIM_STORE.columns]
    assert len(frame) == len(synth.STORES)
