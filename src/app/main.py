"""데모 화면 — 명세 9장 (Streamlit 1페이지).

이 계층은 철저히 **소비자**다: 저장된 것을 읽고, 누른 것을 기록한다.
어떤 계산도, 어떤 LLM 호출도, 어떤 외부 네트워크 접근도 여기엔 없다 (불변식 7).

브리핑 3줄은 배치가 새벽에 만들어 ``BRIEFING_DAILY`` 에 넣어 둔 글자를
그대로 출력할 뿐이다 — 3초 표시 목표의 비밀이 여기 있다.

실행:
    streamlit run src/app/main.py
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import Engine, select
from streamlit.errors import StreamlitAPIException

from src.common.config import DB_PATH, DB_URL_ENV, get_engine, is_sqlite
from src.common.dateutil import date_range, format_date, parse_date, shift_days
from src.load import schema

# --- 화면 문구 (전문용어 금지 — 명세 14장) ---------------------------------

PAGE_TITLE = "30초 매장 브리핑"
MOCKUP_BADGE = "🧪 목업 데이터"
EMPTY_STATE = "이 날짜의 브리핑이 아직 없어요. 다른 날짜를 선택해 주세요"
FOOTER_NOTE = "본 화면의 수치는 운영 참고용입니다. 정산·회계 기준이 아닙니다."
REPORT_READY = "오늘의 일일 보고가 준비되어 있어요."
NO_STOCK_RISK = "지금은 부족한 상품이 없어요"
FEEDBACK_TOAST = "반영했어요"

#: 피드백 버튼 (명세 9장) — "괜찮아요"는 거절이 아니라 사양의 어휘다
FEEDBACK_ACTIONS: dict[str, str] = {"확인했어요": "ACCEPT", "괜찮아요": "DECLINE"}

#: 최근 추이 차트가 보여 줄 일수 (명세 9장 "최근 14일 추이")
TREND_DAYS = 14

#: 관리자 재생성 구간 길이 (명세 9장 "최근 7일 재생성")
ADMIN_REGEN_DAYS = 7


# --- 데이터 접근: 저장된 것을 읽기만 한다 -----------------------------------


def database_url() -> str | None:
    """화면이 붙을 DB를 정한다 (ADR-0011).

    우선순위: Streamlit secrets → 환경변수 → 로컬 SQLite.
    클라우드 배포에서는 secrets에 원격 PostgreSQL(Supabase) 연결 문자열을 넣고,
    로컬 개발에서는 아무것도 설정하지 않으면 ``data/pos_mockup.db`` 를 쓴다.

    **연결 문자열은 화면 어디에도 표시하지 않는다.** 사용자에게 키를 요구하지 않는
    원칙(불변식 7)은 그대로다 — 값은 운영자가 배포 설정에 넣고, 사용자는 URL만 연다.

    Returns:
        연결 URL. 설정된 것이 없으면 None (기본 SQLite로 떨어진다).
    """
    try:
        secret = st.secrets.get(DB_URL_ENV)
    except (FileNotFoundError, StreamlitAPIException):
        # secrets.toml 이 없는 로컬 실행 — 정상 경로다.
        secret = None

    return str(secret) if secret else None


def load_stores(engine: Engine) -> pd.DataFrame:
    """점포 목록을 읽는다.

    Args:
        engine: 대상 엔진.

    Returns:
        ``DEPT_CD``·``DEPT_NM``·``SIZE_GRADE`` 프레임.
    """
    with engine.connect() as connection:
        return pd.read_sql(
            select(schema.DIM_STORE).order_by(schema.DIM_STORE.c.DEPT_CD), connection
        )


def load_available_dates(engine: Engine, dept_cd: str) -> list[str]:
    """브리핑이 있는 날짜를 오름차순으로 읽는다.

    Args:
        engine: 대상 엔진.
        dept_cd: 점포코드.

    Returns:
        ``YYYYMMDD`` 리스트.
    """
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(schema.BRIEFING_DAILY.c.SALEDATE)
                .where(schema.BRIEFING_DAILY.c.DEPT_CD == dept_cd)
                .order_by(schema.BRIEFING_DAILY.c.SALEDATE)
            ).scalars()
        )


def load_briefing(engine: Engine, saledate: str, dept_cd: str) -> dict[str, Any] | None:
    """저장된 브리핑을 읽는다 — JSON 1건 읽기가 전부다 (명세 9장 성능).

    Args:
        engine: 대상 엔진.
        saledate: ``YYYYMMDD``.
        dept_cd: 점포코드.

    Returns:
        계산 JSON. 없으면 None (빈 상태 안내를 위해 예외를 던지지 않는다).
    """
    with engine.connect() as connection:
        raw = connection.execute(
            select(schema.BRIEFING_DAILY.c.PAYLOAD_JSON).where(
                schema.BRIEFING_DAILY.c.SALEDATE == saledate,
                schema.BRIEFING_DAILY.c.DEPT_CD == dept_cd,
            )
        ).scalar_one_or_none()

    return json.loads(raw) if raw is not None else None


def load_trend(engine: Engine, saledate: str, dept_cd: str, days: int = TREND_DAYS) -> pd.DataFrame:
    """최근 N일 매출 추이를 읽는다 (자세히 보기를 펼칠 때만 호출).

    Args:
        engine: 대상 엔진.
        saledate: 기준일 ``YYYYMMDD``.
        dept_cd: 점포코드.
        days: 거슬러 볼 일수 (기준일 포함).

    Returns:
        ``SALEDATE``·``SALE_AMT`` 프레임 (날짜 오름차순).
    """
    start = shift_days(saledate, 0 - days + 1)
    table = schema.MART_DAY_STORE

    with engine.connect() as connection:
        return pd.read_sql(
            select(table.c.SALEDATE, table.c.SALE_AMT)
            .where(
                table.c.DEPT_CD == dept_cd,
                table.c.SALEDATE.between(start, saledate),
            )
            .order_by(table.c.SALEDATE),
            connection,
        )


def load_totals(engine: Engine, from_date: str, to_date: str) -> dict[str, int]:
    """기간의 총매출·거래건수를 읽는다 (관리자 재생성 전후 비교용).

    Args:
        engine: 대상 엔진.
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.

    Returns:
        ``sale_amt``·``deal_cnt`` 딕셔너리.
    """
    from sqlalchemy import func

    table = schema.MART_DAY_STORE
    with engine.connect() as connection:
        row = connection.execute(
            select(
                func.coalesce(func.sum(table.c.SALE_AMT), 0),
                func.coalesce(func.sum(table.c.DEAL_CNT), 0),
            ).where(table.c.SALEDATE.between(from_date, to_date))
        ).one()

    return {"sale_amt": int(row[0]), "deal_cnt": int(row[1])}


def record_feedback(
    engine: Engine, saledate: str, dept_cd: str, card_id: str, action: str
) -> None:
    """피드백을 기록한다 (명세 9장 ``FEEDBACK_LOG``).

    채택률과 문구 개선의 원료가 된다. 같은 사람이 여러 번 눌러도 전부 남긴다.

    Args:
        engine: 대상 엔진.
        saledate: ``YYYYMMDD``.
        dept_cd: 점포코드.
        card_id: 대상 카드 ID.
        action: ``ACCEPT`` 또는 ``DECLINE``.
    """
    with engine.begin() as connection:
        connection.execute(
            schema.FEEDBACK_LOG.insert(),
            {
                "TS": datetime.now().isoformat(timespec="seconds"),
                "SALEDATE": saledate,
                "DEPT_CD": dept_cd,
                "CARD_ID": card_id,
                "ACTION": action,
            },
        )


def format_display_date(saledate: str) -> str:
    """화면에 보여 줄 날짜 문자열을 만든다.

    Args:
        saledate: ``YYYYMMDD``.

    Returns:
        ``YYYY-MM-DD`` 형식.
    """
    return parse_date(saledate).strftime("%Y-%m-%d")


def arrow_text(diff_pct: float | None) -> str:
    """전일 대비 화살표 문구를 만든다 (명세 9장 자세히 보기).

    이모지·부호·문장 셋으로 같은 뜻을 겹쳐 보여 준다 (명세 9장 스타일).

    Args:
        diff_pct: 증감률. 없으면 None.

    Returns:
        화살표가 붙은 문구. 비교 대상이 없으면 안내 문구.
    """
    if diff_pct is None:
        return "비교할 날이 없어요"
    if diff_pct > 0:
        return f"🔺 {diff_pct}% 늘었어요"
    if diff_pct < 0:
        return f"🔻 {diff_pct}% 줄었어요"
    return "➖ 그대로예요"


# --- 화면 조립 -------------------------------------------------------------


def _sidebar(engine: Engine, stores: pd.DataFrame) -> tuple[str, str, str]:
    """사이드바를 그리고 선택값을 돌려준다.

    Args:
        engine: 대상 엔진.
        stores: 점포 프레임.

    Returns:
        ``(점포코드, 점포명, 기준일)``.
    """
    st.sidebar.header("매장 선택")

    names = dict(zip(stores["DEPT_CD"], stores["DEPT_NM"], strict=True))
    dept_cd = st.sidebar.selectbox(
        "점포", options=list(names), format_func=lambda code: names[code]
    )

    dates = load_available_dates(engine, dept_cd)
    if not dates:
        return dept_cd, names[dept_cd], ""

    # 기본값은 데이터 최신일 (명세 9장)
    chosen: date = st.sidebar.date_input(
        "기준일",
        value=parse_date(dates[-1]),
        min_value=parse_date(dates[0]),
        max_value=parse_date(dates[-1]),
        format="YYYY-MM-DD",
    )
    return dept_cd, names[dept_cd], format_date(chosen)


def _briefing_section(engine: Engine, payload: dict[str, Any]) -> None:
    """브리핑 3줄과 피드백 버튼을 그린다 — 저장된 글자를 그대로 출력한다.

    Args:
        engine: 대상 엔진.
        payload: 계산 JSON.
    """
    st.markdown("## 오늘의 브리핑")

    with st.container(border=True):
        for line in payload["briefing_lines"]:
            st.markdown(f"### {line}")

    primary_card = payload["cards"][0]["card_id"]
    columns = st.columns(len(FEEDBACK_ACTIONS))

    for column, (label, action) in zip(columns, FEEDBACK_ACTIONS.items(), strict=True):
        if column.button(label, width="stretch", key=f"feedback_{action}"):
            record_feedback(
                engine, payload["saledate"], payload["dept_cd"], primary_card, action
            )
            st.toast(FEEDBACK_TOAST)


def _report_section(engine: Engine, payload: dict[str, Any]) -> None:
    """일일 보고 내려받기를 그린다.

    Args:
        engine: 대상 엔진.
        payload: 계산 JSON.
    """
    from src.report import daily_report

    st.markdown("## 📄 일일 보고")
    st.write(REPORT_READY)

    st.download_button(
        "보고서 내려받기(.xlsx)",
        data=daily_report.report_bytes(engine, payload["saledate"], payload["dept_cd"]),
        file_name=daily_report.report_filename(payload["dept_nm"], payload["saledate"]),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )


def _detail_section(engine: Engine, payload: dict[str, Any]) -> None:
    """자세히 보기(기본 접힘)를 그린다 — 펼칠 때만 마트를 조회한다.

    Args:
        engine: 대상 엔진.
        payload: 계산 JSON.
    """
    with st.expander("▼ 자세히 보기", expanded=False):
        left, middle, right = st.columns(3)
        left.metric("어제 매출", f"{payload['sale_amt']:,}원", arrow_text(payload["prev_diff_pct"]))
        middle.metric("손님 수", f"{payload['deal_cnt']:,}명", arrow_text(payload["cnt_diff_pct"]))
        right.metric(
            "1인당 구매액", f"{payload['avg_ticket']:,}원", arrow_text(payload["ticket_diff_pct"])
        )

        st.markdown("#### 시간대별 매출")
        hourly = pd.DataFrame(payload["hourly"])
        st.bar_chart(hourly.set_index("hour")["sale_amt"], height=220)

        st.markdown("#### 많이 팔린 상품")
        if payload["top5"]:
            top5 = pd.DataFrame(payload["top5"]).rename(
                columns={"goods_nm": "상품", "sale_amt": "매출(원)", "qty": "판매수량"}
            )
            st.dataframe(top5, hide_index=True, width="stretch")
        else:
            st.write("이 날짜에는 판매 기록이 없어요")

        st.markdown("#### 곧 떨어질 수 있는 상품")
        risk_items = payload.get("stock_risk", {}).get("items", [])
        if risk_items:
            st.dataframe(
                pd.DataFrame(risk_items)[["goods_nm", "stock_qty", "sale_average_qty"]].rename(
                    columns={
                        "goods_nm": "상품",
                        "stock_qty": "남은 재고",
                        "sale_average_qty": "하루 평균 판매",
                    }
                ),
                hide_index=True,
                width="stretch",
            )
        else:
            st.write(NO_STOCK_RISK)

        st.markdown(f"#### 최근 {TREND_DAYS}일 흐름")
        trend = load_trend(engine, payload["saledate"], payload["dept_cd"])
        if not trend.empty:
            st.line_chart(trend.set_index("SALEDATE")["SALE_AMT"], height=220)


def _admin_section(engine: Engine, dept_cd: str) -> None:
    """관리자 메뉴 — 최근 7일 재생성으로 멱등을 화면에서 증명한다 (명세 9장).

    발표 노트북에 터미널이 없어도 "몇 번을 다시 만들어도 같은 결과"를 보여 줄 수 있다.

    Args:
        engine: 대상 엔진.
        dept_cd: 현재 선택된 점포코드 (표시용).
    """
    from src.extract.sample import SampleExtractor
    from src.load.pipeline import load_period

    with st.sidebar.expander("⚙ 관리자"):
        dates = load_available_dates(engine, dept_cd)
        if not dates:
            st.write("재생성할 데이터가 없어요")
            return

        latest = dates[-1]
        start = shift_days(latest, 0 - ADMIN_REGEN_DAYS + 1)
        st.caption(f"{format_display_date(start)} ~ {format_display_date(latest)}")

        if st.button(f"최근 {ADMIN_REGEN_DAYS}일 재생성", width="stretch"):
            before = load_totals(engine, start, latest)

            with st.spinner("다시 만드는 중…"):
                result = load_period(SampleExtractor(), start, latest, engine=engine)

            after = load_totals(engine, start, latest)

            st.write(f"소요 {result.elapsed_sec}초")
            comparison = pd.DataFrame(
                {
                    "실행 전": [f"{before['sale_amt']:,}원", f"{before['deal_cnt']:,}건"],
                    "실행 후": [f"{after['sale_amt']:,}원", f"{after['deal_cnt']:,}건"],
                },
                index=["총매출", "거래건수"],
            )
            st.table(comparison)

            if before == after:
                st.success("몇 번을 다시 만들어도 같은 결과예요")
            else:
                st.error("값이 달라졌어요 — 확인이 필요합니다")


def main() -> None:
    """화면 전체를 그린다."""
    st.set_page_config(page_title=PAGE_TITLE, page_icon="🏪", layout="centered")

    engine = get_engine(database_url())
    stores = load_stores(engine)

    if stores.empty:
        st.warning(
            "데이터가 아직 없어요. 터미널에서 아래 명령을 먼저 실행해 주세요.\n\n"
            "`python -m src.load.pipeline --from 20250701 --to 20260731`"
        )
        return

    dept_cd, dept_nm, saledate = _sidebar(engine, stores)
    _admin_section(engine, dept_cd)

    if not saledate:
        st.info(EMPTY_STATE)
        return

    st.markdown(
        f"### 🏪 {dept_nm} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"데이터 기준일: {format_display_date(saledate)} &nbsp;&nbsp;|&nbsp;&nbsp; {MOCKUP_BADGE}"
    )
    st.divider()

    payload = load_briefing(engine, saledate, dept_cd)
    if payload is None:
        st.info(EMPTY_STATE)
        return

    _briefing_section(engine, payload)
    st.divider()
    _report_section(engine, payload)
    _detail_section(engine, payload)

    st.divider()
    st.caption(FOOTER_NOTE)
    st.caption(f"데이터: {DB_PATH.name if is_sqlite(engine) else '클라우드 데이터베이스'}")


if __name__ == "__main__":
    main()
