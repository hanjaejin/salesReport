"""정적 HTML 스냅샷 — 명세 15장 보험 2.

발표장 인터넷이 전면 장애일 때 "녹화 시연 + 스냅샷 설명"으로 완주하기 위한 저장본이다.
따라서 이 모듈의 유일한 절대 규칙은 **바깥을 한 번도 쳐다보지 않는 것**이다:
CDN도, 그림 파일도, 폰트 다운로드도 없다. 차트는 인라인 SVG로 직접 그린다.
파일을 USB에 통째로 담아 더블클릭하면 열린다.

값은 화면과 마찬가지로 **저장된 것을 옮기기만** 한다 — 브리핑 문장은
``BRIEFING_DAILY`` 의 글자 그대로이고, 수치는 마트에서 읽은 그대로다 (불변식 1·7).

실행:
    python -m src.report.snapshot --date 20260609
"""

from __future__ import annotations

import argparse
import html
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, select

from src.app.main import (
    FOOTER_NOTE,
    MOCKUP_BADGE,
    NO_STOCK_RISK,
    TREND_DAYS,
    arrow_text,
    format_display_date,
)
from src.common.config import DATA_DIR, get_engine
from src.common.dateutil import shift_days
from src.common.logger import get_logger
from src.load import schema
from src.report.daily_report import fetch_report_data

logger = get_logger(__name__)

#: 스냅샷 기본 저장 위치
SNAPSHOT_DIR: Path = DATA_DIR / "snapshot"

#: 관리자 스냅샷이 담는 결론 문구 (화면과 같은 말)
IDEMPOTENT_MESSAGE = "몇 번을 다시 만들어도 같은 결과예요"

#: 관리자 재생성 구간 길이 (명세 9장)
ADMIN_REGEN_DAYS = 7

#: 파일명 — 목차가 첫 장이고 나머지는 발표 순서대로 번호를 붙인다
PAGE_INDEX = "index.html"
PAGE_BRIEFING = "01_브리핑.html"
PAGE_DETAIL = "02_자세히.html"
PAGE_REPORT = "03_보고서.html"
PAGE_ADMIN = "04_관리자.html"

#: 스냅샷에서 상세·보고서를 보여 줄 대표 점포
DETAIL_STORE = "901001"
REPORT_STORE = "901002"

_STYLE = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1c2733;--muted:#5c6b7a;
--line:#dfe4ea;--navy:#22324a;--cream:#fff3df;--gold:#e0b15e;
--up:#c0392b;--down:#2266cc}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 64px;background:var(--bg);color:var(--ink);
font-family:"Malgun Gothic","맑은 고딕","Apple SD Gothic Neo","Noto Sans KR",
system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.65}
.wrap{max-width:980px;margin:0 auto}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;
justify-content:space-between;border-bottom:2px solid var(--navy);padding-bottom:14px}
h1{font-size:26px;margin:0}
h2{font-size:20px;margin:36px 0 12px}
h3{font-size:16px;margin:24px 0 8px;color:var(--muted)}
.badge{display:inline-block;background:var(--cream);border:1px solid var(--gold);
color:#5c430f;border-radius:999px;padding:4px 14px;font-size:14px;font-weight:700}
.meta{color:var(--muted);font-size:14px}
nav{margin:18px 0 8px;display:flex;flex-wrap:wrap;gap:10px}
nav a{display:inline-block;padding:8px 16px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--navy);text-decoration:none;font-size:14px}
nav a:hover{background:var(--cream)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:20px 24px;margin:16px 0}
.store-name{font-size:15px;font-weight:700;color:var(--navy);margin-bottom:4px}
.cards-tag{font-size:12px;color:var(--muted);font-weight:400;margin-left:8px}
.line{font-size:19px;font-weight:700;margin:10px 0}
.metrics{display:flex;flex-wrap:wrap;gap:16px;margin:8px 0}
.metric{flex:1 1 190px;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:16px 20px}
.metric .label{font-size:14px;color:var(--muted)}
.metric .value{font-size:26px;font-weight:800;margin:4px 0}
.metric .delta{font-size:14px}
table{border-collapse:collapse;width:100%;background:var(--card);
border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line);font-size:15px}
th{background:var(--navy);color:#dce7f5;font-weight:700}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.same{background:var(--cream);border:1px solid var(--gold);color:#5c430f;
border-radius:10px;padding:14px 18px;font-weight:700;margin-top:14px}
.note{color:var(--muted);font-size:13px;margin-top:8px}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);
color:var(--muted);font-size:13px}
svg{display:block;max-width:100%;height:auto;background:var(--card);
border:1px solid var(--line);border-radius:12px}
"""


def _esc(value: object) -> str:
    """DB에서 온 값을 HTML에 안전하게 넣는다.

    점포명·상품명은 원천 데이터라 태그가 섞여 들어올 수 있다고 보고 항상 이스케이프한다.

    Args:
        value: 넣을 값.

    Returns:
        이스케이프된 문자열.
    """
    return html.escape(str(value), quote=True)


def _page(title: str, subtitle: str, body: str, active: str) -> str:
    """공통 뼈대를 씌워 완결된 HTML 문서를 만든다.

    Args:
        title: 문서 제목.
        subtitle: 상단 우측 보조 문구.
        body: 본문 HTML.
        active: 현재 페이지 파일명 (목차 링크에서 표시하지 않는다).

    Returns:
        완결된 HTML 문서.
    """
    links = "".join(
        f'<a href="{name}">{_esc(label)}</a>'
        for name, label in (
            (PAGE_INDEX, "목차"),
            (PAGE_BRIEFING, "① 브리핑"),
            (PAGE_DETAIL, "② 자세히"),
            (PAGE_REPORT, "③ 보고서"),
            (PAGE_ADMIN, "④ 관리자"),
        )
        if name != active
    )

    return (
        "<!doctype html>\n"
        '<html lang="ko">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n<body>\n"
        f'<div class="wrap">\n'
        f"<header><h1>{_esc(title)}</h1>"
        f'<div><span class="badge">{_esc(MOCKUP_BADGE)}</span></div></header>\n'
        f'<p class="meta">{_esc(subtitle)}</p>\n'
        f"<nav>{links}</nav>\n"
        f"{body}\n"
        f"<footer>{_esc(FOOTER_NOTE)}<br>"
        "인터넷·서버 없이 열리는 저장본입니다 (명세 15장 보험 2).</footer>\n"
        "</div>\n</body>\n</html>\n"
    )


def bar_chart_svg(labels: Sequence[str], values: Sequence[int], *, height: int = 240) -> str:
    """막대차트를 인라인 SVG로 그린다.

    Args:
        labels: 가로축 라벨.
        values: 막대 높이가 될 값.
        height: 전체 높이(px).

    Returns:
        ``<svg>`` 문자열.
    """
    width, pad_x, pad_y = 940, 44, 26
    plot_w, plot_h = width - pad_x * 2, height - pad_y * 2
    peak = max([*values, 1])
    slot = plot_w / max(len(values), 1)
    bar_w = max(slot * 0.62, 2)

    bars = []
    for index, (label, value) in enumerate(zip(labels, values, strict=True)):
        bar_h = plot_h * max(value, 0) / peak
        x = pad_x + slot * index + (slot - bar_w) / 2
        y = pad_y + plot_h - bar_h
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
            f'rx="3" fill="#22324a"></rect>'
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" font-size="11" '
            f'fill="#5c6b7a" text-anchor="middle">{_esc(label)}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="시간대별 매출">'
        f'<line x1="{pad_x}" y1="{pad_y + plot_h}" x2="{width - pad_x}" '
        f'y2="{pad_y + plot_h}" stroke="#dfe4ea"></line>'
        f'<text x="{pad_x}" y="{pad_y - 8}" font-size="11" fill="#5c6b7a">'
        f"최대 {peak:,}원</text>" + "".join(bars) + "</svg>"
    )


def line_chart_svg(labels: Sequence[str], values: Sequence[int], *, height: int = 240) -> str:
    """선차트를 인라인 SVG로 그린다.

    Args:
        labels: 가로축 라벨 (날짜).
        values: 선의 값.
        height: 전체 높이(px).

    Returns:
        ``<svg>`` 문자열.
    """
    width, pad_x, pad_y = 940, 52, 26
    plot_w, plot_h = width - pad_x * 2, height - pad_y * 2
    peak, floor = max([*values, 1]), min([*values, 0])
    span = max(peak - floor, 1)
    step = plot_w / max(len(values) - 1, 1)

    points = []
    dots = []
    for index, value in enumerate(values):
        x = pad_x + step * index
        y = pad_y + plot_h - plot_h * (value - floor) / span
        points.append(f"{x:.1f},{y:.1f}")
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#22324a"></circle>')

    ticks = []
    for index, label in enumerate(labels):
        if index % max(len(labels) // 7, 1) and index != len(labels) - 1:
            continue
        x = pad_x + step * index
        ticks.append(
            f'<text x="{x:.1f}" y="{height - 8}" font-size="11" fill="#5c6b7a" '
            f'text-anchor="middle">{_esc(label[4:6])}/{_esc(label[6:8])}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="최근 매출 흐름">'
        f'<line x1="{pad_x}" y1="{pad_y + plot_h}" x2="{width - pad_x}" '
        f'y2="{pad_y + plot_h}" stroke="#dfe4ea"></line>'
        f'<text x="{pad_x}" y="{pad_y - 8}" font-size="11" fill="#5c6b7a">'
        f"최대 {peak:,}원</text>"
        f'<polyline points="{" ".join(points)}" fill="none" stroke="#e0b15e" '
        f'stroke-width="2.5"></polyline>' + "".join(dots) + "".join(ticks) + "</svg>"
    )


# --- 조회 (저장된 것을 읽기만 한다) -----------------------------------------


def _load_payloads(engine: Engine, saledate: str) -> dict[str, dict]:
    """그 날의 전 점포 브리핑을 읽는다.

    Args:
        engine: 대상 엔진.
        saledate: ``YYYYMMDD``.

    Returns:
        점포코드 → 계산 JSON.

    Raises:
        LookupError: 그 날짜의 브리핑이 하나도 없을 때.
    """
    import json

    with engine.connect() as connection:
        rows = connection.execute(
            select(schema.BRIEFING_DAILY.c.DEPT_CD, schema.BRIEFING_DAILY.c.PAYLOAD_JSON)
            .where(schema.BRIEFING_DAILY.c.SALEDATE == saledate)
            .order_by(schema.BRIEFING_DAILY.c.DEPT_CD)
        ).all()

    if not rows:
        raise LookupError(f"이 날짜의 브리핑이 없습니다: {saledate}")
    return {dept_cd: json.loads(raw) for dept_cd, raw in rows}


def _load_trend(engine: Engine, saledate: str, dept_cd: str) -> pd.DataFrame:
    """최근 추이를 읽는다.

    Args:
        engine: 대상 엔진.
        saledate: 기준일 ``YYYYMMDD``.
        dept_cd: 점포코드.

    Returns:
        ``SALEDATE``·``SALE_AMT`` 프레임.
    """
    table = schema.MART_DAY_STORE
    start = shift_days(saledate, 0 - TREND_DAYS + 1)

    with engine.connect() as connection:
        return pd.read_sql(
            select(table.c.SALEDATE, table.c.SALE_AMT)
            .where(table.c.DEPT_CD == dept_cd, table.c.SALEDATE.between(start, saledate))
            .order_by(table.c.SALEDATE),
            connection,
        )


# --- 각 장 -----------------------------------------------------------------


def render_briefing_page(payloads: dict[str, dict], saledate: str) -> str:
    """① 브리핑 — 세 점포의 3줄을 한 장에 담는다.

    데모의 핵심 주장("점포마다 다른 브리핑")이 한눈에 드러나도록 나란히 놓는다.

    Args:
        payloads: 점포코드 → 계산 JSON.
        saledate: ``YYYYMMDD``.

    Returns:
        완결된 HTML 문서.
    """
    blocks = []
    for payload in payloads.values():
        card_ids = "·".join(card["card_id"] for card in payload["cards"])
        lines = "".join(f'<p class="line">{_esc(line)}</p>' for line in payload["briefing_lines"])
        blocks.append(
            f'<div class="card"><div class="store-name">🏪 {_esc(payload["dept_nm"])}'
            f'<span class="cards-tag">발동 {_esc(card_ids)} · '
            f'최대 시간대 {_esc(payload["peak_block"]["name"])} '
            f'{payload["peak_block"]["share_pct"]}%</span></div>{lines}</div>'
        )

    body = (
        "<h2>오늘의 브리핑</h2>"
        "<p class=\"note\">아침에 점포장이 30초 동안 읽는 화면입니다. "
        "문장은 새벽 배치가 만들어 저장해 둔 것을 그대로 표시합니다.</p>"
        + "".join(blocks)
        + '<p class="note">세 점포의 문장이 서로 다릅니다 — 시간대 프로파일이 다르기 때문입니다. '
        "신호가 없는 점포는 <strong>G6</strong>으로 침묵합니다.</p>"
    )
    return _page(
        "① 브리핑 — 세 점포",
        f"기준일 {format_display_date(saledate)}",
        body,
        PAGE_BRIEFING,
    )


def render_detail_page(payload: dict, trend: pd.DataFrame) -> str:
    """② 자세히 — 요약·시간대·TOP5·최근 흐름.

    Args:
        payload: 대표 점포의 계산 JSON.
        trend: 최근 추이 프레임.

    Returns:
        완결된 HTML 문서.
    """
    metrics = "".join(
        f'<div class="metric"><div class="label">{_esc(label)}</div>'
        f'<div class="value">{value:,}{_esc(unit)}</div>'
        f'<div class="delta">{_esc(arrow_text(diff))}</div></div>'
        for label, value, unit, diff in (
            ("어제 매출", payload["sale_amt"], "원", payload["prev_diff_pct"]),
            ("손님 수", payload["deal_cnt"], "명", payload["cnt_diff_pct"]),
            ("1인당 구매액", payload["avg_ticket"], "원", payload["ticket_diff_pct"]),
        )
    )

    hourly = payload["hourly"]
    top_rows = "".join(
        f"<tr><td>{rank}</td><td>{_esc(item['goods_nm'])}</td>"
        f'<td class="num">{item["sale_amt"]:,}원</td>'
        f'<td class="num">{item["qty"]:,}개</td></tr>'
        for rank, item in enumerate(payload["top5"], start=1)
    )
    risk_rows = "".join(
        f"<tr><td>{_esc(item['goods_nm'])}</td>"
        f'<td class="num">{item["stock_qty"]:,}개</td>'
        f'<td class="num">{item["sale_average_qty"]}개</td></tr>'
        for item in payload.get("stock_risk", {}).get("items", [])
    )

    body = (
        "<h2>자세히 보기</h2>"
        f'<p class="note">🏪 {_esc(payload["dept_nm"])} · 화면에서는 접혀 있고, '
        "궁금할 때만 펼치는 영역입니다.</p>"
        f'<div class="metrics">{metrics}</div>'
        "<h3>시간대별 매출</h3>"
        + bar_chart_svg(
            [entry["hour"] for entry in hourly], [entry["sale_amt"] for entry in hourly]
        )
        + "<h3>많이 팔린 상품</h3>"
        '<table><tr><th>순위</th><th>상품</th><th class="num">매출</th>'
        f'<th class="num">판매수량</th></tr>{top_rows}</table>'
        + "<h3>곧 떨어질 수 있는 상품</h3>"
        + (
            '<table><tr><th>상품</th><th class="num">남은 재고</th>'
            f'<th class="num">하루 평균 판매</th></tr>{risk_rows}</table>'
            if risk_rows
            else f'<p class="note">{NO_STOCK_RISK}</p>'
        )
        + f"<h3>최근 {TREND_DAYS}일 흐름</h3>"
        + line_chart_svg(trend["SALEDATE"].tolist(), trend["SALE_AMT"].tolist())
    )
    return _page(
        "② 자세히 보기",
        f"기준일 {format_display_date(payload['saledate'])} · {payload['dept_nm']}",
        body,
        PAGE_DETAIL,
    )


def render_report_page(payload: dict, top_items: pd.DataFrame, hourly: pd.DataFrame) -> str:
    """③ 보고서 — 내려받는 xlsx와 같은 내용을 그대로 보여 준다.

    Args:
        payload: 계산 JSON.
        top_items: 상위 상품 프레임.
        hourly: 시간대 프레임.

    Returns:
        완결된 HTML 문서.
    """
    summary_rows = "".join(
        f"<tr><td>{_esc(label)}</td>"
        f'<td class="num">{value:,}{_esc(unit)}</td>'
        f'<td class="num">{"-" if diff is None else f"{diff}%"}</td></tr>'
        for label, value, unit, diff in (
            ("매출", payload["sale_amt"], "원", payload["prev_diff_pct"]),
            ("손님 수", payload["deal_cnt"], "명", payload["cnt_diff_pct"]),
            ("1인당 구매액", payload["avg_ticket"], "원", payload["ticket_diff_pct"]),
        )
    )
    top_rows = "".join(
        f"<tr><td>{rank}</td><td>{_esc(row.GOODS_NM)}</td><td>{_esc(row.ITEM_HEAD_NM)}</td>"
        f'<td class="num">{int(row.SALE_AMT):,}원</td>'
        f'<td class="num">{int(row.QTY):,}개</td></tr>'
        for rank, row in enumerate(top_items.itertuples(index=False), start=1)
    )
    hour_rows = "".join(
        f"<tr><td>{_esc(row.HOUR)}시</td>"
        f'<td class="num">{int(row.SALE_AMT):,}원</td>'
        f'<td class="num">{int(row.DEAL_CNT):,}명</td></tr>'
        for row in hourly.itertuples(index=False)
    )
    briefing_lines = "".join(f"<li>{_esc(line)}</li>" for line in payload["briefing_lines"])

    body = (
        "<h2>일일 보고</h2>"
        f'<p class="note">화면의 [보고서 내려받기(.xlsx)] 버튼이 만드는 파일과 같은 내용입니다. '
        f"파일명: 일일보고_{_esc(payload['dept_nm'])}_{_esc(payload['saledate'])}.xlsx</p>"
        "<h3>요약</h3>"
        '<table><tr><th>항목</th><th class="num">값</th>'
        f'<th class="num">그저께 대비</th></tr>{summary_rows}</table>'
        f"<h3>오늘의 브리핑</h3><div class=\"card\"><ol>{briefing_lines}</ol></div>"
        "<h3>TOP5</h3>"
        '<table><tr><th>순위</th><th>상품명</th><th>분류</th>'
        f'<th class="num">매출</th><th class="num">판매수량</th></tr>{top_rows}</table>'
        "<h3>시간대</h3>"
        '<table><tr><th>시간대</th><th class="num">매출</th>'
        f'<th class="num">손님 수</th></tr>{hour_rows}</table>'
    )
    return _page(
        "③ 일일 보고서",
        f"기준일 {format_display_date(payload['saledate'])} · {payload['dept_nm']}",
        body,
        PAGE_REPORT,
    )


def render_admin_page(
    window: tuple[str, str],
    before: dict[str, int],
    after: dict[str, int],
    elapsed_sec: float,
) -> str:
    """④ 관리자 — 재생성 전/후 수치를 나란히 놓아 멱등을 보존한다.

    Args:
        window: ``(시작일, 종료일)``.
        before: 재생성 전 총계.
        after: 재생성 후 총계.
        elapsed_sec: 재생성 소요 시간(초).

    Returns:
        완결된 HTML 문서.
    """
    start, end = window
    identical = before == after
    verdict = (
        f'<div class="same">{_esc(IDEMPOTENT_MESSAGE)}</div>'
        if identical
        else '<div class="same">값이 달라졌어요 — 확인이 필요합니다</div>'
    )

    rows = "".join(
        f"<tr><td>{_esc(label)}</td>"
        f'<td class="num">{before[key]:,}{_esc(unit)}</td>'
        f'<td class="num">{after[key]:,}{_esc(unit)}</td></tr>'
        for label, key, unit in (("총매출", "sale_amt", "원"), ("거래건수", "deal_cnt", "건"))
    )

    body = (
        "<h2>관리자 — 최근 7일 재생성</h2>"
        '<p class="note">발표 노트북에 터미널이 없어도 멱등을 보여 주기 위한 버튼입니다. '
        "같은 기간을 다시 만들어도 숫자가 그대로라는 것을 화면에서 증명합니다.</p>"
        f'<p class="meta">대상 기간 {format_display_date(start)} ~ {format_display_date(end)} '
        f"· 소요 {elapsed_sec}초</p>"
        '<table><tr><th>항목</th><th class="num">실행 전</th>'
        f'<th class="num">실행 후</th></tr>{rows}</table>'
        f"{verdict}"
        '<p class="note">데이터는 (기본 시드, 점포, 날짜)에서 파생한 시드로 만들어집니다. '
        "어떤 구간을 몇 번 다시 만들어도 같은 값이 나오는 이유입니다.</p>"
    )
    return _page(
        "④ 관리자 — 멱등 시연",
        f"재생성 대상 {format_display_date(start)} ~ {format_display_date(end)}",
        body,
        PAGE_ADMIN,
    )


def render_index_page(saledate: str, store_names: Sequence[str]) -> str:
    """목차 — 발표 중 어느 장으로든 한 번에 넘어가기 위한 첫 화면.

    Args:
        saledate: ``YYYYMMDD``.
        store_names: 점포명 목록.

    Returns:
        완결된 HTML 문서.
    """
    body = (
        "<h2>30초 매장 브리핑 — 오프라인 저장본</h2>"
        '<p class="note">인터넷과 서버 없이 열리는 화면 저장본입니다. '
        "발표장 통신이 끊겼을 때 이 파일들로 시연을 이어 갑니다.</p>"
        "<table>"
        '<tr><th>장</th><th>내용</th></tr>'
        "<tr><td>① 브리핑</td><td>세 점포의 3줄 — 점포마다 다른 문장</td></tr>"
        "<tr><td>② 자세히</td><td>매출·손님 수·1인당 구매액, 시간대, TOP5, 최근 흐름</td></tr>"
        "<tr><td>③ 보고서</td><td>내려받는 일일 보고 xlsx와 같은 내용</td></tr>"
        "<tr><td>④ 관리자</td><td>최근 7일 재생성 전/후 수치 비교 (멱등)</td></tr>"
        "</table>"
        f'<p class="note">대상 점포: {_esc(" · ".join(store_names))}</p>'
    )
    return _page("30초 매장 브리핑 — 스냅샷 목차", f"기준일 {format_display_date(saledate)}", body, PAGE_INDEX)


# --- 조립 ------------------------------------------------------------------


def build_snapshots(
    engine: Engine,
    saledate: str,
    out_dir: Path | str | None = None,
    *,
    run_regeneration: bool = True,
) -> list[Path]:
    """스냅샷 4장 + 목차를 만든다 (명세 15장 보험 2).

    Args:
        engine: 대상 엔진.
        saledate: 기준일 ``YYYYMMDD``.
        out_dir: 저장 디렉토리. None이면 ``data/snapshot``.
        run_regeneration: True면 관리자 스냅샷을 위해 **최근 7일을 실제로 재생성**해
            전/후 수치를 측정한다. 멱등이라 데이터는 변하지 않는다. False면
            재생성 없이 현재 값을 전/후에 그대로 적는다 (측정이 아니라 표시).

    Returns:
        만들어진 파일 경로 목록 (목차가 첫 번째).

    Raises:
        LookupError: 그 날짜의 브리핑이 없을 때.
    """
    import time

    from src.app.main import load_available_dates, load_totals

    payloads = _load_payloads(engine, saledate)
    directory = Path(out_dir) if out_dir is not None else SNAPSHOT_DIR
    directory.mkdir(parents=True, exist_ok=True)

    # ④ 관리자: 실제 재생성을 돌려 전/후를 측정한다 (멱등이라 데이터는 그대로).
    dates = load_available_dates(engine, next(iter(payloads)))
    latest = dates[-1]
    window = (shift_days(latest, 0 - ADMIN_REGEN_DAYS + 1), latest)

    before = load_totals(engine, *window)
    elapsed = 0.0
    if run_regeneration:
        from src.extract.sample import SampleExtractor
        from src.load.pipeline import load_period

        started = time.perf_counter()
        load_period(SampleExtractor(), window[0], window[1], engine=engine)
        elapsed = round(time.perf_counter() - started, 3)
    after = load_totals(engine, *window)

    detail_code = DETAIL_STORE if DETAIL_STORE in payloads else next(iter(payloads))
    report_code = REPORT_STORE if REPORT_STORE in payloads else detail_code
    report_payload, top_items, hourly = fetch_report_data(engine, saledate, report_code)

    documents = {
        PAGE_INDEX: render_index_page(
            saledate, [payload["dept_nm"] for payload in payloads.values()]
        ),
        PAGE_BRIEFING: render_briefing_page(payloads, saledate),
        PAGE_DETAIL: render_detail_page(
            payloads[detail_code], _load_trend(engine, saledate, detail_code)
        ),
        PAGE_REPORT: render_report_page(report_payload, top_items, hourly),
        PAGE_ADMIN: render_admin_page(window, before, after, elapsed),
    }

    written: list[Path] = []
    for name, document in documents.items():
        path = directory / name
        path.write_text(document, encoding="utf-8")
        written.append(path)

    logger.info(
        "스냅샷 %d장 생성 (기준일 %s) → %s%s",
        len(written),
        saledate,
        directory,
        "" if before == after else "  ※ 관리자 전/후 수치가 다릅니다",
    )
    return written


# --- CLI -------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 파싱한다.

    Args:
        argv: 인자 목록. None이면 ``sys.argv``.

    Returns:
        ``date``·``out``·``no_regen`` 을 가진 네임스페이스.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.report.snapshot",
        description="발표용 정적 HTML 스냅샷 생성 (인터넷·서버 없이 열림 — 명세 15장 보험 2)",
    )
    parser.add_argument("--date", required=True, help="기준일 YYYYMMDD")
    parser.add_argument("--out", default=None, help=f"저장 디렉토리 (기본: {SNAPSHOT_DIR})")
    parser.add_argument(
        "--no-regen",
        action="store_true",
        help="관리자 스냅샷용 재생성을 건너뛴다 (전/후 측정 없이 현재 값만 표시)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 진입점.

    Args:
        argv: 인자 목록. None이면 ``sys.argv``.

    Returns:
        종료 코드. 0이면 성공.
    """
    args = parse_args(argv)

    try:
        pages = build_snapshots(
            get_engine(), args.date, args.out, run_regeneration=not args.no_regen
        )
    except LookupError as error:
        logger.error("스냅샷 생성 실패: %s", error)
        return 2

    logger.info("첫 화면을 브라우저로 여세요: %s", pages[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
