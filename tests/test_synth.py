"""합성 데이터 생성기 검증 — 명세 6장 스펙과 10장 정합 테스트.

명세 6.5가 "정합성 필수 — 테스트로 검증"이라고 못 박은 규칙들을 여기서 지킨다.
숫자가 조금씩 틀어지는 합성 데이터는 데모 전체를 무너뜨리므로,
표본이 아니라 **생성된 전 행**을 검사한다.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.generate import synth

#: 검사에 쓰는 표본 날짜 — 요일·계절이 서로 다르게 흩어지도록 골랐다.
SAMPLE_DATES: tuple[str, ...] = (
    "20250701",  # 기간 첫날 (화)
    "20251225",  # 연말 (목)
    "20260222",  # 겨울 일요일
    "20260717",  # 여름 금요일
    "20260731",  # 기간 마지막날 (금)
)


@pytest.fixture(scope="module")
def sample_days() -> list[synth.DayData]:
    """점포 3곳 × 표본 날짜 전부를 생성한다 (모듈 1회)."""
    return [
        synth.generate_day(store, saledate)
        for store in synth.STORES
        for saledate in SAMPLE_DATES
    ]


@pytest.fixture(scope="module")
def merged(sample_days: list[synth.DayData]) -> synth.DayData:
    """표본 전체를 한 덩어리로 합친다."""
    return synth.DayData(
        receipts=pd.concat([day.receipts for day in sample_days], ignore_index=True),
        items=pd.concat([day.items for day in sample_days], ignore_index=True),
        payments=pd.concat([day.payments for day in sample_days], ignore_index=True),
    )


# --- 명세 6.3 프로파일 ---------------------------------------------------


def test_hour_profiles_sum_to_100() -> None:
    """명세 6.3: 각 등급 프로파일의 가중치 합이 100이다."""
    for grade, weights in synth.HOUR_PROFILES.items():
        assert len(weights) == len(synth.HOURS), f"{grade} 프로파일 길이 불일치"
        assert sum(weights) == pytest.approx(100.0), f"{grade} 합계 {sum(weights)}"


def test_hour_profiles_produce_expected_peak_blocks() -> None:
    """명세 6.3 기대 발동: L=아침 30%, M=점심 26%, S=최대 블록 25 미만."""
    share = synth.block_share_from_profile

    assert share("L", "아침") == pytest.approx(30.0)
    assert share("M", "점심") == pytest.approx(26.0)

    s_max = max(share("S", block) for block in synth.TIME_BLOCKS)
    assert s_max < 25.0, f"S 최대 블록 비중 {s_max} — G2가 발동해 침묵일이 사라진다"


def test_store_master_matches_spec() -> None:
    """명세 6.1: 점포 3곳의 코드·이름·등급·거래량·POS 대수 (ADR-0003)."""
    expected = {
        "901001": ("중앙역 대형점", "L", 800, 0.15, 3),
        "901002": ("동부역 중형점", "M", 300, 0.15, 2),
        "901003": ("간이역 소형점", "S", 80, 0.20, 1),
    }

    actual = {
        store.dept_cd: (
            store.dept_nm,
            store.size_grade,
            store.avg_deals,
            store.variation,
            store.pos_count,
        )
        for store in synth.STORES
    }

    assert actual == expected


# --- 명세 10장 정합 테스트 -------------------------------------------------


def test_synth_receipt_item_count_consistent(merged: synth.DayData) -> None:
    """명세 6.5: 전 영수증에서 ITEMCNT=ITEM 행수, TENDERCNT=PAYMENT 행수."""
    key = ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"]

    item_rows = merged.items.groupby(key).size().rename("ITEM_ROWS")
    payment_rows = merged.payments.groupby(key).size().rename("PAYMENT_ROWS")

    joined = merged.receipts.set_index(key).join([item_rows, payment_rows])

    assert joined["ITEM_ROWS"].notna().all(), "상품 행이 없는 영수증이 있다"
    assert joined["PAYMENT_ROWS"].notna().all(), "결제 행이 없는 영수증이 있다"
    assert (joined["ITEMCNT"] == joined["ITEM_ROWS"]).all()
    assert (joined["TENDERCNT"] == joined["PAYMENT_ROWS"]).all()


def test_synth_amount_consistent(merged: synth.DayData) -> None:
    """명세 6.5: DEALAMOUNT = Σ SALEAMOUNT = Σ TENDERAMOUNT (전 영수증)."""
    key = ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"]

    item_sum = merged.items.groupby(key)["SALEAMOUNT"].sum().rename("ITEM_SUM")
    payment_sum = merged.payments.groupby(key)["TENDERAMOUNT"].sum().rename("PAYMENT_SUM")

    joined = merged.receipts.set_index(key).join([item_sum, payment_sum])

    assert (joined["DEALAMOUNT"] == joined["ITEM_SUM"]).all()
    assert (joined["DEALAMOUNT"] == joined["PAYMENT_SUM"]).all()


def test_item_amount_is_price_times_qty(merged: synth.DayData) -> None:
    """상품 행의 SALEAMOUNT는 단가 × 수량이다 (할인 컬럼이 스키마에 없으므로)."""
    items = merged.items
    assert (items["SALEAMOUNT"] == items["SALEPRICE"] * items["QTY"]).all()


# --- 명세 6.5 취소 거래 ----------------------------------------------------


def test_cancel_rows_are_same_date_with_org_keys(merged: synth.DayData) -> None:
    """명세 6.5: 취소는 동일자이며 ORG* 3컬럼이 원거래 키를 가리킨다."""
    receipts = merged.receipts
    cancels = receipts[receipts["CANCELTYPE"] == "1"]
    normals = receipts[receipts["CANCELTYPE"].isna()]

    assert not cancels.empty, "표본에 취소 거래가 하나도 없다"

    # 동일자: 원거래 일자가 자기 일자와 같다
    assert (cancels["ORGSALEDATE"] == cancels["SALEDATE"]).all()
    assert cancels[["ORGSALEDATE", "ORGPOSNO", "ORGDEALNO"]].notna().all().all()

    # ORG* 가 실제로 존재하는 정상 거래를 가리킨다
    normal_keys = set(
        map(tuple, normals[["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"]].to_numpy())
    )
    cancel_targets = set(
        map(tuple, cancels[["DEPT_CD", "ORGSALEDATE", "ORGPOSNO", "ORGDEALNO"]].to_numpy())
    )
    assert cancel_targets <= normal_keys, "존재하지 않는 원거래를 가리키는 취소가 있다"

    # 정상 거래에는 ORG* 가 비어 있다
    assert normals[["ORGSALEDATE", "ORGPOSNO", "ORGDEALNO"]].isna().all().all()


def test_cancel_amounts_are_negative(merged: synth.DayData) -> None:
    """명세 6.5: 취소 거래는 전 금액이 음수다."""
    cancel_keys = merged.receipts.loc[
        merged.receipts["CANCELTYPE"] == "1",
        ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"],
    ]
    key = ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"]

    assert (
        merged.receipts[merged.receipts["CANCELTYPE"] == "1"]["DEALAMOUNT"] < 0
    ).all()

    cancel_items = merged.items.merge(cancel_keys, on=key)
    cancel_payments = merged.payments.merge(cancel_keys, on=key)

    assert not cancel_items.empty
    assert (cancel_items["SALEAMOUNT"] < 0).all()
    assert (cancel_items["QTY"] < 0).all(), "수량도 음수라야 상품 마트가 상계된다"
    assert (cancel_payments["TENDERAMOUNT"] < 0).all()


def test_cancel_time_is_after_original(merged: synth.DayData) -> None:
    """명세 6.5: 취소는 원거래 SALETIME 뒤에 일어난다 (같은 날 안)."""
    receipts = merged.receipts
    cancels = receipts[receipts["CANCELTYPE"] == "1"]

    originals = receipts[receipts["CANCELTYPE"].isna()][
        ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SALETIME"]
    ].rename(
        columns={
            "SALEDATE": "ORGSALEDATE",
            "POSNO": "ORGPOSNO",
            "DEALNO": "ORGDEALNO",
            "SALETIME": "ORG_SALETIME",
        }
    )

    joined = cancels.merge(
        originals, on=["DEPT_CD", "ORGSALEDATE", "ORGPOSNO", "ORGDEALNO"], how="left"
    )

    assert joined["ORG_SALETIME"].notna().all()
    assert (joined["SALETIME"] > joined["ORG_SALETIME"]).all()


def test_cancel_rate_is_near_spec(merged: synth.DayData) -> None:
    """명세 6.5: 취소 거래 비율이 1.5% 근처다 (표본 변동 허용)."""
    receipts = merged.receipts
    rate = (receipts["CANCELTYPE"] == "1").mean()

    assert 0.008 <= rate <= 0.025, f"취소 비율 {rate:.4f} — 명세 1.5%에서 너무 벗어났다"


# --- 명세 6.2 / 6.5 구조 ---------------------------------------------------


def test_saletime_within_business_hours(merged: synth.DayData) -> None:
    """명세 6.2: 영업시간 05~23시 안에서만 거래가 발생한다."""
    hours = merged.receipts["SALETIME"].str[:2].astype(int)
    assert hours.between(5, 23).all()


def test_dealno_is_sequential_per_pos(sample_days: list[synth.DayData]) -> None:
    """명세 6.5: DEALNO는 점포·일자·POS 안에서 0001부터 1씩 증가한다."""
    for day in sample_days:
        for _, group in day.receipts.groupby(["DEPT_CD", "SALEDATE", "POSNO"]):
            numbers = sorted(group["DEALNO"].astype(int).tolist())
            assert numbers == list(range(1, len(numbers) + 1))
            assert group["DEALNO"].str.fullmatch(r"\d{4}").all(), "DEALNO는 4자리 문자열"


def test_dealno_order_follows_time(sample_days: list[synth.DayData]) -> None:
    """DEALNO 순서가 거래 시각 순서와 일치한다 (취소 거래 포함)."""
    for day in sample_days:
        for _, group in day.receipts.groupby(["DEPT_CD", "SALEDATE", "POSNO"]):
            ordered = group.sort_values("DEALNO")
            assert ordered["SALETIME"].is_monotonic_increasing


def test_avg_lines_per_receipt_matches_spec(merged: synth.DayData) -> None:
    """명세 6.5: 영수증당 상품 행수 평균이 약 1.48이다."""
    key = ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"]
    avg = merged.items.groupby(key).size().mean()

    assert avg == pytest.approx(1.48, abs=0.06), f"평균 상품 행수 {avg:.3f}"


def test_products_come_from_frozen_catalog(merged: synth.DayData) -> None:
    """상품은 동결된 씨앗 사전에서만 나온다 (ADR-0002)."""
    catalog_plu = {product["plu_cd"] for product in synth.load_catalog()["products"]}

    assert set(merged.items["PLU_CD"]) <= catalog_plu
    assert merged.items["PLU_CD"].str.fullmatch(r"88\d{11}").all(), "명세 6.4 PLU 형식"


def test_tender_sections_are_spec_codes(merged: synth.DayData) -> None:
    """명세 4장·6.5: 결제수단 코드는 01/02/03 뿐이다."""
    assert set(merged.payments["TENDERSECTION"]) <= {"01", "02", "03"}


def test_dealtype_is_normal_sale_only(merged: synth.DayData) -> None:
    """명세 4장: 데모는 DEALTYPE='0'(정상판매)만 만든다."""
    assert (merged.receipts["DEALTYPE"] == "0").all()


def test_frames_have_exact_ddl_columns(merged: synth.DayData) -> None:
    """생성 결과의 컬럼이 명세 4장 DDL과 이름·순서까지 같다 (불변식 5)."""
    from src.load import schema

    assert list(merged.receipts.columns) == [c.name for c in schema.FACT_RECEIPT.columns]
    assert list(merged.items.columns) == [c.name for c in schema.FACT_RECEIPT_ITEM.columns]
    assert list(merged.payments.columns) == [c.name for c in schema.FACT_PAYMENT.columns]


# --- 불변식 4: 파생 시드 결정성 --------------------------------------------


def test_generate_day_is_deterministic() -> None:
    """같은 (점포, 날짜)를 두 번 생성하면 완전히 같은 데이터가 나온다."""
    store = synth.STORES[0]
    first = synth.generate_day(store, "20260722")
    second = synth.generate_day(store, "20260722")

    pd.testing.assert_frame_equal(first.receipts, second.receipts)
    pd.testing.assert_frame_equal(first.items, second.items)
    pd.testing.assert_frame_equal(first.payments, second.payments)


def test_generate_day_is_independent_of_order() -> None:
    """앞선 날짜를 몇 개나 생성했든 특정 날짜의 결과는 같다 (불변식 4).

    전역 순차 난수를 쓰면 이 테스트가 깨진다 — 부분 재생성 결정성의 핵심이다.
    """
    store = synth.STORES[1]
    target = "20260710"

    alone = synth.generate_day(store, target)

    for warmup_date in ("20260701", "20260702", "20260703"):
        synth.generate_day(store, warmup_date)
    after_warmup = synth.generate_day(store, target)

    pd.testing.assert_frame_equal(alone.receipts, after_warmup.receipts)
    pd.testing.assert_frame_equal(alone.items, after_warmup.items)


def test_stores_differ_on_same_date() -> None:
    """같은 날짜라도 점포가 다르면 데이터가 다르다 (시드에 점포가 들어간다)."""
    date = "20260722"
    left = synth.generate_day(synth.STORES[0], date)
    right = synth.generate_day(synth.STORES[1], date)

    assert len(left.receipts) != len(right.receipts)


# --- 명세 6.2 달력 효과 ----------------------------------------------------


def test_weekend_volume_is_lower_than_weekday() -> None:
    """명세 6.2 요일 계수: 일요일(0.75)이 금요일(1.10)보다 거래가 적다."""
    store = synth.STORES[0]

    fridays = ["20260703", "20260710", "20260717", "20260724"]
    sundays = ["20260705", "20260712", "20260719", "20260726"]

    friday_avg = sum(len(synth.generate_day(store, d).receipts) for d in fridays) / len(fridays)
    sunday_avg = sum(len(synth.generate_day(store, d).receipts) for d in sundays) / len(sundays)

    assert sunday_avg < friday_avg


def test_generate_period_covers_every_day() -> None:
    """generate_period가 기간의 모든 날짜 × 모든 점포를 빠짐없이 만든다."""
    days = list(synth.generate_period("20260701", "20260707"))

    assert len(days) == 7 * len(synth.STORES)

    produced = {
        (day.receipts["DEPT_CD"].iloc[0], day.receipts["SALEDATE"].iloc[0]) for day in days
    }
    expected = {
        (store.dept_cd, f"202607{day:02d}")
        for store in synth.STORES
        for day in range(1, 8)
    }
    assert produced == expected


def test_generate_period_filters_stores() -> None:
    """generate_period가 점포 부분집합만 생성할 수 있다 (CLI --stores 대비)."""
    days = list(synth.generate_period("20260701", "20260702", dept_cds=["901003"]))

    assert len(days) == 2
    assert {day.receipts["DEPT_CD"].iloc[0] for day in days} == {"901003"}


def test_store_dim_frame_matches_ddl() -> None:
    """DIM_STORE 적재용 프레임이 DDL 컬럼과 일치한다."""
    from src.load import schema

    frame = synth.store_dim_frame()

    assert list(frame.columns) == [c.name for c in schema.DIM_STORE.columns]
    assert len(frame) == len(synth.STORES)
