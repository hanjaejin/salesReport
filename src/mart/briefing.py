"""브리핑 생성 — 명세 7.2~7.4.

**계산 계층**(``build_payload``)이 숫자를 만들어 JSON에 담고,
**문장 계층**(``render_line1``~``render_line3``)은 그 값을 치환만 한다.
문장 계층에는 산술 연산이 하나도 없다 — 반올림·절댓값·비교조차 계산 계층에서 끝난다
(불변식 1 · ADR-0007). 이 규율은 정적 검사 테스트가 지킨다.

환각이 원천적으로 불가능한 이유가 여기 있다: 자유 생성이 없고 분기와 치환뿐이다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, delete, select

from src.common.config import (
    DOW_BASELINE_WEEKS,
    G2_THRESHOLD_PCT,
    G4_THRESHOLD_PCT,
    LINE1_THRESHOLD_PCT,
    derive_seed,
)
from src.common.dateutil import date_range, dow_name, previous_same_dow, shift_days
from src.common.logger import get_logger
from src.generate.synth import BLOCK_RANGES, HOURS, TIME_BLOCKS
from src.load import schema

logger = get_logger(__name__)

#: 계산 JSON의 판(版). 화면이 필드 유무를 판단할 수 있게 남긴다 (ADR-0007).
SCHEMA_VERSION = 1

# --- 명세 7.4 템플릿 (문자열 임의 수정 금지 — 명세 14장) ---------------------
# 변형 A = 접미 ``_0``, 변형 B = 접미 ``_1``.
# 1줄은 "어제 {sale_amt:,}원 — " 접두를 두 변형이 공유한다 (ADR-0007 결정 1).

LINE1_TEMPLATES: dict[str, str] = {
    "rise_0": "어제 {sale_amt:,}원 — 평소 {dow_name}보다 {dow_diff_pct}% 좋았어요 🔺",
    "rise_1": "어제 {sale_amt:,}원 — 평소 {dow_name}보다 {dow_diff_pct}% 잘 나온 하루였어요 🔺",
    "fall_0": "어제 {sale_amt:,}원 — 평소 {dow_name}보다 {dow_diff_pct_abs}% 아쉬웠어요 🔻",
    "fall_1": "어제 {sale_amt:,}원 — 평소 {dow_name}보다 {dow_diff_pct_abs}% 조용한 하루였어요 🔻",
    "flat_0": "어제 {sale_amt:,}원 — 평소 {dow_name} 수준이었어요",
    "flat_1": "어제 {sale_amt:,}원 — 평소 {dow_name}과 비슷했어요",
    # 기준선 폴백: 직전 4주 동일 요일 표본이 4개 미만일 때 (명세 7.4)
    "fallback": "어제 {sale_amt:,}원이었어요",
}

LINE2_TEMPLATES: dict[str, str] = {
    "g2_0": "{block_name}({block_range})에 하루 매출의 {share_pct}%가 나와요 — 그 전에 진열을 확인해 보세요",
    "g2_1": "{block_name} 손님이 몰리기 전({block_range})에 진열을 한 번 봐주세요 — 하루 매출의 {share_pct}%가 이때 나와요",
    "silent_0": "오늘은 평소 준비대로 하시면 충분해요",
    "silent_1": "특별한 준비 없이 평소처럼 하시면 돼요",
}

#: 2줄 G3(결품) 발동형 — 부록 A.5. 기존 2줄 문자열은 건드리지 않는다.
#: 조사 문제를 피하려고 "{goods_nm} 재고가" / "{goods_nm}부터" 형태를 쓴다.
LINE2_STOCK_TEMPLATES: dict[str, str] = {
    "single_0": "{goods_nm} 재고가 얼마 남지 않았어요 — 오늘 채워 두는 게 좋아요",
    "multi_0": "{goods_nm} 외 {other_count}개 상품의 재고가 얼마 남지 않았어요 — 오늘 채워 두는 게 좋아요",
    "single_1": "재고가 얼마 남지 않은 상품이 있어요 — {goods_nm}부터 확인해 보세요",
    "multi_1": "재고가 얼마 남지 않은 상품이 {risk_count}개 있어요 — {goods_nm}부터 확인해 보세요",
}

#: 3줄은 정확성 우선으로 단일 문형을 유지한다 (명세 7.4).
#: ``{support_particle}`` 은 명세의 고정 조사 "는"을 대신한다 — 보조어가
#: "1인당 구매액"일 때 "구매액는"이라는 비문이 화면에 나오기 때문이다 (ADR-0007 결정 3).
LINE3_G4_TEMPLATE = (
    "그저께와 비교하면, {subject_name}({subject_pct}%) 영향이 컸어요 — "
    "{support_name}{support_particle} {support_pct}%였어요"
)
LINE3_SILENT = "특별한 신호는 없어요"

#: 신호 카드의 주어 후보 (명세 7.4). 전문용어 대신 쉬운 말을 쓴다 (명세 14장).
SIGNAL_LABELS: dict[str, str] = {"cnt": "손님 수", "ticket": "1인당 구매액"}

#: 받침 유무에 따른 주격 보조사 (ADR-0007 결정 3).
SIGNAL_PARTICLES: dict[str, str] = {"cnt": "는", "ticket": "은"}

#: 위험 품목 하한 — 하루 1개도 안 나가는 꼬리 상품은 제외한다 (부록 A.4).
MIN_SALE_AVERAGE_FOR_RISK: float = 1.0

#: 자세히 화면에 보여 줄 위험 품목 수 (부록 A.6).
STOCK_RISK_LIST_SIZE: int = 5


# --- 명세 7.3 카드 판정 -----------------------------------------------------


def g4_fires(prev_diff_pct: float | None) -> bool:
    """G4(구조) 카드 발동 여부 (명세 7.3: ``abs(prev_diff_pct) >= 5.0``).

    Args:
        prev_diff_pct: 전일 대비 증감률. 전일 데이터가 없으면 None.

    Returns:
        발동하면 True. 전일 데이터가 없으면 항상 False (명세 7.4).
    """
    if prev_diff_pct is None:
        return False
    return abs(prev_diff_pct) >= G4_THRESHOLD_PCT


def g2_fires(peak_share_pct: float | None) -> bool:
    """G2(시간대) 카드 발동 여부 (명세 7.3: ``share_pct >= 25.0``).

    Args:
        peak_share_pct: 최대 시간 블록의 매출 비중.

    Returns:
        발동하면 True.
    """
    if peak_share_pct is None:
        return False
    return peak_share_pct >= G2_THRESHOLD_PCT


def g3_fires(risk_count: int) -> bool:
    """G3(결품) 카드 발동 여부 (부록 A.4: 위험 품목 >= 1개).

    Args:
        risk_count: 위험 품목 수.

    Returns:
        발동하면 True.
    """
    return risk_count >= 1


def find_stock_risk(stock: pd.DataFrame) -> dict[str, Any]:
    """재고 스냅샷에서 위험 품목을 골라 계산 JSON 조각을 만든다 (부록 A.4·A.6).

    위험 판정은 두 조건을 모두 만족할 때다::

        소진일수 = (운영재고 + 입고예정) / 매출평균수량  <= 리드타임
        매출평균수량 >= 1.0

    두 번째 조건이 없으면 하루 1개도 안 나가는 꼬리 상품이 매일 목록을 채워
    신호가 무의미해진다.

    반올림과 정렬을 **여기서** 끝낸다 — 문장 계층은 치환만 한다 (불변식 1).

    Args:
        stock: 그 날 그 점포의 ``FACT_STOCK_SNAPSHOT`` 프레임.

    Returns:
        ``risk_count``·``other_count``·``top``·``items`` 를 담은 딕셔너리.
    """
    empty: dict[str, Any] = {"risk_count": 0, "other_count": 0, "top": None, "items": []}
    if stock.empty:
        return empty

    average = stock["SALE_AVERAGE_QTY"].to_numpy(dtype=float)
    available = (stock["RUNNING_STOCK_QTY"] + stock["IPGO_QTY"]).to_numpy(dtype=float)

    # 하루 판매가 0이면 영원히 안 떨어지는 셈이라 위험이 아니다 (0 나눗셈 방어).
    days_left = np.divide(
        available, average, out=np.full(average.shape, np.inf), where=average > 0
    )
    is_risk = (days_left <= stock["LEAD_TM"].to_numpy(dtype=float)) & (
        average >= MIN_SALE_AVERAGE_FOR_RISK
    )
    if not is_risk.any():
        return empty

    risky = (
        stock.loc[is_risk]
        .assign(DAYS_LEFT=np.round(days_left[is_risk], 1))
        .sort_values(["DAYS_LEFT", "SALE_AVERAGE_QTY"], ascending=[True, False])
    )

    items = [
        {
            "goods_nm": row.GOODS_NM,
            "stock_qty": int(row.RUNNING_STOCK_QTY),
            "incoming_qty": int(row.IPGO_QTY),
            "sale_average_qty": float(row.SALE_AVERAGE_QTY),
            "days_left": float(row.DAYS_LEFT),
        }
        for row in risky.head(STOCK_RISK_LIST_SIZE).itertuples(index=False)
    ]
    risk_count = int(is_risk.sum())

    return {
        "risk_count": risk_count,
        "other_count": risk_count - 1,
        "top": items[0],
        "items": items,
    }


def pick_signal(cnt_diff_pct: float | None, ticket_diff_pct: float | None) -> dict[str, Any]:
    """G4 문장의 주어·보조어를 정한다 (명세 7.4: 절댓값 큰 쪽이 주어).

    비교와 절댓값은 **여기서** 끝난다. 문장 계층은 결과만 치환한다 (불변식 1).

    Args:
        cnt_diff_pct: 손님 수 증감률 (전일 대비).
        ticket_diff_pct: 1인당 구매액 증감률 (전일 대비).

    Returns:
        ``subject_name``·``subject_pct``·``support_name``·``support_particle``·
        ``support_pct`` 를 담은 치환용 딕셔너리.
    """
    counts = 0.0 if cnt_diff_pct is None else cnt_diff_pct
    tickets = 0.0 if ticket_diff_pct is None else ticket_diff_pct

    # 동률이면 건수를 주어로 삼는다 (명세 7.4가 "건수·객단가" 순으로 적었다).
    subject_key, support_key = ("cnt", "ticket") if abs(counts) >= abs(tickets) else ("ticket", "cnt")
    values = {"cnt": counts, "ticket": tickets}

    return {
        "subject_name": SIGNAL_LABELS[subject_key],
        "subject_pct": values[subject_key],
        "support_name": SIGNAL_LABELS[support_key],
        "support_particle": SIGNAL_PARTICLES[support_key],
        "support_pct": values[support_key],
    }


def build_cards(
    prev_diff_pct: float | None,
    peak_share_pct: float | None,
    signal: dict[str, Any] | None,
    block: dict[str, Any] | None,
    stock_risk: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """발동 카드 목록을 만든다 (명세 7.3 · 부록 A.4).

    카드는 최대 2개다 — 3줄을 차지하는 G4 하나와, 2줄을 차지하는 G3/G2 중 하나.
    **G3가 G2보다 우선한다**: 둘 다 "오늘 준비"를 말하는데 물건이 떨어지는 일이
    진열 시점보다 급하기 때문이다. 없으면 G6 1개.

    Args:
        prev_diff_pct: 전일 대비 증감률.
        peak_share_pct: 최대 시간 블록 비중.
        signal: G4 카드의 치환값 (``pick_signal`` 결과).
        block: G2 카드의 치환값 (``block_name``·``block_range``·``share_pct``).
        stock_risk: G3 카드의 치환값 (``find_stock_risk`` 결과). None이면 재고 미사용.

    Returns:
        우선순위 순(G4 → G3/G2) 카드 리스트.
    """
    cards: list[dict[str, Any]] = []

    if g4_fires(prev_diff_pct):
        cards.append({"card_id": "G4", "lines": dict(signal) if signal else {}})

    risk_count = int(stock_risk["risk_count"]) if stock_risk else 0
    if g3_fires(risk_count):
        top = stock_risk["top"] if stock_risk else {}
        cards.append(
            {
                "card_id": "G3",
                "lines": {
                    "goods_nm": top["goods_nm"] if top else "",
                    "risk_count": risk_count,
                    "other_count": int(stock_risk["other_count"]) if stock_risk else 0,
                },
            }
        )
    elif g2_fires(peak_share_pct):
        cards.append({"card_id": "G2", "lines": dict(block) if block else {}})

    if not cards:
        cards.append({"card_id": "G6", "lines": {}})

    return cards


def find_card(cards: Sequence[dict[str, Any]], card_id: str) -> dict[str, Any] | None:
    """카드 목록에서 해당 카드를 찾는다.

    Args:
        cards: 카드 리스트.
        card_id: 찾을 카드 ID.

    Returns:
        찾으면 카드, 없으면 None.
    """
    return next((card for card in cards if card["card_id"] == card_id), None)


def template_variant(dept_cd: str, saledate: str) -> int:
    """템플릿 변형을 고른다 (명세 7.4: (날짜, 점포) 파생 시드로 결정적 선택).

    재생성해도 같은 문장이 나온다 — 즉석 창작이 아니라 결정적 선택이다.

    Args:
        dept_cd: 점포코드.
        saledate: ``YYYYMMDD``.

    Returns:
        0(변형 A) 또는 1(변형 B).
    """
    return derive_seed(dept_cd, saledate) % 2


# --- 문장 계층: 치환만 한다 (불변식 1) ---------------------------------------


def render_line1(payload: dict[str, Any], variant: int) -> str:
    """1줄(결과)을 만든다 — 명세 7.4.

    Args:
        payload: 계산 JSON. 필요한 값이 모두 반올림까지 끝난 상태여야 한다.
        variant: 0(변형 A) 또는 1(변형 B).

    Returns:
        완성된 1줄.
    """
    if not payload["dow_baseline_available"]:
        return LINE1_TEMPLATES["fallback"].format(**payload)

    if payload["dow_diff_pct_abs"] < LINE1_THRESHOLD_PCT:
        shape = "flat"
    elif payload["dow_diff_pct"] > 0:
        shape = "rise"
    else:
        shape = "fall"

    return LINE1_TEMPLATES[f"{shape}_{variant}"].format(**payload)


def render_line2(card: dict[str, Any] | None, variant: int) -> str:
    """2줄(준비)을 만든다 — 명세 7.4 · 부록 A.5.

    Args:
        card: 2줄을 차지하는 카드 (``G3`` 또는 ``G2``). 미발동이면 None.
        variant: 0(변형 A) 또는 1(변형 B).

    Returns:
        완성된 2줄.
    """
    if card is None:
        return LINE2_TEMPLATES[f"silent_{variant}"]

    if card["card_id"] == "G3":
        shape = "single" if card["lines"]["risk_count"] == 1 else "multi"
        return LINE2_STOCK_TEMPLATES[f"{shape}_{variant}"].format(**card["lines"])

    return LINE2_TEMPLATES[f"g2_{variant}"].format(**card["lines"])


def render_line3(card: dict[str, Any] | None) -> str:
    """3줄(신호)을 만든다 — 명세 7.4 (단일 문형).

    Args:
        card: G4 카드. 미발동이면 None.

    Returns:
        완성된 3줄.
    """
    if card is None:
        return LINE3_SILENT
    return LINE3_G4_TEMPLATE.format(**card["lines"])


# --- 계산 계층 -------------------------------------------------------------


def _pct_change(current: float, base: float) -> float | None:
    """증감률을 소수 1자리로 구한다 (명세 7.4 표기 규칙).

    반올림을 **여기서** 끝낸다. 문장 계층은 반올림하지 않는다 (불변식 1).

    Args:
        current: 현재 값.
        base: 비교 기준값.

    Returns:
        증감률(%). 기준값이 0이면 None (0나눗셈 방어).
    """
    if not base:
        return None
    return round((current / base - 1) * 100, 1)


def _peak_block(hourly_amounts: dict[str, int], sale_amt: int) -> dict[str, Any]:
    """최대 시간 블록과 그 매출 비중을 구한다 (명세 7.3 블록 정의).

    Args:
        hourly_amounts: 시각(``HH``) → 매출액.
        sale_amt: 그 날의 총 매출액.

    Returns:
        ``name``·``range``·``share_pct`` 를 담은 딕셔너리.
    """
    shares = {
        block: sum(hourly_amounts.get(hour, 0) for hour in hours)
        for block, hours in TIME_BLOCKS.items()
    }
    best = max(shares, key=lambda block: shares[block])

    # 매출이 0 이하(전액 취소 등)면 비중을 따질 수 없다 — 0으로 두어 G2를 재운다.
    share_pct = round(shares[best] / sale_amt * 100, 1) if sale_amt > 0 else 0.0
    return {"name": best, "range": BLOCK_RANGES[best], "share_pct": share_pct}


def _load_marts(
    engine: Engine, from_date: str, to_date: str, dept_cds: Sequence[str] | None
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, str]]:
    """브리핑에 필요한 마트를 한 번에 읽는다.

    요일 기준선(직전 4주)과 전일 비교 때문에 시작일보다 앞선 구간까지 읽는다.

    Args:
        engine: 대상 엔진.
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.
        dept_cds: 대상 점포코드. None이면 전 점포.

    Returns:
        ``(일 마트, 시간대 마트, 상품 마트, 재고 스냅샷, 점포명 매핑)``.
    """
    lookback_from = shift_days(from_date, 0 - (7 * DOW_BASELINE_WEEKS))

    def scoped(statement, table, start: str):  # type: ignore[no-untyped-def]
        """기간·점포 조건을 붙인다."""
        statement = statement.where(table.c.SALEDATE.between(start, to_date))
        if dept_cds is not None:
            statement = statement.where(table.c.DEPT_CD.in_(dept_cds))
        return statement

    with engine.connect() as connection:
        days = pd.read_sql(
            scoped(select(schema.MART_DAY_STORE), schema.MART_DAY_STORE, lookback_from),
            connection,
        )
        hours = pd.read_sql(
            scoped(select(schema.MART_HOUR_STORE), schema.MART_HOUR_STORE, from_date),
            connection,
        )
        items = pd.read_sql(
            scoped(
                select(schema.MART_DAY_STORE_ITEM), schema.MART_DAY_STORE_ITEM, from_date
            ),
            connection,
        )
        stock = pd.read_sql(
            scoped(
                select(schema.FACT_STOCK_SNAPSHOT), schema.FACT_STOCK_SNAPSHOT, from_date
            ),
            connection,
        )
        stores = pd.read_sql(select(schema.DIM_STORE), connection)

    names = dict(zip(stores["DEPT_CD"], stores["DEPT_NM"], strict=True))
    return days, hours, items, stock, names


def build_payload(
    saledate: str,
    dept_cd: str,
    dept_nm: str,
    day_row: pd.Series,
    prev_row: pd.Series | None,
    dow_rows: pd.DataFrame,
    hourly_amounts: dict[str, int],
    top_items: pd.DataFrame,
    stock: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """하루치 계산 JSON을 만든다 (명세 7.2 스키마 · ADR-0007 보강).

    모든 반올림이 여기서 끝난다 — 금액은 정수 원, 1인당 구매액은 정수 원,
    모든 %는 소수 1자리 (명세 7.4 표기 규칙).

    Args:
        saledate: ``YYYYMMDD``.
        dept_cd: 점포코드.
        dept_nm: 점포명.
        day_row: 그 날의 ``MART_DAY_STORE`` 행.
        prev_row: 전일의 ``MART_DAY_STORE`` 행. 없으면 None.
        dow_rows: 직전 4주 동일 요일의 ``MART_DAY_STORE`` 행들.
        hourly_amounts: 시각(``HH``) → 매출액.
        top_items: 매출 상위 상품 (최대 5행).
        stock: 그 날의 재고 스냅샷 (부록 A). None이면 결품 판정을 건너뛴다.

    Returns:
        ``BRIEFING_DAILY.PAYLOAD_JSON`` 에 저장할 딕셔너리.
    """
    sale_amt = int(day_row["SALE_AMT"])
    deal_cnt = int(day_row["DEAL_CNT"])
    avg_ticket = round(float(day_row["AVG_TICKET"]))

    baseline_available = len(dow_rows) >= DOW_BASELINE_WEEKS
    baseline_amt = round(float(dow_rows["SALE_AMT"].mean())) if not dow_rows.empty else 0
    dow_diff_pct = _pct_change(sale_amt, baseline_amt) if baseline_available else None

    prev_amt = int(prev_row["SALE_AMT"]) if prev_row is not None else 0
    prev_cnt = int(prev_row["DEAL_CNT"]) if prev_row is not None else 0
    prev_ticket = round(float(prev_row["AVG_TICKET"])) if prev_row is not None else 0

    prev_diff_pct = _pct_change(sale_amt, prev_amt) if prev_row is not None else None
    cnt_diff_pct = _pct_change(deal_cnt, prev_cnt) if prev_row is not None else None
    ticket_diff_pct = _pct_change(avg_ticket, prev_ticket) if prev_row is not None else None

    peak_block = _peak_block(hourly_amounts, sale_amt)
    signal = pick_signal(cnt_diff_pct, ticket_diff_pct)
    block_lines = {
        "block_name": peak_block["name"],
        "block_range": peak_block["range"],
        "share_pct": peak_block["share_pct"],
    }
    stock_risk = find_stock_risk(stock if stock is not None else pd.DataFrame())
    cards = build_cards(
        prev_diff_pct, peak_block["share_pct"], signal, block_lines, stock_risk
    )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "saledate": saledate,
        "dept_cd": dept_cd,
        "dept_nm": dept_nm,
        "sale_amt": sale_amt,
        "deal_cnt": deal_cnt,
        "avg_ticket": avg_ticket,
        "dow_name": dow_name(saledate),
        "dow_baseline_amt": baseline_amt,
        "dow_baseline_available": baseline_available,
        "dow_diff_pct": dow_diff_pct,
        # 문장 계층이 abs()를 부르지 않도록 절댓값을 미리 담는다 (ADR-0007 결정 2)
        "dow_diff_pct_abs": abs(dow_diff_pct) if dow_diff_pct is not None else None,
        "prev_amt": prev_amt,
        "prev_diff_pct": prev_diff_pct,
        "cnt_diff_pct": cnt_diff_pct,
        "ticket_diff_pct": ticket_diff_pct,
        "peak_block": peak_block,
        "top5": [
            {
                "goods_nm": row.GOODS_NM,
                "sale_amt": int(row.SALE_AMT),
                "qty": int(row.QTY),
            }
            for row in top_items.itertuples(index=False)
        ],
        "hourly": [
            {"hour": hour, "sale_amt": int(hourly_amounts.get(hour, 0))} for hour in HOURS
        ],
        "stock_risk": stock_risk,
        "cards": cards,
    }

    variant = template_variant(dept_cd, saledate)
    payload["template_variant"] = variant
    payload["briefing_lines"] = [
        render_line1(payload, variant),
        render_line2(find_card(cards, "G3") or find_card(cards, "G2"), variant),
        render_line3(find_card(cards, "G4")),
    ]
    return payload


def build_briefings(
    engine: Engine, from_date: str, to_date: str, dept_cds: Sequence[str] | None = None
) -> int:
    """기간의 브리핑을 재생성해 ``BRIEFING_DAILY`` 에 저장한다 (명세 8장 4단계).

    문장까지 여기서 완성된다. 화면은 저장된 글자를 표시만 한다 (불변식 7).

    Args:
        engine: 대상 엔진.
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.
        dept_cds: 대상 점포코드. None이면 전 점포.

    Returns:
        생성한 브리핑 수.
    """
    days, hours, items, stock, names = _load_marts(engine, from_date, to_date, dept_cds)
    if days.empty:
        logger.warning("브리핑 생성 대상이 없습니다: %s ~ %s", from_date, to_date)
        return 0

    day_index = days.set_index(["DEPT_CD", "SALEDATE"]).sort_index()
    hour_groups = {key: group for key, group in hours.groupby(["DEPT_CD", "SALEDATE"])}
    item_groups = {key: group for key, group in items.groupby(["DEPT_CD", "SALEDATE"])}
    stock_groups = {key: group for key, group in stock.groupby(["DEPT_CD", "SALEDATE"])}

    records: list[dict[str, object]] = []
    for saledate in date_range(from_date, to_date):
        for dept_cd in sorted({code for code, _ in day_index.index}):
            if (dept_cd, saledate) not in day_index.index:
                continue

            day_row = day_index.loc[(dept_cd, saledate)]
            prev_key = (dept_cd, shift_days(saledate, -1))
            prev_row = day_index.loc[prev_key] if prev_key in day_index.index else None

            dow_keys = [
                (dept_cd, date)
                for date in previous_same_dow(saledate, DOW_BASELINE_WEEKS)
                if (dept_cd, date) in day_index.index
            ]
            dow_rows = day_index.loc[dow_keys] if dow_keys else day_index.iloc[:0]

            hour_group = hour_groups.get((dept_cd, saledate))
            hourly_amounts = (
                dict(zip(hour_group["HOUR"], hour_group["SALE_AMT"], strict=True))
                if hour_group is not None
                else {}
            )

            item_group = item_groups.get((dept_cd, saledate))
            top_items = (
                item_group.nlargest(5, "SALE_AMT")
                if item_group is not None
                else items.iloc[:0]
            )

            payload = build_payload(
                saledate=saledate,
                dept_cd=dept_cd,
                dept_nm=names.get(dept_cd, dept_cd),
                day_row=day_row,
                prev_row=prev_row,
                dow_rows=dow_rows,
                hourly_amounts=hourly_amounts,
                top_items=top_items,
                stock=stock_groups.get((dept_cd, saledate)),
            )
            records.append(
                {
                    "SALEDATE": saledate,
                    "DEPT_CD": dept_cd,
                    "PAYLOAD_JSON": json.dumps(payload, ensure_ascii=False),
                }
            )

    statement = delete(schema.BRIEFING_DAILY).where(
        schema.BRIEFING_DAILY.c.SALEDATE.between(from_date, to_date)
    )
    if dept_cds is not None:
        statement = statement.where(schema.BRIEFING_DAILY.c.DEPT_CD.in_(dept_cds))

    with engine.begin() as connection:
        connection.execute(statement)
        if records:
            connection.execute(schema.BRIEFING_DAILY.insert(), records)

    logger.info("브리핑 생성 완료 %s ~ %s: %d건", from_date, to_date, len(records))
    return len(records)
