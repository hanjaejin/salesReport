"""브리핑 생성 검증 — 명세 7.2~7.4.

문장은 이 제품이 사용자에게 보여 주는 전부다. 임계값 경계와 문자열을
글자 단위로 고정해, 나중에 누가 "조금 다듬는" 것을 테스트가 막는다 (명세 14장).
"""

from __future__ import annotations

import ast
import inspect as inspect_module
import json
import re
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from src.common.config import G2_THRESHOLD_PCT, G4_THRESHOLD_PCT, get_engine
from src.extract.sample import SampleExtractor
from src.load import pipeline, schema
from src.mart import aggregate, briefing

FROM_DATE = "20260701"
TO_DATE = "20260714"


@pytest.fixture(scope="module")
def built_engine(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    """FACT·마트·브리핑까지 모두 만든 엔진 (모듈 1회).

    요일 기준선(직전 4주)이 채워지도록 기간보다 5주 앞부터 적재한다.
    """
    engine = get_engine(tmp_path_factory.mktemp("briefing") / "briefing.db")

    pipeline.load_period(SampleExtractor(), "20260527", TO_DATE, engine=engine)
    aggregate.build_marts(engine, "20260527", TO_DATE)
    briefing.build_briefings(engine, "20260527", TO_DATE)
    return engine


def _payload(engine: Engine, saledate: str, dept_cd: str) -> dict:
    """저장된 브리핑 JSON을 읽는다.

    Args:
        engine: 대상 엔진.
        saledate: ``YYYYMMDD``.
        dept_cd: 점포코드.

    Returns:
        계산 JSON.
    """
    with engine.connect() as connection:
        raw = connection.execute(
            select(schema.BRIEFING_DAILY.c.PAYLOAD_JSON).where(
                schema.BRIEFING_DAILY.c.SALEDATE == saledate,
                schema.BRIEFING_DAILY.c.DEPT_CD == dept_cd,
            )
        ).scalar_one()
    return json.loads(raw)


# --- 명세 7.3 임계값 경계 ---------------------------------------------------


@pytest.mark.parametrize(
    ("prev_diff_pct", "expected"),
    [(4.9, False), (5.0, True), (5.1, True), (-4.9, False), (-5.0, True), (-5.1, True)],
)
def test_g4_threshold(prev_diff_pct: float, expected: bool) -> None:
    """명세 7.3: G4는 abs(prev_diff_pct) >= 5.0 에서 발동한다."""
    assert briefing.g4_fires(prev_diff_pct) is expected
    assert G4_THRESHOLD_PCT == 5.0


def test_g4_does_not_fire_without_previous_day() -> None:
    """명세 7.4: 전일 데이터가 없으면 G4는 발동하지 않는다 (0나눗셈·예외 금지)."""
    assert briefing.g4_fires(None) is False


@pytest.mark.parametrize(
    ("share_pct", "expected"),
    [(24.9, False), (25.0, True), (25.1, True), (0.0, False)],
)
def test_g2_threshold(share_pct: float, expected: bool) -> None:
    """명세 7.3: G2는 peak_block.share_pct >= 25.0 에서 발동한다."""
    assert briefing.g2_fires(share_pct) is expected
    assert G2_THRESHOLD_PCT == 25.0


def test_cards_follow_priority_and_count() -> None:
    """명세 7.3: 카드는 최대 2개(G4·G2), 없으면 G6 1개. G4가 우선순위 1."""
    both = briefing.build_cards(prev_diff_pct=9.0, peak_share_pct=31.0, signal=None, block=None)
    assert [card["card_id"] for card in both] == ["G4", "G2"]

    only_g2 = briefing.build_cards(prev_diff_pct=1.0, peak_share_pct=31.0, signal=None, block=None)
    assert [card["card_id"] for card in only_g2] == ["G2"]

    only_g4 = briefing.build_cards(prev_diff_pct=9.0, peak_share_pct=10.0, signal=None, block=None)
    assert [card["card_id"] for card in only_g4] == ["G4"]

    silent = briefing.build_cards(prev_diff_pct=1.0, peak_share_pct=10.0, signal=None, block=None)
    assert [card["card_id"] for card in silent] == ["G6"]


# --- 명세 7.4 문자열 (변형 A / 변형 B) --------------------------------------

BASE_PAYLOAD = {
    "sale_amt": 1_234_000,
    "dow_name": "화요일",
    "dow_diff_pct": 12.0,
    "dow_diff_pct_abs": 12.0,
    "dow_baseline_available": True,
}


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        (0, "어제 1,234,000원 — 평소 화요일보다 12.0% 좋았어요 🔺"),
        (1, "어제 1,234,000원 — 평소 화요일보다 12.0% 잘 나온 하루였어요 🔺"),
    ],
)
def test_line1_rising(variant: int, expected: str) -> None:
    """명세 7.4: 1줄 상승형 (변형 A/B). ADR-0007에 따라 변형 B도 금액 접두를 갖는다."""
    assert briefing.render_line1(BASE_PAYLOAD, variant) == expected


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        (0, "어제 1,234,000원 — 평소 화요일보다 7.4% 아쉬웠어요 🔻"),
        (1, "어제 1,234,000원 — 평소 화요일보다 7.4% 조용한 하루였어요 🔻"),
    ],
)
def test_line1_falling(variant: int, expected: str) -> None:
    """명세 7.4: 1줄 하락형은 절댓값을 쓴다 (JSON에 미리 담긴 값 — ADR-0007)."""
    payload = {**BASE_PAYLOAD, "dow_diff_pct": -7.4, "dow_diff_pct_abs": 7.4}
    assert briefing.render_line1(payload, variant) == expected


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        (0, "어제 1,234,000원 — 평소 화요일 수준이었어요"),
        (1, "어제 1,234,000원 — 평소 화요일과 비슷했어요"),
    ],
)
def test_line1_flat(variant: int, expected: str) -> None:
    """명세 7.4: ±3% 안이면 보합 문구."""
    payload = {**BASE_PAYLOAD, "dow_diff_pct": 1.2, "dow_diff_pct_abs": 1.2}
    assert briefing.render_line1(payload, variant) == expected


@pytest.mark.parametrize("dow_diff_pct", [3.0, -3.0])
def test_line1_threshold_is_inclusive(dow_diff_pct: float) -> None:
    """명세 7.4: ±3 은 경계 포함 — 3.0이면 증감 문구가 나온다."""
    payload = {**BASE_PAYLOAD, "dow_diff_pct": dow_diff_pct, "dow_diff_pct_abs": 3.0}
    line = briefing.render_line1(payload, 0)
    assert "수준이었어요" not in line


@pytest.mark.parametrize("variant", [0, 1])
def test_line1_baseline_fallback(variant: int) -> None:
    """명세 7.4 폴백: 직전 4주 표본이 4개 미만이면 요일 비교를 생략한다."""
    payload = {**BASE_PAYLOAD, "dow_baseline_available": False}

    line = briefing.render_line1(payload, variant)

    assert line == "어제 1,234,000원이었어요"
    assert "평소" not in line


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        (
            0,
            "아침(07~09시)에 하루 매출의 31.0%가 나와요 — 그 전에 진열을 확인해 보세요",
        ),
        (
            1,
            "아침 손님이 몰리기 전(07~09시)에 진열을 한 번 봐주세요 — "
            "하루 매출의 31.0%가 이때 나와요",
        ),
    ],
)
def test_line2_when_g2_fires(variant: int, expected: str) -> None:
    """명세 7.4: 2줄 G2 발동형 (변형 A/B는 전체 교체)."""
    card = {
        "card_id": "G2",
        "lines": {"block_name": "아침", "block_range": "07~09시", "share_pct": 31.0},
    }
    assert briefing.render_line2(card, variant) == expected


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        (0, "오늘은 평소 준비대로 하시면 충분해요"),
        (1, "특별한 준비 없이 평소처럼 하시면 돼요"),
    ],
)
def test_line2_when_g2_silent(variant: int, expected: str) -> None:
    """명세 7.4: 2줄 미발동형."""
    assert briefing.render_line2(None, variant) == expected


def test_line3_when_g4_fires() -> None:
    """명세 7.4: 3줄은 단일 문형이며 비교 기준을 문장에 명시한다."""
    card = {
        "card_id": "G4",
        "lines": {
            "subject_name": "손님 수",
            "subject_amt": 929,
            "subject_unit": "명",
            "subject_pct": 8.0,
            "support_name": "1인당 구매액",
            "support_particle": "은",
            "support_amt": 3229,
            "support_unit": "원",
            "support_pct": -3.1,
            "support_pct_abs": 3.1,
            "support_direction": "줄었어요",
            "support_shape": "change",
        },
    }

    assert briefing.render_line3(card) == (
        "그저께와 비교하면, 손님 수(929명, 8.0%) 영향이 컸어요 — "
        "1인당 구매액은 3,229원으로 3.1% 줄었어요"
    )


def test_line3_particle_agrees_with_support_word() -> None:
    """명세 7.4의 고정 조사 "는"은 "1인당 구매액"과 맞지 않는다 (ADR-0007 결정 3).

    받침 유무에 따라 은/는을 계산 계층에서 골라 두므로 비문이 나오지 않는다.
    """
    ticket_support = briefing.pick_signal(9.0, 1.0, deal_cnt=929, avg_ticket=3229)
    cnt_support = briefing.pick_signal(1.0, 9.0, deal_cnt=929, avg_ticket=3229)

    assert ticket_support["support_particle"] == "은"  # 보조어=1인당 구매액
    assert cnt_support["support_particle"] == "는"  # 보조어=손님 수

    line = briefing.render_line3({"card_id": "G4", "lines": cnt_support})
    assert line.endswith("손님 수는 929명으로 1.0% 늘었어요")


def test_line3_when_g4_silent() -> None:
    """명세 7.4: 3줄 미발동형."""
    assert briefing.render_line3(None) == "특별한 신호는 없어요"


def test_g4_subject_is_the_larger_absolute_change() -> None:
    """명세 7.4: 건수·1인당 구매액 중 절댓값이 큰 쪽이 주어가 된다."""
    signal = briefing.pick_signal(
        cnt_diff_pct=3.0, ticket_diff_pct=-9.5, deal_cnt=929, avg_ticket=3229
    )
    assert signal["subject_name"] == "1인당 구매액"
    assert signal["subject_pct"] == -9.5
    assert signal["support_name"] == "손님 수"
    assert signal["support_pct"] == 3.0

    reversed_signal = briefing.pick_signal(
        cnt_diff_pct=-9.5, ticket_diff_pct=3.0, deal_cnt=929, avg_ticket=3229
    )
    assert reversed_signal["subject_name"] == "손님 수"


def test_no_forbidden_jargon_in_templates() -> None:
    """명세 14장·9장: 화면 문장에 전문용어가 없다 (객단가 → 1인당 구매액)."""
    templates = " ".join(
        [
            *briefing.LINE1_TEMPLATES.values(),
            *briefing.LINE2_TEMPLATES.values(),
            *briefing.LINE3_G4_TEMPLATES.values(),
            briefing.LINE3_SILENT,
        ]
    )

    for word in ("객단가", "증감률", "AI", "LLM", "분석", "예측"):
        assert word not in templates, f"금지 용어 '{word}' 가 템플릿에 있다"


def test_no_cause_assertion_in_templates() -> None:
    """명세 2장 원칙 5: 원인을 단정하지 않는다 ("~때문입니다" 금지)."""
    templates = " ".join(
        [
            *briefing.LINE1_TEMPLATES.values(),
            *briefing.LINE2_TEMPLATES.values(),
            *briefing.LINE3_G4_TEMPLATES.values(),
            briefing.LINE3_SILENT,
        ]
    )

    for word in ("때문", "탓", "원인은"):
        assert word not in templates


# --- 불변식 1: 문장 계층은 계산하지 않는다 -----------------------------------


def test_render_functions_contain_no_arithmetic() -> None:
    """불변식 1(정적 검사): 렌더 함수 안에 산술 연산이 없다.

    문장 계층은 ``str.format`` 치환만 한다. 반올림·절댓값·비교조차
    계산 계층에서 끝나 있어야 한다 (ADR-0007).
    """
    forbidden_calls = {"abs", "round", "sum", "min", "max", "int", "float"}

    for function in (briefing.render_line1, briefing.render_line2, briefing.render_line3):
        tree = ast.parse(textwrap.dedent(inspect_module.getsource(function)))
        definition = tree.body[0]
        assert isinstance(definition, ast.FunctionDef)

        # 시그니처와 docstring을 빼고 실행되는 본문만 본다.
        statements = definition.body[1:]

        for node in ast.walk(ast.Module(body=statements, type_ignores=[])):
            assert not isinstance(node, ast.BinOp), (
                f"{function.__name__} 안에 이항 산술 연산이 있다 — 불변식 1 위반: "
                f"{ast.unparse(node)}"
            )
            assert not isinstance(node, (ast.UAdd, ast.USub)), (
                f"{function.__name__} 안에 부호 연산이 있다 — 불변식 1 위반"
            )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, (
                    f"{function.__name__} 안에서 '{node.func.id}()' 를 부른다 — 불변식 1 위반"
                )


def test_briefing_lines_use_only_json_numbers(built_engine: Engine) -> None:
    """명세 10장: 3줄 안의 모든 숫자가 계산 JSON 값 집합에 존재한다."""
    for dept_cd in ("901001", "901002", "901003"):
        payload = _payload(built_engine, TO_DATE, dept_cd)
        allowed = _numeric_strings(payload)

        for line in payload["briefing_lines"]:
            for number in re.findall(r"-?\d[\d,]*(?:\.\d+)?", line):
                assert number in allowed, (
                    f"[{dept_cd}] '{number}' 가 JSON 값 집합에 없다\n"
                    f"  문장: {line}\n  허용: {sorted(allowed)}"
                )


def _numeric_strings(payload: dict) -> set[str]:
    """계산 JSON에 등장하는 모든 수치를 문장에 쓰이는 표기로 펼친다.

    수치 필드뿐 아니라 **문자열 값 안의 숫자**도 모은다. ``peak_block.range``
    ("07~09시")나 ``support_name``("1인당 구매액")처럼 숫자를 품은 문자열도
    JSON에 담긴 값이므로, 문장에 나타나는 것이 정상이다.

    Args:
        payload: 계산 JSON.

    Returns:
        문장에 나타날 수 있는 숫자 문자열 집합.
    """
    found: set[str] = set()

    def walk(node: object) -> None:
        """중첩 구조를 훑으며 숫자를 모은다."""
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            found.update({str(node), f"{node:,}"})
        elif isinstance(node, str):
            found.update(re.findall(r"-?\d[\d,]*(?:\.\d+)?", node))
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


# --- 명세 7.2 JSON / 저장 ---------------------------------------------------


def test_briefing_numbers_match_mart(built_engine: Engine) -> None:
    """명세 10장: briefing JSON의 수치가 마트 재조회값과 일치한다."""
    with built_engine.connect() as connection:
        rows = connection.execute(
            select(
                schema.MART_DAY_STORE.c.DEPT_CD,
                schema.MART_DAY_STORE.c.SALE_AMT,
                schema.MART_DAY_STORE.c.DEAL_CNT,
                schema.MART_DAY_STORE.c.AVG_TICKET,
            ).where(schema.MART_DAY_STORE.c.SALEDATE == TO_DATE)
        ).all()

    assert rows
    for dept_cd, sale_amt, deal_cnt, avg_ticket in rows:
        payload = _payload(built_engine, TO_DATE, dept_cd)
        assert payload["sale_amt"] == sale_amt
        assert payload["deal_cnt"] == deal_cnt
        assert payload["avg_ticket"] == round(avg_ticket)


def test_payload_has_spec_fields(built_engine: Engine) -> None:
    """명세 7.2: 계산 JSON이 규정된 필드를 모두 갖는다."""
    payload = _payload(built_engine, TO_DATE, "901001")

    for field in (
        "saledate", "dept_cd", "dept_nm", "sale_amt", "deal_cnt", "avg_ticket",
        "dow_name", "dow_baseline_amt", "dow_diff_pct", "prev_amt", "prev_diff_pct",
        "cnt_diff_pct", "ticket_diff_pct", "peak_block", "top5", "hourly",
        "cards", "briefing_lines",
    ):
        assert field in payload, f"명세 7.2 필드 '{field}' 누락"

    assert set(payload["peak_block"]) == {"name", "range", "share_pct"}
    assert len(payload["briefing_lines"]) == 3
    assert len(payload["top5"]) <= 5


def test_percentages_rounded_to_one_decimal(built_engine: Engine) -> None:
    """명세 7.4 표기 규칙: 모든 %는 소수 1자리로 저장 시점에 반올림된다."""
    payload = _payload(built_engine, TO_DATE, "901001")

    for field in ("dow_diff_pct", "prev_diff_pct", "cnt_diff_pct", "ticket_diff_pct"):
        value = payload[field]
        if value is not None:
            assert round(value, 1) == value, f"{field}={value} 가 소수 1자리가 아니다"

    assert round(payload["peak_block"]["share_pct"], 1) == payload["peak_block"]["share_pct"]


def test_amounts_are_integers(built_engine: Engine) -> None:
    """명세 7.4 표기 규칙: 금액과 1인당 구매액은 정수 원이다."""
    payload = _payload(built_engine, TO_DATE, "901001")

    assert isinstance(payload["sale_amt"], int)
    assert isinstance(payload["avg_ticket"], int)
    assert isinstance(payload["prev_amt"], int)
    assert isinstance(payload["dow_baseline_amt"], int)


def test_hourly_covers_business_hours(built_engine: Engine) -> None:
    """명세 9장: 시간대 차트가 영업시간 전체를 덮는다 (빈 시간도 0으로)."""
    payload = _payload(built_engine, TO_DATE, "901001")

    hours = [entry["hour"] for entry in payload["hourly"]]
    assert hours == [f"{hour:02d}" for hour in range(5, 24)]


# --- 로테이션 결정성 --------------------------------------------------------


def test_template_rotation_is_deterministic(built_engine: Engine) -> None:
    """명세 7.4: 변형 선택이 (날짜, 점포) 파생 시드로 정해져 재현된다."""
    first = briefing.template_variant("901001", TO_DATE)
    second = briefing.template_variant("901001", TO_DATE)

    assert first == second
    assert first in (0, 1)


def test_template_rotation_actually_varies() -> None:
    """로테이션이 실제로 두 변형을 모두 쓴다 (한쪽으로 쏠리지 않는다)."""
    variants = {
        briefing.template_variant("901001", f"202607{day:02d}") for day in range(1, 32)
    }

    assert variants == {0, 1}


def test_rebuild_produces_identical_briefings(tmp_path: Path) -> None:
    """브리핑을 다시 만들어도 문장이 같다 (멱등 — 명세 12장 라이브 시연)."""
    engine = get_engine(tmp_path / "rebuild.db")
    pipeline.load_period(SampleExtractor(dept_cds=["901003"]), FROM_DATE, TO_DATE, engine=engine)
    aggregate.build_marts(engine, FROM_DATE, TO_DATE)

    briefing.build_briefings(engine, FROM_DATE, TO_DATE)
    first = _payload(engine, TO_DATE, "901003")

    briefing.build_briefings(engine, FROM_DATE, TO_DATE)
    second = _payload(engine, TO_DATE, "901003")

    assert first == second


def test_build_briefings_is_row_idempotent(tmp_path: Path) -> None:
    """두 번 만들어도 BRIEFING_DAILY 행이 늘지 않는다."""
    engine = get_engine(tmp_path / "rows.db")
    pipeline.load_period(SampleExtractor(dept_cds=["901003"]), FROM_DATE, FROM_DATE, engine=engine)
    aggregate.build_marts(engine, FROM_DATE, FROM_DATE)

    briefing.build_briefings(engine, FROM_DATE, FROM_DATE)
    briefing.build_briefings(engine, FROM_DATE, FROM_DATE)

    with engine.connect() as connection:
        count = connection.execute(
            select(schema.BRIEFING_DAILY.c.SALEDATE)
        ).all()

    assert len(count) == 1


# --- 명세 12장 데모 조건 ----------------------------------------------------


def test_stores_produce_different_briefings(built_engine: Engine) -> None:
    """명세 12장: 세 점포의 브리핑이 서로 다르다."""
    lines = {
        dept_cd: tuple(_payload(built_engine, TO_DATE, dept_cd)["briefing_lines"])
        for dept_cd in ("901001", "901002", "901003")
    }

    assert len(set(lines.values())) == 3, f"같은 브리핑이 나온 점포가 있다: {lines}"


def test_store_l_peak_block_is_morning(built_engine: Engine) -> None:
    """명세 12장: L 점포의 최대 시간 블록은 아침이다 (프로파일 차등의 결과).

    부록 A 도입 후 2줄은 G3(결품)와 G2(시간대)가 나눠 갖는다. G3가 우선하므로
    "L은 항상 G2"는 더 이상 참이 아니다 — 최대 블록이 아침이라는 사실은 그대로다.
    """
    payload = _payload(built_engine, TO_DATE, "901001")

    assert payload["peak_block"]["name"] == "아침"
    assert payload["peak_block"]["share_pct"] >= G2_THRESHOLD_PCT


def test_line2_is_owned_by_g3_or_g2(built_engine: Engine) -> None:
    """2줄은 G3 또는 G2 하나가 차지한다 — 둘이 동시에 카드에 들어가지 않는다 (부록 A.4)."""
    for dept_cd in ("901001", "901002", "901003"):
        payload = _payload(built_engine, TO_DATE, dept_cd)
        card_ids = {card["card_id"] for card in payload["cards"]}

        assert not ({"G3", "G2"} <= card_ids), f"[{dept_cd}] G3와 G2가 함께 있다"
        assert len(payload["cards"]) <= 2


def test_store_m_fires_lunch_g2(built_engine: Engine) -> None:
    """명세 12장: M 점포는 점심 블록이 최대다."""
    payload = _payload(built_engine, TO_DATE, "901002")

    assert payload["peak_block"]["name"] == "점심"


# --- 3줄 금액 표기 (ADR-0014) ------------------------------------------------


def test_line3_shows_amount_next_to_percent() -> None:
    """3줄이 백분율만이 아니라 **금액**도 말한다.

    "1인당 구매액은 13.2%였어요" 는 금액을 백분율로 서술하는 비문이었다.
    """
    signal = briefing.pick_signal(
        cnt_diff_pct=14.7, ticket_diff_pct=13.2, deal_cnt=929, avg_ticket=3229
    )

    line = briefing.render_line3({"card_id": "G4", "lines": signal})

    assert "3,229원" in line
    assert "929명" in line
    assert "13.2%" in line


def test_line3_says_direction_in_words() -> None:
    """증감을 부호가 아니라 말로 알린다 — "-4.1% 줄었어요" 같은 겹말을 막는다."""
    rose = briefing.pick_signal(
        cnt_diff_pct=14.7, ticket_diff_pct=13.2, deal_cnt=929, avg_ticket=3229
    )
    fell = briefing.pick_signal(
        cnt_diff_pct=14.7, ticket_diff_pct=-4.1, deal_cnt=929, avg_ticket=3229
    )
    flat = briefing.pick_signal(
        cnt_diff_pct=14.7, ticket_diff_pct=0.0, deal_cnt=929, avg_ticket=3229
    )

    assert "13.2% 늘었어요" in briefing.render_line3({"card_id": "G4", "lines": rose})
    fell_line = briefing.render_line3({"card_id": "G4", "lines": fell})
    assert "4.1% 줄었어요" in fell_line
    assert "-4.1%" not in fell_line
    assert "같았어요" in briefing.render_line3({"card_id": "G4", "lines": flat})


def test_line3_units_follow_the_subject() -> None:
    """손님 수는 '명', 1인당 구매액은 '원' 을 쓴다."""
    ticket_subject = briefing.pick_signal(
        cnt_diff_pct=2.1, ticket_diff_pct=13.2, deal_cnt=929, avg_ticket=3229
    )

    line = briefing.render_line3({"card_id": "G4", "lines": ticket_subject})

    assert line.startswith("그저께와 비교하면, 1인당 구매액(3,229원, 13.2%)")
    assert "손님 수는 929명으로 2.1% 늘었어요" in line


def test_line3_amounts_come_from_json_not_the_template() -> None:
    """금액은 계산 계층이 넣어 준 값이다 — 문장 계층은 치환만 한다 (불변식 1)."""
    signal = briefing.pick_signal(
        cnt_diff_pct=1.0, ticket_diff_pct=9.0, deal_cnt=351, avg_ticket=3370
    )

    assert signal["subject_amt"] == 3370
    assert signal["support_amt"] == 351
    assert signal["subject_unit"] == "원"
    assert signal["support_unit"] == "명"


def test_line3_flat_support_drops_the_percent() -> None:
    """변화가 없으면 백분율을 말하지 않는다 — "0.0% 같았어요" 는 군더더기다.

    1줄이 ``flat`` 형태를 따로 두는 것과 같은 방식이다 (명세 7.4).
    """
    flat = briefing.pick_signal(
        cnt_diff_pct=16.0, ticket_diff_pct=0.0, deal_cnt=70, avg_ticket=3189
    )

    line = briefing.render_line3({"card_id": "G4", "lines": flat})

    assert "0.0%" not in line
    assert line.endswith("1인당 구매액은 3,189원으로 그저께와 거의 같았어요")


# --- 부록 B: 그룹 브리핑 (관리자 화면) ---------------------------------------


def _group(engine: Engine, saledate: str = FROM_DATE) -> dict:
    """저장된 그룹 요약을 읽는다.

    Args:
        engine: 대상 엔진.
        saledate: ``YYYYMMDD``.

    Returns:
        그룹 요약 JSON.
    """
    with engine.connect() as connection:
        raw = connection.execute(
            select(schema.BRIEFING_DAILY_GROUP.c.PAYLOAD_JSON).where(
                schema.BRIEFING_DAILY_GROUP.c.SALEDATE == saledate
            )
        ).scalar_one()
    return json.loads(raw)


def _store_payloads(engine: Engine, saledate: str = FROM_DATE) -> dict[str, dict]:
    """그 날짜의 매장별 브리핑을 전부 읽는다.

    Args:
        engine: 대상 엔진.
        saledate: ``YYYYMMDD``.

    Returns:
        점포코드 → 계산 JSON.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                schema.BRIEFING_DAILY.c.DEPT_CD, schema.BRIEFING_DAILY.c.PAYLOAD_JSON
            ).where(schema.BRIEFING_DAILY.c.SALEDATE == saledate)
        ).all()
    return {code: json.loads(raw) for code, raw in rows}


def test_group_totals_match_store_sum(built_engine: Engine) -> None:
    """부록 B.5: 그룹 합계 = 매장별 값의 합."""
    group = _group(built_engine)
    stores = _store_payloads(built_engine)

    assert group["total_sale_amt"] == sum(p["sale_amt"] for p in stores.values())
    assert group["total_deal_cnt"] == sum(p["deal_cnt"] for p in stores.values())
    assert group["store_count"] == len(stores)


def test_group_avg_ticket_is_total_based(built_engine: Engine) -> None:
    """부록 B.5: 1인당 = 총매출 ÷ 총손님. 매장별 1인당의 평균이 아니다.

    손님 수가 다르므로 두 값은 다르다. 큰 매장이 더 크게 반영돼야 한다.
    """
    group = _group(built_engine)

    assert group["group_avg_ticket"] == round(
        group["total_sale_amt"] / group["total_deal_cnt"]
    )


def test_group_avg_ticket_handles_zero_deals() -> None:
    """부록 B.5: 손님이 0명이면 1인당은 0이다 (0 나눗셈 없음)."""
    assert briefing.group_avg_ticket(0, 0) == 0
    assert briefing.group_avg_ticket(1000, 0) == 0


def test_group_status_matches_store_cards(built_engine: Engine) -> None:
    """부록 B.5: ``status`` 는 매장 브리핑의 2줄을 차지한 카드와 항상 일치한다."""
    group = _group(built_engine)
    stores = _store_payloads(built_engine)

    for row in group["stores"]:
        card_ids = {card["card_id"] for card in stores[row["dept_cd"]]["cards"]}
        expected = "STOCK" if "G3" in card_ids else ("PEAK" if "G2" in card_ids else "CALM")
        assert row["status"] == expected, row["dept_cd"]


def test_group_store_order_is_by_sale_amt(built_engine: Engine) -> None:
    """부록 B.4: 매장 행은 매출 내림차순 고정 — 매일 순서가 바뀌면 못 읽는다."""
    amounts = [row["sale_amt"] for row in _group(built_engine)["stores"]]

    assert amounts == sorted(amounts, reverse=True)


def test_attention_follows_card_priority() -> None:
    """부록 B.4: G3 > G2 > 없음. 동순위면 매출이 큰 매장이 앞선다."""
    rows = [
        {"dept_cd": "A", "status": "PEAK", "sale_amt": 900},
        {"dept_cd": "B", "status": "STOCK", "sale_amt": 100},
        {"dept_cd": "C", "status": "STOCK", "sale_amt": 500},
    ]

    assert briefing.pick_attention(rows) == "C"


def test_attention_is_none_when_all_calm() -> None:
    """부록 B.4: 아무 매장도 해당 없으면 비운다 — 침묵할 줄 아는 것이 신뢰 조건이다."""
    rows = [
        {"dept_cd": "A", "status": "CALM", "sale_amt": 900},
        {"dept_cd": "B", "status": "CALM", "sale_amt": 100},
    ]

    assert briefing.pick_attention(rows) is None


def test_group_briefing_is_idempotent(built_engine: Engine) -> None:
    """불변식 3: 같은 기간을 다시 만들어도 결과가 같다."""
    before = _group(built_engine)

    briefing.build_briefings(built_engine, FROM_DATE, TO_DATE)

    assert _group(built_engine) == before


def test_group_status_text_uses_no_jargon(built_engine: Engine) -> None:
    """부록 B.6: 상태 라벨에 전문용어가 없다."""
    texts = " ".join(row["status_text"] for row in _group(built_engine)["stores"])

    for word in ("객단가", "증감률", "결품", "리드타임", "소진", "발주", "분석", "예측"):
        assert word not in texts


def test_group_share_pct_sums_to_hundred(built_engine: Engine) -> None:
    """부록 B.10: 매장 비중 합계가 100%에 수렴한다 (반올림 오차 허용)."""
    shares = [row["share_pct"] for row in _group(built_engine)["stores"]]

    assert abs(sum(shares) - 100.0) <= 0.3


def test_group_share_pct_is_zero_without_sales() -> None:
    """부록 B.10: 매출이 0원이면 비중은 0이다 (0 나눗셈 없음)."""
    assert briefing.share_pct(0, 0) == 0.0
    assert briefing.share_pct(100, 0) == 0.0
    assert briefing.share_pct(25, 200) == 12.5
