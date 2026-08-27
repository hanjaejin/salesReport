"""정적 HTML 스냅샷 검증 — 명세 15장 보험 2.

스냅샷의 존재 이유는 **발표장 인터넷 전면 장애**다. 따라서 가장 중요한 검증은
"파일이 만들어졌다"가 아니라 **바깥을 한 번도 쳐다보지 않는다**는 것이다.
외부 참조가 하나라도 있으면 그날 그 화면은 깨진 채로 뜬다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from src.common.config import get_engine
from src.extract.sample import SampleExtractor
from src.load import pipeline, schema
from src.report import snapshot

SALEDATE = "20260703"
STORES = ("901001", "901002", "901003")


@pytest.fixture(scope="module")
def built_engine(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    """스냅샷을 만들 수 있는 상태의 엔진 (모듈 1회)."""
    engine = get_engine(tmp_path_factory.mktemp("snapshot") / "snapshot.db")
    pipeline.load_period(SampleExtractor(), "20260620", "20260710", engine=engine)
    return engine


@pytest.fixture(scope="module")
def pages(built_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    """스냅샷 전체를 한 번 만들어 재사용한다."""
    out_dir = tmp_path_factory.mktemp("pages")
    return snapshot.build_snapshots(built_engine, SALEDATE, out_dir)


def _text(page: Path) -> str:
    """스냅샷 파일 내용을 읽는다.

    Args:
        page: 파일 경로.

    Returns:
        HTML 원문.
    """
    return page.read_text(encoding="utf-8")


def _payload(engine: Engine, dept_cd: str) -> dict:
    """저장된 브리핑 JSON을 읽는다.

    Args:
        engine: 대상 엔진.
        dept_cd: 점포코드.

    Returns:
        계산 JSON.
    """
    with engine.connect() as connection:
        raw = connection.execute(
            select(schema.BRIEFING_DAILY.c.PAYLOAD_JSON).where(
                schema.BRIEFING_DAILY.c.SALEDATE == SALEDATE,
                schema.BRIEFING_DAILY.c.DEPT_CD == dept_cd,
            )
        ).scalar_one()
    return json.loads(raw)


# --- 자체 완결성 (명세 15장 보험 2의 존재 이유) ------------------------------


def test_pages_have_no_external_references(pages: list[Path]) -> None:
    """서버·인터넷 없이 열린다: 바깥을 가리키는 참조가 하나도 없다."""
    patterns = {
        "절대 URL": re.compile(r"""["'(]\s*(?:https?:)?//""", re.IGNORECASE),
        "외부 스크립트": re.compile(r"<script[^>]*\ssrc=", re.IGNORECASE),
        "외부 스타일시트": re.compile(r"<link[^>]*\srel=[\"']?stylesheet", re.IGNORECASE),
        "@import": re.compile(r"@import", re.IGNORECASE),
        "폰트 다운로드": re.compile(r"@font-face", re.IGNORECASE),
    }

    for page in pages:
        html = _text(page)
        for label, pattern in patterns.items():
            found = pattern.search(html)
            assert found is None, (
                f"{page.name} 에 {label} 이 있다: {html[max(0, found.start() - 40): found.end() + 40]!r}"
            )


def test_pages_link_only_to_each_other(pages: list[Path]) -> None:
    """링크는 같은 폴더 안의 스냅샷끼리만 건다 (USB에 통째로 담아도 동작)."""
    names = {page.name for page in pages}

    for page in pages:
        for href in re.findall(r'href="([^"]+)"', _text(page)):
            assert href in names, f"{page.name} → 알 수 없는 링크 {href!r}"


def test_pages_are_standalone_html(pages: list[Path]) -> None:
    """각 파일이 그 자체로 완결된 HTML 문서다."""
    for page in pages:
        html = _text(page)
        assert html.lstrip().lower().startswith("<!doctype html>")
        assert 'charset="utf-8"' in html.lower()
        assert "</html>" in html.lower()
        assert page.stat().st_size > 1000


def test_snapshot_count_matches_spec(pages: list[Path]) -> None:
    """명세 15장: 브리핑·자세히·보고서·관리자 4장 + 목차."""
    names = [page.name for page in pages]

    assert len(pages) == 5, names
    assert names[0] == "index.html"
    for keyword in ("브리핑", "자세히", "보고서", "관리자"):
        assert any(keyword in name for name in names), f"{keyword} 스냅샷 없음"


# --- 내용: 저장된 값을 그대로 옮긴다 (재계산 없음) ---------------------------


def test_briefing_page_shows_all_three_stores(
    pages: list[Path], built_engine: Engine
) -> None:
    """브리핑 스냅샷이 세 점포의 3줄을 저장된 그대로 담는다."""
    page = next(p for p in pages if "브리핑" in p.name)
    html = _text(page)

    for dept_cd in STORES:
        payload = _payload(built_engine, dept_cd)
        assert payload["dept_nm"] in html
        for line in payload["briefing_lines"]:
            assert line in html, f"[{dept_cd}] 문장이 스냅샷에 없다: {line}"


def test_briefing_page_proves_stores_differ(pages: list[Path], built_engine: Engine) -> None:
    """DoD 증빙: 세 점포의 브리핑이 서로 다르다는 것이 한 장에 담긴다."""
    lines = {
        dept_cd: tuple(_payload(built_engine, dept_cd)["briefing_lines"])
        for dept_cd in STORES
    }
    assert len(set(lines.values())) == 3

    html = _text(next(p for p in pages if "브리핑" in p.name))
    assert "G6" in html or "침묵" in html, "S 점포의 침묵 상태가 드러나야 한다"


def test_detail_page_numbers_match_briefing(pages: list[Path], built_engine: Engine) -> None:
    """자세히 스냅샷의 수치가 브리핑 JSON과 같다 (숫자 불일치 원천 차단)."""
    payload = _payload(built_engine, "901001")
    html = _text(next(p for p in pages if "자세히" in p.name))

    assert f"{payload['sale_amt']:,}" in html
    assert f"{payload['deal_cnt']:,}" in html
    assert f"{payload['avg_ticket']:,}" in html


def test_report_page_matches_xlsx_source(pages: list[Path], built_engine: Engine) -> None:
    """보고서 스냅샷이 xlsx와 같은 자료를 쓴다."""
    from src.report import daily_report

    payload, top_items, hourly = daily_report.fetch_report_data(
        built_engine, SALEDATE, "901002"
    )
    html = _text(next(p for p in pages if "보고서" in p.name))

    assert f"{payload['sale_amt']:,}" in html
    for goods_nm in top_items["GOODS_NM"]:
        assert goods_nm in html
    assert len(hourly) > 0


def test_admin_page_shows_identical_totals(pages: list[Path]) -> None:
    """관리자 스냅샷이 재생성 전/후 수치가 같음을 보여 준다 (멱등 시연 보존)."""
    html = _text(next(p for p in pages if "관리자" in p.name))

    assert "실행 전" in html
    assert "실행 후" in html
    assert snapshot.IDEMPOTENT_MESSAGE in html


def test_charts_are_inline_svg(pages: list[Path]) -> None:
    """차트가 인라인 SVG다 — 그림 파일도 차트 라이브러리도 필요 없다."""
    html = _text(next(p for p in pages if "자세히" in p.name))

    assert "<svg" in html
    assert "<img" not in html


# --- 명세 9장·14장 규율은 스냅샷에도 적용된다 --------------------------------


def test_pages_carry_mockup_badge(pages: list[Path]) -> None:
    """명세 9장: 목업 고지가 모든 장에 상시 노출된다."""
    for page in pages:
        assert "목업" in _text(page), f"{page.name} 에 목업 배지가 없다"


def test_pages_use_no_jargon(pages: list[Path]) -> None:
    """명세 14장: 스냅샷에도 전문용어를 노출하지 않는다."""
    for page in pages:
        html = _text(page)
        for word in ("객단가", "증감률", "LLM", "머신러닝"):
            assert word not in html, f"{page.name} 에 금지 용어 '{word}'"


def test_pages_carry_footer_disclaimer(pages: list[Path]) -> None:
    """명세 9장 하단 고지가 유지된다."""
    from src.app import main

    for page in pages:
        assert main.FOOTER_NOTE in _text(page)


# --- 재현성 ----------------------------------------------------------------


def test_snapshots_are_reproducible(built_engine: Engine, tmp_path: Path) -> None:
    """같은 DB·같은 기준일이면 같은 스냅샷이 나온다.

    단 관리자 장의 "소요 N초"는 **실측값**이라 실행마다 다른 것이 정상이다.
    재현되어야 하는 것은 데이터이지 측정 시간이 아니므로 그 부분만 정규화해 비교한다.
    """
    elapsed = re.compile(r"소요 [\d.]+초")

    first = snapshot.build_snapshots(built_engine, SALEDATE, tmp_path / "a")
    second = snapshot.build_snapshots(built_engine, SALEDATE, tmp_path / "b")

    for left, right in zip(first, second, strict=True):
        assert left.name == right.name
        assert elapsed.sub("소요 N초", _text(left)) == elapsed.sub("소요 N초", _text(right)), (
            f"{left.name} 이 재생성마다 달라진다"
        )


def test_admin_snapshot_records_measured_elapsed(pages: list[Path]) -> None:
    """관리자 장은 실제로 잰 소요 시간을 남긴다 (시연의 근거)."""
    html = _text(next(p for p in pages if "관리자" in p.name))

    assert re.search(r"소요 [\d.]+초", html), "소요 시간이 기록되지 않았다"


def test_missing_date_raises(built_engine: Engine, tmp_path: Path) -> None:
    """브리핑이 없는 날짜는 빈 스냅샷을 만들지 않고 실패한다."""
    with pytest.raises(LookupError):
        snapshot.build_snapshots(built_engine, "20991231", tmp_path / "none")


def test_html_escapes_store_names(built_engine: Engine, tmp_path: Path) -> None:
    """DB 값이 HTML로 새지 않는다 (이스케이프)."""
    with built_engine.begin() as connection:
        connection.execute(
            schema.DIM_STORE.update()
            .where(schema.DIM_STORE.c.DEPT_CD == "901003")
            .values(DEPT_NM="<script>alert(1)</script>")
        )
        connection.execute(
            schema.BRIEFING_DAILY.delete().where(
                schema.BRIEFING_DAILY.c.DEPT_CD == "901003"
            )
        )

    from src.mart import briefing

    briefing.build_briefings(built_engine, SALEDATE, SALEDATE, dept_cds=["901003"])
    pages = snapshot.build_snapshots(built_engine, SALEDATE, tmp_path / "escaped")

    html = _text(next(p for p in pages if "브리핑" in p.name))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html

    # 원상 복구 (모듈 픽스처를 공유하므로)
    with built_engine.begin() as connection:
        connection.execute(
            schema.DIM_STORE.update()
            .where(schema.DIM_STORE.c.DEPT_CD == "901003")
            .values(DEPT_NM="간이역 소형점")
        )
    briefing.build_briefings(built_engine, SALEDATE, SALEDATE, dept_cds=["901003"])


# --- CLI -------------------------------------------------------------------


def test_cli_writes_snapshots(built_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 진입점이 스냅샷을 파일로 남긴다."""
    monkeypatch.setattr(snapshot, "get_engine", lambda *a, **k: built_engine)

    out_dir = tmp_path / "cli"
    exit_code = snapshot.main(["--date", SALEDATE, "--out", str(out_dir)])

    assert exit_code == 0
    assert len(list(out_dir.glob("*.html"))) == 5
