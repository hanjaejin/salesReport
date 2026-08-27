"""결품 예상(G3) 검증 — 부록 A.

명세 본문의 규율을 그대로 적용한다: 임계값 경계를 고정하고, 문장에 발주 수량이
새어 나가지 않는지(본 명세 7.4 금지) 정적으로 막는다.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, select

from src.common.config import get_engine
from src.extract.sample import SampleExtractor
from src.generate import synth
from src.load import pipeline, schema
from src.mart import briefing

SALEDATE = "20260703"


def _stock_frame(rows: list[dict]) -> pd.DataFrame:
    """테스트용 재고 스냅샷 프레임을 만든다.

    Args:
        rows: 부분 지정 딕셔너리 목록. 빠진 값은 안전한 기본값으로 채운다.

    Returns:
        ``FACT_STOCK_SNAPSHOT`` 컬럼을 가진 프레임.
    """
    defaults = {
        "SALEDATE": SALEDATE,
        "DEPT_CD": "901001",
        "PLU_CD": "8800000000001",
        "GOODS_NM": "테스트상품",
        "ITEM_HEAD_NM": "음료",
        "RUNNING_STOCK_QTY": 100,
        "IPGO_QTY": 0,
        "SALE_AVERAGE_QTY": 10.0,
        "PROPER_STOCK_QTY": 20,
        "ADVICE_ORDER_QTY": 0,
        "LEAD_TM": 1,
    }
    return pd.DataFrame([{**defaults, **row} for row in rows])


# --- 부록 A.3 생성 스펙 ------------------------------------------------------


@pytest.fixture(scope="module")
def snapshots() -> list[pd.DataFrame]:
    """표본 날짜 × 점포 3곳의 재고 스냅샷."""
    dates = ("20250701", "20251225", "20260305", "20260703", "20260731")
    return [
        synth.generate_stock_snapshot(store, date)
        for store in synth.STORES
        for date in dates
    ]


def test_stock_snapshot_never_negative(snapshots: list[pd.DataFrame]) -> None:
    """부록 A.3: 재고·입고예정·적정재고·권고발주가 음수가 아니다.

    씨앗 실데이터에는 음수 재고가 40건 있었다. 화면에 "-270개"가 뜨지 않도록
    생성 단계에서 막는다.
    """
    for frame in snapshots:
        for column in (
            "RUNNING_STOCK_QTY", "IPGO_QTY", "PROPER_STOCK_QTY",
            "ADVICE_ORDER_QTY", "SALE_AVERAGE_QTY",
        ):
            assert (frame[column] >= 0).all(), f"{column} 에 음수가 있다"


def test_stock_snapshot_covers_whole_catalog(snapshots: list[pd.DataFrame]) -> None:
    """상품 사전 전 품목에 매일 1행씩 만든다 (부록 A.3)."""
    catalog_size = len(synth.load_catalog()["products"])

    for frame in snapshots:
        assert len(frame) == catalog_size
        assert frame["PLU_CD"].is_unique


def test_stock_columns_match_ddl() -> None:
    """생성 결과의 컬럼이 부록 A.2 DDL과 이름·순서까지 같다 (불변식 5)."""
    frame = synth.generate_stock_snapshot(synth.STORES[0], SALEDATE)

    assert list(frame.columns) == [
        column.name for column in schema.FACT_STOCK_SNAPSHOT.columns
    ]


def test_proper_stock_follows_holding_days(snapshots: list[pd.DataFrame]) -> None:
    """부록 A.3: 적정재고 = ceil(매출평균수량 × 재고보유일수), 최소 1."""
    for frame in snapshots:
        expected = np.maximum(
            np.ceil(frame["SALE_AVERAGE_QTY"] * synth.STOCK_HOLDING_DAYS), 1
        )
        assert (frame["PROPER_STOCK_QTY"] == expected).all()


def test_lead_time_is_one_or_two(snapshots: list[pd.DataFrame]) -> None:
    """부록 A.3: 리드타임은 1일 또는 2일이다 (실측 평균 1.012)."""
    for frame in snapshots:
        assert set(frame["LEAD_TM"].unique()) <= {1, 2}


def test_stock_average_matches_sales() -> None:
    """부록 A.8: 매출평균수량 합이 실제 판매수량과 통계적으로 정합하다.

    재고가 판매와 따로 놀면 "잘 팔리는데 재고가 넘친다" 같은 모순이 화면에 뜬다.
    """
    store = synth.STORES[0]
    dates = [f"202607{day:02d}" for day in range(1, 15)]

    predicted = sum(
        float(synth.generate_stock_snapshot(store, date)["SALE_AVERAGE_QTY"].sum())
        for date in dates
    )
    actual = sum(
        int(synth.generate_day(store, date).items["QTY"].sum()) for date in dates
    )

    ratio = predicted / actual
    assert 0.75 <= ratio <= 1.25, f"예측/실제 = {ratio:.3f} (예측 {predicted:.0f}, 실제 {actual})"


def test_stock_snapshot_deterministic() -> None:
    """불변식 4: 같은 (점포, 날짜)면 같은 재고가 나온다."""
    first = synth.generate_stock_snapshot(synth.STORES[1], SALEDATE)
    second = synth.generate_stock_snapshot(synth.STORES[1], SALEDATE)

    pd.testing.assert_frame_equal(first, second)


def test_stock_generation_does_not_disturb_sales() -> None:
    """부록 A.3: 재고 생성이 판매 데이터를 바꾸지 않는다 (용도별 독립 시드)."""
    before = synth.generate_day(synth.STORES[0], SALEDATE)

    for date in ("20260701", "20260702", SALEDATE):
        synth.generate_stock_snapshot(synth.STORES[0], date)

    after = synth.generate_day(synth.STORES[0], SALEDATE)

    pd.testing.assert_frame_equal(before.receipts, after.receipts)
    pd.testing.assert_frame_equal(before.items, after.items)


def test_stock_seed_is_independent_of_sales_seed() -> None:
    """재고 시드가 판매 시드와 다르다 — 규칙을 고쳐도 판매가 흔들리지 않는다."""
    from src.common.config import derive_seed

    sales = derive_seed("901001", SALEDATE)
    stock = derive_seed("901001", SALEDATE, synth.STOCK_PURPOSE)

    assert sales != stock


def test_calm_days_have_no_shortage() -> None:
    """부록 A.3 재고 압박 모델: 부족 품목이 하나도 없는 날이 존재한다.

    이것이 없으면 G3가 매일 발동해 G2(시간대) 문장이 영영 나오지 않는다.
    """
    from src.common.dateutil import date_range

    store = synth.STORES[0]
    calm_days = 0
    for date in date_range("20260601", "20260731"):
        risk = briefing.find_stock_risk(synth.generate_stock_snapshot(store, date))
        if risk["risk_count"] == 0:
            calm_days += 1

    assert calm_days > 0, "부족 품목이 없는 날이 하루도 없다 — G3가 100% 발동한다"


def test_g3_and_g2_share_line_two() -> None:
    """G3와 G2가 2줄을 나눠 갖는다 — 어느 한쪽이 독점하지 않는다 (부록 A.3)."""
    from src.common.dateutil import date_range

    store = synth.STORES[0]
    fired = sum(
        briefing.g3_fires(
            briefing.find_stock_risk(synth.generate_stock_snapshot(store, date))["risk_count"]
        )
        for date in date_range("20260401", "20260731")
    )
    total = len(date_range("20260401", "20260731"))
    rate = fired / total

    assert 0.15 <= rate <= 0.85, f"G3 발동률 {rate:.1%} — 한쪽이 2줄을 독점한다"


# --- 부록 A.4 판정 규칙 ------------------------------------------------------


@pytest.mark.parametrize(
    ("stock_qty", "lead_tm", "expected"),
    [
        (11, 1, False),  # 소진 1.1일 > 리드타임 1 → 안전
        (10, 1, True),   # 소진 1.0일 = 리드타임 1 → 경계 발동
        (9, 1, True),
        (21, 2, False),  # 소진 2.1일 > 리드타임 2
        (20, 2, True),   # 소진 2.0일 = 리드타임 2 → 경계 발동
    ],
)
def test_g3_threshold(stock_qty: int, lead_tm: int, expected: bool) -> None:
    """부록 A.4: 소진일수 <= 리드타임 경계에서 정확히 발동한다."""
    risk = briefing.find_stock_risk(
        _stock_frame([{"RUNNING_STOCK_QTY": stock_qty, "LEAD_TM": lead_tm}])
    )

    assert briefing.g3_fires(risk["risk_count"]) is expected


def test_g3_counts_incoming_stock() -> None:
    """입고 예정 수량이 가용 재고에 더해진다 (부록 A.4)."""
    without = briefing.find_stock_risk(_stock_frame([{"RUNNING_STOCK_QTY": 5}]))
    with_incoming = briefing.find_stock_risk(
        _stock_frame([{"RUNNING_STOCK_QTY": 5, "IPGO_QTY": 20}])
    )

    assert without["risk_count"] == 1
    assert with_incoming["risk_count"] == 0


def test_g3_excludes_slow_movers() -> None:
    """부록 A.4: 하루 1개도 안 나가는 상품은 위험 목록에서 제외한다."""
    risk = briefing.find_stock_risk(
        _stock_frame([{"RUNNING_STOCK_QTY": 0, "SALE_AVERAGE_QTY": 0.4}])
    )

    assert risk["risk_count"] == 0


def test_g3_zero_sales_is_never_risky() -> None:
    """판매가 0인 상품은 0으로 나누지 않고 위험도 아니다."""
    risk = briefing.find_stock_risk(
        _stock_frame([{"RUNNING_STOCK_QTY": 0, "SALE_AVERAGE_QTY": 0.0}])
    )

    assert risk["risk_count"] == 0


def test_g3_top_is_most_urgent() -> None:
    """가장 급한 품목(소진일수 최소)이 문장의 주어가 된다 (부록 A.5)."""
    risk = briefing.find_stock_risk(
        _stock_frame(
            [
                {"PLU_CD": "8800000000001", "GOODS_NM": "여유", "RUNNING_STOCK_QTY": 10},
                {"PLU_CD": "8800000000002", "GOODS_NM": "급함", "RUNNING_STOCK_QTY": 2},
                {"PLU_CD": "8800000000003", "GOODS_NM": "보통", "RUNNING_STOCK_QTY": 6},
            ]
        )
    )

    assert risk["risk_count"] == 3
    assert risk["other_count"] == 2
    assert risk["top"]["goods_nm"] == "급함"
    assert [item["goods_nm"] for item in risk["items"]] == ["급함", "보통", "여유"]


def test_g3_days_left_rounded_to_one_decimal() -> None:
    """부록 A.6: 소진일수는 저장 시점에 소수 1자리로 반올림한다."""
    risk = briefing.find_stock_risk(
        _stock_frame([{"RUNNING_STOCK_QTY": 1, "SALE_AVERAGE_QTY": 3.0}])
    )

    days = risk["top"]["days_left"]
    assert days == round(days, 1)


def test_g3_item_list_is_capped() -> None:
    """자세히 목록은 최대 5개다 (부록 A.6). 총 개수는 그대로 센다."""
    risk = briefing.find_stock_risk(
        _stock_frame(
            [
                {
                    "PLU_CD": f"88000000000{index:02d}",
                    "GOODS_NM": f"상품{index}",
                    "RUNNING_STOCK_QTY": index,
                }
                for index in range(9)
            ]
        )
    )

    assert risk["risk_count"] == 9
    assert len(risk["items"]) == briefing.STOCK_RISK_LIST_SIZE


def test_g3_outranks_g2() -> None:
    """부록 A.4: G3가 발동하면 G2는 카드에 넣지 않는다."""
    stock_risk = {
        "risk_count": 2, "other_count": 1, "top": {"goods_nm": "삼각김밥"}, "items": []
    }

    cards = briefing.build_cards(
        prev_diff_pct=1.0, peak_share_pct=31.0, signal=None, block=None, stock_risk=stock_risk
    )

    assert [card["card_id"] for card in cards] == ["G3"]


def test_g2_returns_when_g3_silent() -> None:
    """위험 품목이 없으면 2줄은 기존 G2로 돌아간다."""
    stock_risk = {"risk_count": 0, "other_count": 0, "top": None, "items": []}

    cards = briefing.build_cards(
        prev_diff_pct=1.0, peak_share_pct=31.0, signal=None, block=None, stock_risk=stock_risk
    )

    assert [card["card_id"] for card in cards] == ["G2"]


def test_cards_stay_at_most_two_with_g3() -> None:
    """부록 A.4: G3를 넣어도 카드는 최대 2개다 (명세 7.3 규칙 유지)."""
    stock_risk = {"risk_count": 1, "other_count": 0, "top": {"goods_nm": "우유"}, "items": []}

    cards = briefing.build_cards(
        prev_diff_pct=9.0, peak_share_pct=31.0, signal=None, block=None, stock_risk=stock_risk
    )

    assert [card["card_id"] for card in cards] == ["G4", "G3"]


# --- 부록 A.5 문장 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        (0, "농심)백산수500ml 재고가 얼마 남지 않았어요 — 오늘 채워 두는 게 좋아요"),
        (1, "재고가 얼마 남지 않은 상품이 있어요 — 농심)백산수500ml부터 확인해 보세요"),
    ],
)
def test_line2_g3_single(variant: int, expected: str) -> None:
    """부록 A.5: 위험 품목이 1개일 때의 2줄."""
    card = {
        "card_id": "G3",
        "lines": {"goods_nm": "농심)백산수500ml", "risk_count": 1, "other_count": 0},
    }

    assert briefing.render_line2(card, variant) == expected


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        (0, "농심)백산수500ml 외 3개 상품의 재고가 얼마 남지 않았어요 — 오늘 채워 두는 게 좋아요"),
        (1, "재고가 얼마 남지 않은 상품이 4개 있어요 — 농심)백산수500ml부터 확인해 보세요"),
    ],
)
def test_line2_g3_multi(variant: int, expected: str) -> None:
    """부록 A.5: 위험 품목이 여러 개일 때의 2줄."""
    card = {
        "card_id": "G3",
        "lines": {"goods_nm": "농심)백산수500ml", "risk_count": 4, "other_count": 3},
    }

    assert briefing.render_line2(card, variant) == expected


def test_g3_templates_never_show_order_quantity() -> None:
    """명세 7.4·부록 A.5: 발주 수량을 제시하지 않고 전문용어도 쓰지 않는다."""
    templates = " ".join(briefing.LINE2_STOCK_TEMPLATES.values())

    for forbidden in ("발주", "권고", "적정재고", "리드타임", "결품", "소진", "재고회전"):
        assert forbidden not in templates, f"금지 표현 '{forbidden}' 이 G3 템플릿에 있다"


def test_g3_templates_avoid_cause_assertion() -> None:
    """명세 2장 원칙 5: 원인을 단정하지 않는다."""
    templates = " ".join(briefing.LINE2_STOCK_TEMPLATES.values())

    for word in ("때문", "탓", "원인"):
        assert word not in templates


# --- 적재·저장 통합 ----------------------------------------------------------


@pytest.fixture(scope="module")
def built_engine(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    """재고까지 적재·브리핑을 마친 엔진 (모듈 1회)."""
    engine = get_engine(tmp_path_factory.mktemp("stock") / "stock.db")
    pipeline.load_period(SampleExtractor(), "20260628", "20260705", engine=engine)
    return engine


def test_stock_is_loaded_by_pipeline(built_engine: Engine) -> None:
    """파이프라인이 재고 스냅샷을 적재한다."""
    with built_engine.connect() as connection:
        frame = pd.read_sql(
            select(schema.FACT_STOCK_SNAPSHOT).where(
                schema.FACT_STOCK_SNAPSHOT.c.SALEDATE == SALEDATE
            ),
            connection,
        )

    catalog_size = len(synth.load_catalog()["products"])
    assert len(frame) == catalog_size * len(synth.STORES)


def test_stock_load_is_idempotent(tmp_path: Path) -> None:
    """재고도 날짜 단위 DELETE→INSERT 멱등이다 (불변식 3)."""
    engine = get_engine(tmp_path / "idem.db")
    extractor = SampleExtractor(dept_cds=["901003"])

    pipeline.load_period(extractor, SALEDATE, SALEDATE, engine=engine)
    with engine.connect() as connection:
        first = pd.read_sql(select(schema.FACT_STOCK_SNAPSHOT), connection)

    pipeline.load_period(extractor, SALEDATE, SALEDATE, engine=engine)
    with engine.connect() as connection:
        second = pd.read_sql(select(schema.FACT_STOCK_SNAPSHOT), connection)

    pd.testing.assert_frame_equal(first, second)


def test_briefing_line2_reflects_stock(built_engine: Engine) -> None:
    """G3가 발동한 점포의 2줄이 실제로 재고 문장이다."""
    import json

    with built_engine.connect() as connection:
        rows = connection.execute(
            select(
                schema.BRIEFING_DAILY.c.DEPT_CD, schema.BRIEFING_DAILY.c.PAYLOAD_JSON
            ).where(schema.BRIEFING_DAILY.c.SALEDATE == SALEDATE)
        ).all()

    checked = 0
    for _, raw in rows:
        payload = json.loads(raw)
        if "G3" not in {card["card_id"] for card in payload["cards"]}:
            continue
        checked += 1
        assert "재고" in payload["briefing_lines"][1]
        assert payload["stock_risk"]["risk_count"] >= 1
        assert payload["stock_risk"]["top"]["goods_nm"] in payload["briefing_lines"][1]

    assert checked > 0, "표본에 G3 발동 점포가 없어 검증하지 못했다"
