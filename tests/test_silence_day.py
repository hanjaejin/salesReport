"""S 점포의 침묵일 존재 검증 — 명세 10장 ``test_g6_silence_exists_for_store_s``.

명세 6.3이 시간대 프로파일을 점포별로 차등한 **이유 자체**를 검증하는 테스트다.
세 점포가 같은 분포를 쓰면 G2가 매일 발동해 "침묵하는 날"이 사라지고,
브리핑이 뻔해지는 실패를 데이터가 그대로 재현하게 된다.

13개월 전체를 만들어야 하므로 느리다 (약 1분). 데모의 성립 조건을 지키는
테스트라 표본으로 줄이지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from src.common.config import PERIOD_FROM, PERIOD_TO, get_engine
from src.extract.sample import SampleExtractor
from src.load import pipeline, schema

SMALL_STORE = "901003"


@pytest.fixture(scope="module")
def full_period_engine(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    """S 점포의 13개월치를 전부 구축한 엔진 (모듈 1회)."""
    engine = get_engine(tmp_path_factory.mktemp("silence") / "silence.db")
    pipeline.load_period(
        SampleExtractor(dept_cds=[SMALL_STORE]), PERIOD_FROM, PERIOD_TO, engine=engine
    )
    return engine


def _payloads(engine: Engine, dept_cd: str) -> list[dict]:
    """해당 점포의 모든 브리핑 JSON을 읽는다.

    Args:
        engine: 대상 엔진.
        dept_cd: 점포코드.

    Returns:
        계산 JSON 리스트.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            select(schema.BRIEFING_DAILY.c.PAYLOAD_JSON).where(
                schema.BRIEFING_DAILY.c.DEPT_CD == dept_cd
            )
        ).scalars()
        return [json.loads(raw) for raw in rows]


def test_g6_silence_exists_for_store_s(full_period_engine: Engine) -> None:
    """명세 10장: S 점포의 13개월 중 G6 침묵일이 1일 이상 존재한다."""
    payloads = _payloads(full_period_engine, SMALL_STORE)
    assert len(payloads) > 380, "13개월치가 만들어지지 않았다"

    silent_days = [
        payload["saledate"]
        for payload in payloads
        if {card["card_id"] for card in payload["cards"]} == {"G6"}
    ]

    assert silent_days, "S 점포에 침묵일이 하루도 없다 — 프로파일 차등이 무의미해졌다"


def test_store_s_expected_block_share_is_below_threshold() -> None:
    """명세 6.3: S 프로파일의 **기대** 최대 블록 비중이 임계 25% 미만이다.

    명세가 보장하는 것은 여기까지다 — 프로파일 설계값이 임계 아래라는 것.
    실제 하루하루의 매출 비중은 표본 변동 때문에 이 값 주위에서 흔들리며,
    S는 하루 80건뿐이라 흔들림이 크다 (표준편차 약 4.6%p). 그래서 어떤 날은
    25%를 넘어 G2가 발동한다. 이는 구현 결함이 아니라 임계값 설계의 성질이다.
    자세한 측정과 대응은 ADR-0008을 보라.
    """
    from src.generate import synth

    shares = {
        block: synth.block_share_from_profile("S", block) for block in synth.TIME_BLOCKS
    }

    assert max(shares.values()) < 25.0, f"S 프로파일 기대 비중 {shares}"


def test_store_s_briefings_are_not_uniform(full_period_engine: Engine) -> None:
    """명세 6.3의 목적: S의 브리핑이 매일 똑같지 않다.

    프로파일을 차등한 이유가 "브리핑이 뻔해지는 실패"를 피하는 것이므로,
    카드 조합이 여러 가지로 나오는지를 본다.
    """
    payloads = _payloads(full_period_engine, SMALL_STORE)

    combinations = {
        tuple(sorted(card["card_id"] for card in payload["cards"])) for payload in payloads
    }

    assert len(combinations) >= 3, f"카드 조합이 {combinations} 뿐이다 — 브리핑이 단조롭다"


def test_silence_day_lines_are_the_silent_templates(full_period_engine: Engine) -> None:
    """침묵일의 2·3줄이 실제로 미발동 문구다 (카드와 문장이 어긋나지 않는다)."""
    from src.mart import briefing

    payloads = _payloads(full_period_engine, SMALL_STORE)
    silent = next(
        payload
        for payload in payloads
        if {card["card_id"] for card in payload["cards"]} == {"G6"}
    )

    variant = silent["template_variant"]
    assert silent["briefing_lines"][1] == briefing.LINE2_TEMPLATES[f"silent_{variant}"]
    assert silent["briefing_lines"][2] == briefing.LINE3_SILENT


def test_early_period_uses_baseline_fallback(full_period_engine: Engine) -> None:
    """명세 7.4 폴백: 데이터 초기 구간은 요일 비교 없이 금액만 말한다."""
    payloads = {payload["saledate"]: payload for payload in _payloads(full_period_engine, SMALL_STORE)}

    first_day = payloads[PERIOD_FROM]

    assert first_day["dow_baseline_available"] is False
    assert first_day["dow_diff_pct"] is None
    assert first_day["briefing_lines"][0].endswith("원이었어요")
    assert "평소" not in first_day["briefing_lines"][0]


def test_late_period_has_baseline(full_period_engine: Engine) -> None:
    """충분히 쌓인 뒤에는 요일 기준선이 잡혀 비교 문구가 나온다."""
    payloads = {payload["saledate"]: payload for payload in _payloads(full_period_engine, SMALL_STORE)}

    last_day = payloads[PERIOD_TO]

    assert last_day["dow_baseline_available"] is True
    assert last_day["dow_baseline_amt"] > 0
    assert "평소" in last_day["briefing_lines"][0]


def test_briefing_exists_for_every_day(full_period_engine: Engine) -> None:
    """기간의 모든 날짜에 브리핑이 있다 (화면의 빈 상태가 뜨지 않도록)."""
    from src.common.dateutil import date_range

    payloads = _payloads(full_period_engine, SMALL_STORE)
    produced = {payload["saledate"] for payload in payloads}

    assert produced == set(date_range(PERIOD_FROM, PERIOD_TO))
