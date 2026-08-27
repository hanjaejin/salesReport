"""씨앗 엑셀에서 상품 사전을 추출해 JSON으로 동결한다 (ADR-0002).

이 스크립트는 **파이프라인 실행 경로에 없다.** 씨앗 엑셀이 갱신됐을 때만 손으로 돌린다.
산출물 ``seed_catalog.json`` 이 저장소에 커밋되고, 합성 생성기는 그 JSON만 읽는다.
그래야 엑셀 원본 없이 클론한 환경(클라우드 배포)에서도 생성이 재현된다.

실행:
    python -m src.generate.build_catalog
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pandas as pd

from src.common.config import DATA_DIR, SEED_CATALOG_PATH
from src.common.logger import get_logger
from src.generate.catalog_spec import (
    CATALOG_SIZE,
    EXCLUDED_HEAD_NM,
    GROUP_TO_HEAD_NM,
    MIN_PRODUCTS_PER_GROUP,
    PLU_PATTERN,
)

logger = get_logger(__name__)

#: 씨앗 엑셀 (명세 1장이 말한 "씨앗" — 데이터가 아니라 구조·사전의 근거)
SEED_EXCEL: Path = DATA_DIR / "DATA_영수증데이터.xlsx"

#: 씨앗 추출 일자 — 재실행해도 JSON이 흔들리지 않도록 상수로 고정한다
EXTRACTED_AT = "2026-08-27"


def _head_nm_to_group() -> dict[str, str]:
    """대분류명 → 확률 그룹 역인덱스를 만든다.

    Returns:
        대분류명을 키로, 확률 그룹명을 값으로 갖는 매핑.
    """
    return {
        head_nm: group
        for group, head_nms in GROUP_TO_HEAD_NM.items()
        for head_nm in head_nms
    }


def load_seed(excel_path: Path = SEED_EXCEL) -> pd.DataFrame:
    """씨앗 엑셀을 읽어 숫자 컬럼을 정리한다.

    Args:
        excel_path: 씨앗 엑셀 경로.

    Returns:
        원본 컬럼을 유지한 DataFrame (금액·수량은 숫자형).

    Raises:
        FileNotFoundError: 씨앗 엑셀이 없을 때. 이미 동결된 JSON이 있으면
            이 스크립트를 실행할 필요가 없다는 뜻이기도 하다.
    """
    if not excel_path.exists():
        raise FileNotFoundError(
            f"씨앗 엑셀이 없습니다: {excel_path}\n"
            f"이미 동결된 {SEED_CATALOG_PATH.name} 이 있다면 실행하지 않아도 됩니다 (ADR-0002)."
        )

    frame = pd.read_excel(excel_path, dtype=str)
    for column in ("SALEPRICE", "QTY", "SALEAMOUNT"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["SALEPRICE", "QTY"])


def build_qty_distribution(frame: pd.DataFrame) -> dict[str, float]:
    """상품 1줄당 수량(QTY)의 경험 분포를 만든다.

    명세는 영수증당 **행수** 분포만 정했고 행당 **수량** 분포는 정하지 않았다.
    임의로 정하는 대신 씨앗의 실측 분포를 그대로 쓴다 (씨앗의 목적에 부합).
    꼬리(4 이상)는 4로 몰아 안정시킨다.

    Args:
        frame: 씨앗 DataFrame.

    Returns:
        수량 문자열을 키로, 확률을 값으로 갖는 딕셔너리. 값의 합은 1.0.
    """
    clipped = frame["QTY"].astype(int).clip(lower=1, upper=4)
    counts = Counter(clipped.tolist())
    total = sum(counts.values())
    return {str(qty): round(counts[qty] / total, 6) for qty in sorted(counts)}


def build_products(frame: pd.DataFrame) -> list[dict[str, object]]:
    """씨앗에서 상품 사전을 뽑는다.

    필터 순서:
        1. 명세 6.4의 PLU 형식(88 + 11자리)에 맞지 않는 상품 제외
        2. 담배·주류 대분류 제외 (ADR-0002)
        3. 확률 그룹에 매핑되지 않는 대분류 제외
        4. 판매빈도 상위로 잘라 총 ``CATALOG_SIZE`` 종, 단 그룹마다
           최소 ``MIN_PRODUCTS_PER_GROUP`` 종은 보장

    Args:
        frame: 씨앗 DataFrame.

    Returns:
        상품 딕셔너리 리스트. 그룹·판매빈도(weight)·대표가를 포함한다.

    Raises:
        ValueError: 어떤 그룹이든 최소 상품 수를 못 채울 때. 씨앗이 바뀌어
            사전이 조용히 빈약해지는 것을 막는다.
    """
    head_to_group = _head_nm_to_group()
    plu_re = re.compile(PLU_PATTERN)

    usable = frame[
        frame["PLU_CD"].str.match(plu_re, na=False)
        & ~frame["ITEM_HEAD_NM"].isin(EXCLUDED_HEAD_NM)
        & frame["ITEM_HEAD_NM"].isin(head_to_group)
    ]

    grouped = (
        usable.groupby(["PLU_CD", "GOODS_NM", "ITEM_HEAD", "ITEM_HEAD_NM"], as_index=False)
        .agg(weight=("QTY", "size"), price=("SALEPRICE", "median"))
        .assign(group=lambda df: df["ITEM_HEAD_NM"].map(head_to_group))
        .sort_values(["weight", "PLU_CD"], ascending=[False, True])
    )

    # 그룹별 하한을 먼저 확보한 뒤, 남은 자리를 전체 판매빈도 순으로 채운다.
    guaranteed = grouped.groupby("group", group_keys=False).head(MIN_PRODUCTS_PER_GROUP)

    short = [
        group
        for group, count in guaranteed["group"].value_counts().items()
        if count < MIN_PRODUCTS_PER_GROUP
    ]
    missing = sorted(set(GROUP_TO_HEAD_NM) - set(guaranteed["group"]))
    if short or missing:
        raise ValueError(
            f"그룹별 최소 {MIN_PRODUCTS_PER_GROUP}종을 채우지 못했습니다. "
            f"부족: {short} / 아예 없음: {missing}"
        )

    remaining = grouped[~grouped["PLU_CD"].isin(guaranteed["PLU_CD"])]
    selected = pd.concat(
        [guaranteed, remaining.head(max(0, CATALOG_SIZE - len(guaranteed)))]
    ).sort_values(["group", "weight", "PLU_CD"], ascending=[True, False, True])

    return [
        {
            "plu_cd": row.PLU_CD,
            "goods_nm": row.GOODS_NM,
            "item_head": row.ITEM_HEAD,
            "item_head_nm": row.ITEM_HEAD_NM,
            "price": int(row.price),
            "group": row.group,
            "weight": int(row.weight),
        }
        for row in selected.itertuples(index=False)
    ]


def build_catalog(excel_path: Path = SEED_EXCEL) -> dict[str, object]:
    """씨앗에서 사전 전체(메타 + 수량분포 + 상품목록)를 만든다.

    Args:
        excel_path: 씨앗 엑셀 경로.

    Returns:
        ``seed_catalog.json`` 에 그대로 직렬화할 딕셔너리.
    """
    frame = load_seed(excel_path)
    products = build_products(frame)
    receipts = frame.groupby(["POSNO", "DEALNO"]).ngroups

    return {
        "meta": {
            "설명": "씨앗 엑셀에서 추출해 동결한 상품 사전. build_catalog.py가 생성한다 (ADR-0002).",
            "source_file": excel_path.name,
            "source_dept_cd": sorted(frame["DEPT_CD"].unique().tolist()),
            "source_rows": int(len(frame)),
            "source_receipts": int(receipts),
            "source_unique_products": int(frame["PLU_CD"].nunique()),
            "extracted_at": EXTRACTED_AT,
            "excluded_head_nm": list(EXCLUDED_HEAD_NM),
            "plu_pattern": PLU_PATTERN,
            "selected_products": len(products),
            "avg_lines_per_receipt": round(len(frame) / receipts, 4),
        },
        "qty_distribution": build_qty_distribution(frame),
        "products": products,
    }


def main() -> None:
    """사전을 만들어 ``seed_catalog.json`` 에 쓴다."""
    catalog = build_catalog()
    SEED_CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    meta = catalog["meta"]
    logger.info(
        "상품 사전 생성 완료: %d종 (씨앗 %d행·영수증 %d건·고유상품 %d종) → %s",
        meta["selected_products"],
        meta["source_rows"],
        meta["source_receipts"],
        meta["source_unique_products"],
        SEED_CATALOG_PATH,
    )
    for group, count in sorted(Counter(str(p["group"]) for p in catalog["products"]).items()):
        logger.info("  그룹 %-5s %3d종", group, count)


if __name__ == "__main__":
    main()
