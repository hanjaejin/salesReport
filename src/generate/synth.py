"""합성 데이터 생성기 — 명세 6장 스펙 그대로.

씨앗(실샘플)에서 뽑은 상품 사전으로 점포 3곳 × 13개월의 영수증·상품·결제를 만든다.

**난수 규율(불변식 4)**: 모든 난수는 ``(점포, 날짜)`` 파생 시드의 독립 생성기에서
나온다. 전역 순차 난수를 쓰지 않으므로, 어떤 부분 구간만 다시 만들어도
전체를 만들었을 때와 같은 데이터가 나온다 — 멱등의 전제조건이다.

**벡터화**: 하루치를 numpy 배열로 한 번에 만든다. ``iterrows`` 를 쓰지 않는다.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

import numpy as np
import pandas as pd

from src.common.config import SEED_CATALOG_PATH, derive_rng
from src.common.dateutil import date_range, dow_index, parse_date
from src.generate.catalog_spec import BASE_GROUP_PROBS, HOUR_GROUP_ADJUST

# --- 명세 6.2 영업시간 -----------------------------------------------------

#: 영업시간 05~23시 (명세 6.2)
HOURS: Final[tuple[str, ...]] = tuple(f"{hour:02d}" for hour in range(5, 24))

# --- 명세 6.3 시간대 프로파일 (각 행 합 = 100) ------------------------------

#: 등급별 거래 발생 가중치. 세 점포의 브리핑이 서로 달라지게 만드는 장치다.
HOUR_PROFILES: Final[dict[str, tuple[float, ...]]] = {
    # 05   06   07    08    09   10   11   12    13    14   15   16   17   18   19   20   21   22    23
    "L": (2, 5, 12, 11, 7, 5, 6, 9, 6, 4, 4, 4, 5, 6, 5, 4, 3, 1.5, 0.5),
    "M": (1, 3, 8, 7, 5, 5, 8, 14, 12, 6, 5, 5, 6, 6, 4, 3, 1.5, 0.5, 0),
    "S": (2, 4, 8, 8, 6, 5, 6, 8, 7, 5, 5, 5, 7, 8, 7, 5, 3, 1, 0),
}

#: 명세 7.3의 시간 블록 정의 — G2 판정과 프로파일 검증이 같은 정의를 쓴다.
TIME_BLOCKS: Final[dict[str, tuple[str, ...]]] = {
    "아침": ("07", "08", "09"),
    "점심": ("12", "13"),
    "저녁": ("17", "18", "19"),
}

#: 블록 표시용 시간 범위 문자열 (명세 7.2 ``peak_block.range``)
BLOCK_RANGES: Final[dict[str, str]] = {
    "아침": "07~09시",
    "점심": "12~13시",
    "저녁": "17~19시",
}

# --- 명세 6.2 달력 효과 ----------------------------------------------------

#: 요일 계수 (월=0 … 일=6). 역사 출근 수요를 반영한다.
DOW_FACTORS: Final[tuple[float, ...]] = (1.05, 1.00, 1.00, 1.02, 1.10, 0.85, 0.75)

#: 월 계절 진폭 ±10% (명세 6.2)
SEASON_AMPLITUDE: Final[float] = 0.10

# --- 명세 6.5 영수증 구성 --------------------------------------------------

#: 영수증당 상품 행수 분포 — 평균 ≈ 1.48
LINE_COUNTS: Final[tuple[int, ...]] = (1, 2, 3, 4)
LINE_COUNT_PROBS: Final[tuple[float, ...]] = (0.65, 0.25, 0.07, 0.03)

#: 분할결제 비율 (단일결제 95% · 분할 5%)
SPLIT_PAYMENT_PROB: Final[float] = 0.05

#: 결제수단 — 현금 10% / 카드 70% / 간편결제 20%
TENDER_SECTIONS: Final[tuple[str, ...]] = ("01", "02", "03")
TENDER_PROBS: Final[tuple[float, ...]] = (0.10, 0.70, 0.20)

#: 동일자 취소 비율
CANCEL_RATE: Final[float] = 0.015

#: 취소 지연 시간 범위(시간) — 원거래 SALETIME + 1~4시간
CANCEL_DELAY_HOURS: Final[tuple[int, int]] = (1, 4)

#: 하루의 마지막 시각. 취소가 자정을 넘지 않도록 여기서 자른다.
LAST_SECOND_OF_DAY: Final[int] = 23 * 3600 + 59 * 60 + 59

#: 데모는 정상판매만 만든다 (명세 4장 DEALTYPE)
DEALTYPE_NORMAL: Final[str] = "0"

#: 취소 거래 표시 (명세 4장 CANCELTYPE)
CANCELTYPE_CANCELED: Final[str] = "1"


@dataclass(frozen=True)
class StoreProfile:
    """점포 1곳의 생성 파라미터 (명세 6.1 · ADR-0003).

    Attributes:
        dept_cd: 점포코드.
        dept_nm: 점포명.
        size_grade: 등급 ``L``/``M``/``S``. 시간대 프로파일 선택에 쓰인다.
        avg_deals: 일평균 거래 건수.
        variation: 일변동 폭 (0.15 이면 ±15%).
        pos_count: POS 대수.
    """

    dept_cd: str
    dept_nm: str
    size_grade: str
    avg_deals: int
    variation: float
    pos_count: int

    @property
    def hour_weights(self) -> tuple[float, ...]:
        """이 점포의 시간대 가중치 (명세 6.3)."""
        return HOUR_PROFILES[self.size_grade]


#: 명세 6.1의 점포 3곳
STORES: Final[tuple[StoreProfile, ...]] = (
    StoreProfile("901001", "중앙역 대형점", "L", 800, 0.15, 3),
    StoreProfile("901002", "동부역 중형점", "M", 300, 0.15, 2),
    StoreProfile("901003", "간이역 소형점", "S", 80, 0.20, 1),
)


@dataclass(frozen=True)
class DayData:
    """하루치 원장 3종 (명세 4장 DDL 컬럼 그대로).

    Attributes:
        receipts: ``FACT_RECEIPT`` 행.
        items: ``FACT_RECEIPT_ITEM`` 행.
        payments: ``FACT_PAYMENT`` 행.
    """

    receipts: pd.DataFrame
    items: pd.DataFrame
    payments: pd.DataFrame


# --- 상품 사전 -------------------------------------------------------------


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, object]:
    """동결된 씨앗 상품 사전을 읽는다 (ADR-0002).

    Returns:
        ``seed_catalog.json`` 의 내용.

    Raises:
        FileNotFoundError: 사전 JSON이 없을 때. ``build_catalog.py`` 로 만든다.
    """
    if not SEED_CATALOG_PATH.exists():
        raise FileNotFoundError(
            f"상품 사전이 없습니다: {SEED_CATALOG_PATH}\n"
            "씨앗 엑셀을 두고 `python -m src.generate.build_catalog` 를 실행하세요 (ADR-0002)."
        )
    return json.loads(SEED_CATALOG_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _catalog_arrays() -> dict[str, object]:
    """사전을 numpy 배열로 펼쳐 둔다 (하루마다 다시 만들지 않도록 캐시).

    Returns:
        상품 속성 배열들과, 그룹별 상품 인덱스·정규화 가중치.
    """
    catalog = load_catalog()
    products: list[dict[str, object]] = catalog["products"]  # type: ignore[assignment]

    frame = pd.DataFrame(products)
    group_index: dict[str, np.ndarray] = {}
    group_weights: dict[str, np.ndarray] = {}
    for group, subset in frame.groupby("group"):
        group_index[str(group)] = subset.index.to_numpy()
        weights = subset["weight"].to_numpy(dtype=float)
        group_weights[str(group)] = weights / weights.sum()

    qty_dist: dict[str, float] = catalog["qty_distribution"]  # type: ignore[assignment]
    qty_values = np.array([int(q) for q in qty_dist], dtype=np.int64)
    qty_probs = np.array(list(qty_dist.values()), dtype=float)

    return {
        "plu_cd": frame["plu_cd"].to_numpy(),
        "goods_nm": frame["goods_nm"].to_numpy(),
        "item_head": frame["item_head"].to_numpy(),
        "item_head_nm": frame["item_head_nm"].to_numpy(),
        "price": frame["price"].to_numpy(dtype=np.int64),
        "group_index": group_index,
        "group_weights": group_weights,
        "qty_values": qty_values,
        "qty_probs": qty_probs / qty_probs.sum(),
    }


@lru_cache(maxsize=len(HOURS))
def _group_probs_for_hour(hour: str) -> tuple[tuple[str, ...], np.ndarray]:
    """해당 시각의 품목 그룹 선택 확률을 만든다 (명세 6.4 시간대 보정).

    보정을 더하면 합이 1을 넘으므로 다시 정규화한다.

    Args:
        hour: ``HH`` 두 자리 시각.

    Returns:
        ``(그룹명 튜플, 확률 배열)``.
    """
    adjust = HOUR_GROUP_ADJUST.get(hour, {})
    groups = tuple(BASE_GROUP_PROBS)
    probs = np.array(
        [BASE_GROUP_PROBS[group] + adjust.get(group, 0.0) for group in groups], dtype=float
    )
    return groups, probs / probs.sum()


def block_share_from_profile(size_grade: str, block: str) -> float:
    """프로파일상 해당 시간 블록의 거래 가중치 합을 돌려준다 (명세 6.3 검증용).

    Args:
        size_grade: 등급 ``L``/``M``/``S``.
        block: ``아침``/``점심``/``저녁``.

    Returns:
        블록에 속한 시간대 가중치의 합 (프로파일 합이 100이므로 곧 백분율).
    """
    weights = dict(zip(HOURS, HOUR_PROFILES[size_grade], strict=True))
    return sum(weights[hour] for hour in TIME_BLOCKS[block])


# --- 달력 효과 -------------------------------------------------------------


def season_factor(saledate: str) -> float:
    """월 계절 계수를 만든다 (명세 6.2: sin 곡선 ±10%, 여름·연말 소폭 상승).

    한 해에 한 번 도는 곡선은 여름이 높으면 연말이 반드시 낮아져 명세의
    "여름·연말 상승"과 어긋난다. 그래서 **반년 주기** 코사인을 써서
    7월과 1월에 마루, 4월과 10월에 골이 오게 한다 (ADR-0006).

    Args:
        saledate: ``YYYYMMDD``.

    Returns:
        0.9 ~ 1.1 사이의 계수.
    """
    month = parse_date(saledate).month
    return 1.0 + SEASON_AMPLITUDE * math.cos(2 * math.pi * (month - 7) / 6)


def deal_count_for_day(store: StoreProfile, saledate: str, rng: np.random.Generator) -> int:
    """그 날 그 점포의 정상 거래 건수를 정한다 (명세 6.1·6.2).

    일평균에 요일 계수·계절 계수·일변동을 곱한다.

    Args:
        store: 점포 프로파일.
        saledate: ``YYYYMMDD``.
        rng: 그 날·그 점포 전용 난수 생성기.

    Returns:
        1 이상의 거래 건수.
    """
    base = store.avg_deals * DOW_FACTORS[dow_index(saledate)] * season_factor(saledate)
    jitter = rng.uniform(-store.variation, store.variation)
    return max(1, int(round(base * (1.0 + jitter))))


# --- 하루치 생성 -----------------------------------------------------------


def _draw_products(
    hours_of_line: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """상품 행마다 사전에서 상품 하나씩 고른다 (명세 6.4).

    같은 시각의 행을 묶어 한 번에 뽑고, 다시 그룹별로 묶어 한 번에 뽑는다
    (행 단위 반복 없음).

    Args:
        hours_of_line: 각 상품 행이 속한 영수증의 시각 ``HH`` 배열.
        rng: 그 날·그 점포 전용 난수 생성기.

    Returns:
        ``(상품 인덱스 배열, 수량 배열)``.
    """
    arrays = _catalog_arrays()
    total = hours_of_line.size
    product_idx = np.empty(total, dtype=np.int64)

    for hour in np.unique(hours_of_line):
        hour_mask = hours_of_line == hour
        groups, probs = _group_probs_for_hour(str(hour))
        chosen_groups = rng.choice(len(groups), size=int(hour_mask.sum()), p=probs)

        positions = np.flatnonzero(hour_mask)
        for group_no, group in enumerate(groups):
            group_mask = chosen_groups == group_no
            if not group_mask.any():
                continue
            candidates = arrays["group_index"][group]
            weights = arrays["group_weights"][group]
            product_idx[positions[group_mask]] = rng.choice(
                candidates, size=int(group_mask.sum()), p=weights
            )

    qty = rng.choice(arrays["qty_values"], size=total, p=arrays["qty_probs"])
    return product_idx, qty


def _build_payments(
    deal_amounts: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """영수증별 결제 행을 만든다 (명세 6.5: 단일 95% · 분할 5%).

    Args:
        deal_amounts: 영수증별 총액 배열 (양수).
        rng: 그 날·그 점포 전용 난수 생성기.

    Returns:
        ``(영수증별 결제 건수, 결제 금액 배열, 결제수단 배열)``.
        금액·수단 배열은 영수증 순서대로 펼쳐져 있다.
    """
    count = deal_amounts.size
    # 총액이 1원이면 쪼갤 수 없다 — 분할 대상에서 제외한다.
    splittable = deal_amounts >= 2
    is_split = (rng.random(count) < SPLIT_PAYMENT_PROB) & splittable
    tender_counts = np.where(is_split, 2, 1)

    first_part = np.where(
        is_split,
        rng.integers(1, np.maximum(deal_amounts, 2)),  # 1 이상, 총액 미만
        deal_amounts,
    )
    second_part = deal_amounts - first_part

    amounts = np.empty(int(tender_counts.sum()), dtype=np.int64)
    offsets = np.concatenate(([0], np.cumsum(tender_counts)[:-1]))
    amounts[offsets] = first_part
    amounts[offsets[is_split] + 1] = second_part[is_split]

    sections = rng.choice(TENDER_SECTIONS, size=amounts.size, p=TENDER_PROBS)
    return tender_counts, amounts, sections


def _assemble_frames(
    store: StoreProfile,
    saledate: str,
    posno: np.ndarray,
    seconds: np.ndarray,
    line_counts: np.ndarray,
    rng: np.random.Generator,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """거래 골격(POS·시각·행수)에서 원장 3종을 만든다. DEALNO는 아직 비워 둔다.

    Args:
        store: 점포 프로파일.
        saledate: ``YYYYMMDD``.
        posno: 거래별 POS 번호 배열.
        seconds: 거래별 자정 기준 초 배열.
        line_counts: 거래별 상품 행수 배열.
        rng: 그 날·그 점포 전용 난수 생성기.

    Returns:
        ``(영수증, 상품, 결제)`` 프레임. 세 프레임 모두 ``_ROW`` 임시 키로 이어진다.
    """
    arrays = _catalog_arrays()
    deal_count = posno.size
    row_id = np.arange(deal_count)

    # --- 상품 행 ---
    item_row_id = np.repeat(row_id, line_counts)
    hours_of_line = np.repeat((seconds // 3600).astype(int), line_counts)
    hours_of_line = np.char.zfill(hours_of_line.astype(str), 2)

    product_idx, qty = _draw_products(hours_of_line, rng)
    price = arrays["price"][product_idx]
    amount = price * qty

    items = pd.DataFrame(
        {
            "_ROW": item_row_id,
            "SEQ": np.concatenate([np.arange(1, n + 1) for n in line_counts]),
            "PLU_CD": arrays["plu_cd"][product_idx],
            "GOODS_NM": arrays["goods_nm"][product_idx],
            "ITEM_HEAD": arrays["item_head"][product_idx],
            "ITEM_HEAD_NM": arrays["item_head_nm"][product_idx],
            "SALEPRICE": price,
            "QTY": qty,
            "SALEAMOUNT": amount,
        }
    )

    deal_amounts = np.bincount(item_row_id, weights=amount, minlength=deal_count).astype(np.int64)

    # --- 결제 행 ---
    tender_counts, tender_amounts, tender_sections = _build_payments(deal_amounts, rng)
    payments = pd.DataFrame(
        {
            "_ROW": np.repeat(row_id, tender_counts),
            "SEQ": np.concatenate([np.arange(1, n + 1) for n in tender_counts]),
            "TENDERSECTION": tender_sections,
            "TENDERAMOUNT": tender_amounts,
        }
    )

    # --- 영수증 행 ---
    receipts = pd.DataFrame(
        {
            "_ROW": row_id,
            "DEPT_CD": store.dept_cd,
            "SALEDATE": saledate,
            "POSNO": np.char.mod("%d", posno),
            "SALETIME": _seconds_to_saletime(seconds),
            "DEALTYPE": DEALTYPE_NORMAL,
            "ITEMCNT": line_counts,
            "TENDERCNT": tender_counts,
            "DEALAMOUNT": deal_amounts,
        }
    )
    return receipts, items, payments


def _seconds_to_saletime(seconds: np.ndarray) -> np.ndarray:
    """자정 기준 초를 ``HHMMSS`` 문자열로 바꾼다.

    Args:
        seconds: 자정 기준 초 배열.

    Returns:
        ``HHMMSS`` 문자열 배열.
    """
    hour, remainder = np.divmod(seconds.astype(np.int64), 3600)
    minute, second = np.divmod(remainder, 60)
    return np.char.add(
        np.char.add(
            np.char.zfill(hour.astype(str), 2),
            np.char.zfill(minute.astype(str), 2),
        ),
        np.char.zfill(second.astype(str), 2),
    )


def _make_cancels(
    receipts: pd.DataFrame,
    items: pd.DataFrame,
    payments: pd.DataFrame,
    rng: np.random.Generator,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """정상 거래의 일부를 동일자 취소로 복제한다 (명세 6.5).

    원거래와 같은 ``SALEDATE``·같은 POS에서, ``SALETIME`` + 1~4시간 뒤에
    전 금액·수량을 음수로 뒤집은 거래를 만든다. 같은 날 안에서 상계되므로
    날짜 단위 DELETE→INSERT 멱등과 충돌하지 않는다.

    수량(QTY)도 음수로 만든다: 금액만 뒤집으면 상품 마트의 판매수량이
    상계되지 않아 "수량은 남고 매출은 0"인 상품이 생긴다 (ADR-0006).

    Args:
        receipts: 정상 영수증 (``_ROW`` 임시 키 보유).
        items: 정상 상품 행.
        payments: 정상 결제 행.
        rng: 그 날·그 점포 전용 난수 생성기.

    Returns:
        ``(취소 영수증, 취소 상품, 취소 결제)``. 취소 대상이 없으면 빈 프레임.
    """
    picked = rng.random(len(receipts)) < CANCEL_RATE
    if not picked.any():
        empty = receipts.iloc[:0].copy(), items.iloc[:0].copy(), payments.iloc[:0].copy()
        return empty

    source = receipts.loc[picked].copy()

    low, high = CANCEL_DELAY_HOURS
    delay = rng.integers(low, high + 1, size=len(source)) * 3600
    original_seconds = _saletime_to_seconds(source["SALETIME"].to_numpy())
    # 자정을 넘지 않도록 그날의 마지막 시각으로 자른다 (명세 6.5 "같은 SALEDATE").
    cancel_seconds = np.minimum(original_seconds + delay, LAST_SECOND_OF_DAY)

    cancel_receipts = source.assign(
        _ROW=lambda df: df["_ROW"] + len(receipts),
        _ORG_ROW=source["_ROW"].to_numpy(),
        SALETIME=_seconds_to_saletime(cancel_seconds),
        DEALAMOUNT=lambda df: -df["DEALAMOUNT"],
    )

    org_rows = source["_ROW"].to_numpy()
    row_shift = dict(zip(org_rows, org_rows + len(receipts), strict=True))

    cancel_items = items[items["_ROW"].isin(org_rows)].copy()
    cancel_items["_ROW"] = cancel_items["_ROW"].map(row_shift)
    cancel_items[["QTY", "SALEAMOUNT"]] = -cancel_items[["QTY", "SALEAMOUNT"]]

    cancel_payments = payments[payments["_ROW"].isin(org_rows)].copy()
    cancel_payments["_ROW"] = cancel_payments["_ROW"].map(row_shift)
    cancel_payments["TENDERAMOUNT"] = -cancel_payments["TENDERAMOUNT"]

    return cancel_receipts, cancel_items, cancel_payments


def _saletime_to_seconds(saletime: np.ndarray) -> np.ndarray:
    """``HHMMSS`` 문자열 배열을 자정 기준 초로 바꾼다.

    Args:
        saletime: ``HHMMSS`` 문자열 배열.

    Returns:
        자정 기준 초 배열.
    """
    text = pd.Series(saletime, dtype="string")
    return (
        text.str[:2].astype(int) * 3600
        + text.str[2:4].astype(int) * 60
        + text.str[4:6].astype(int)
    ).to_numpy()


def generate_day(store: StoreProfile, saledate: str) -> DayData:
    """그 날 그 점포의 원장 3종을 만든다 (명세 6장 전체).

    이 함수 안의 난수는 전부 ``derive_rng(dept_cd, saledate)`` 한 개에서 나온다.
    따라서 호출 순서·횟수와 무관하게 같은 인자면 같은 결과다 (불변식 4).

    Args:
        store: 점포 프로파일.
        saledate: ``YYYYMMDD``.

    Returns:
        명세 4장 DDL 컬럼을 그대로 가진 하루치 ``DayData``.
    """
    rng = derive_rng(store.dept_cd, saledate)

    deal_count = deal_count_for_day(store, saledate, rng)
    posno = rng.integers(1, store.pos_count + 1, size=deal_count)

    weights = np.array(store.hour_weights, dtype=float)
    hour_choice = rng.choice(len(HOURS), size=deal_count, p=weights / weights.sum())
    seconds = (
        (hour_choice + 5) * 3600
        + rng.integers(0, 60, size=deal_count) * 60
        + rng.integers(0, 60, size=deal_count)
    )

    line_counts = rng.choice(LINE_COUNTS, size=deal_count, p=LINE_COUNT_PROBS)

    receipts, items, payments = _assemble_frames(
        store, saledate, posno, seconds, line_counts, rng
    )
    cancel_receipts, cancel_items, cancel_payments = _make_cancels(
        receipts, items, payments, rng
    )

    receipts = pd.concat([receipts, cancel_receipts], ignore_index=True)
    items = pd.concat([items, cancel_items], ignore_index=True)
    payments = pd.concat([payments, cancel_payments], ignore_index=True)

    return _finalize(receipts, items, payments)


def _finalize(
    receipts: pd.DataFrame, items: pd.DataFrame, payments: pd.DataFrame
) -> DayData:
    """DEALNO를 매기고 DDL 컬럼 순서로 정리한다.

    DEALNO는 POS별로 **거래 시각 순서대로** 0001부터 붙인다 (명세 6.5).
    취소 거래도 이 순서에 섞이므로, 취소는 원거래보다 항상 큰 번호를 받는다.

    Args:
        receipts: 정상+취소 영수증 (``_ROW`` 임시 키 보유).
        items: 정상+취소 상품 행.
        payments: 정상+취소 결제 행.

    Returns:
        DDL 컬럼만 남긴 ``DayData``.
    """
    ordered = receipts.sort_values(["POSNO", "SALETIME", "_ROW"], kind="stable")
    sequence = ordered.groupby("POSNO").cumcount() + 1
    ordered = ordered.assign(DEALNO=sequence.map("{:04d}".format))

    # 취소 행의 ORG* 를 원거래의 확정 DEALNO로 채운다.
    dealno_by_row = dict(zip(ordered["_ROW"], ordered["DEALNO"], strict=True))
    org_row = ordered.get("_ORG_ROW")
    if org_row is None:
        ordered["_ORG_ROW"] = pd.NA
        org_row = ordered["_ORG_ROW"]

    is_cancel = org_row.notna()
    ordered["CANCELTYPE"] = np.where(is_cancel, CANCELTYPE_CANCELED, None)
    ordered["ORGSALEDATE"] = np.where(is_cancel, ordered["SALEDATE"], None)
    ordered["ORGPOSNO"] = np.where(is_cancel, ordered["POSNO"], None)
    ordered["ORGDEALNO"] = org_row.map(lambda row: dealno_by_row.get(row) if pd.notna(row) else None)

    key_columns = ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"]
    keys = ordered.set_index("_ROW")[key_columns]

    def attach(frame: pd.DataFrame) -> pd.DataFrame:
        """상품·결제 행에 확정된 영수증 키를 붙인다."""
        return frame.join(keys, on="_ROW").drop(columns="_ROW")

    receipt_columns = [
        "DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SALETIME", "DEALTYPE",
        "ITEMCNT", "TENDERCNT", "DEALAMOUNT", "CANCELTYPE",
        "ORGSALEDATE", "ORGPOSNO", "ORGDEALNO",
    ]
    item_columns = [
        "DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SEQ", "PLU_CD", "GOODS_NM",
        "ITEM_HEAD", "ITEM_HEAD_NM", "SALEPRICE", "QTY", "SALEAMOUNT",
    ]
    payment_columns = [
        "DEPT_CD", "SALEDATE", "POSNO", "DEALNO", "SEQ", "TENDERSECTION", "TENDERAMOUNT",
    ]

    return DayData(
        receipts=(
            ordered[receipt_columns]
            .sort_values(key_columns, kind="stable")
            .reset_index(drop=True)
        ),
        items=(
            attach(items)[item_columns]
            .sort_values([*key_columns, "SEQ"], kind="stable")
            .reset_index(drop=True)
        ),
        payments=(
            attach(payments)[payment_columns]
            .sort_values([*key_columns, "SEQ"], kind="stable")
            .reset_index(drop=True)
        ),
    )


def generate_period(
    from_date: str, to_date: str, dept_cds: Iterable[str] | None = None
) -> Iterator[DayData]:
    """기간 × 점포의 하루치를 차례로 흘린다.

    날짜마다 독립 생성이므로 어느 날짜부터 시작해도 결과가 같다 (불변식 4).

    Args:
        from_date: 시작일 ``YYYYMMDD`` (포함).
        to_date: 종료일 ``YYYYMMDD`` (포함).
        dept_cds: 생성할 점포코드. None이면 명세 6.1의 3곳 전부.

    Yields:
        하루치 ``DayData``.

    Raises:
        ValueError: 알 수 없는 점포코드가 들어왔을 때.
    """
    stores = STORES
    if dept_cds is not None:
        wanted = list(dept_cds)
        known = {store.dept_cd for store in STORES}
        unknown = sorted(set(wanted) - known)
        if unknown:
            raise ValueError(f"알 수 없는 점포코드: {unknown} (가능: {sorted(known)})")
        stores = tuple(store for store in STORES if store.dept_cd in set(wanted))

    for saledate in date_range(from_date, to_date):
        for store in stores:
            yield generate_day(store, saledate)


def store_dim_frame() -> pd.DataFrame:
    """``DIM_STORE`` 적재용 프레임을 만든다 (명세 6.1).

    Returns:
        DDL 컬럼 순서(``DEPT_CD``, ``DEPT_NM``, ``SIZE_GRADE``)의 프레임.
    """
    return pd.DataFrame(
        [
            {
                "DEPT_CD": store.dept_cd,
                "DEPT_NM": store.dept_nm,
                "SIZE_GRADE": store.size_grade,
            }
            for store in STORES
        ]
    )
