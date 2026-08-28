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
from sqlalchemy.exc import SQLAlchemyError
from streamlit.errors import StreamlitAPIException

from src.common.config import DB_PATH, DB_URL_ENV, get_engine, is_sqlite
from src.common.dateutil import date_range, dow_name, format_date, parse_date, shift_days
from src.load import schema

# --- 화면 문구 (전문용어 금지 — 명세 14장) ---------------------------------

PAGE_TITLE = "30초 매장 브리핑"
MOCKUP_BADGE = "🧪 목업 데이터"
EMPTY_STATE = "이 날짜의 브리핑이 아직 없어요. 다른 날짜를 선택해 주세요"
FOOTER_NOTE = "본 화면의 수치는 운영 참고용입니다. 정산·회계 기준이 아닙니다."
REPORT_READY = "오늘의 일일 보고가 준비되어 있어요."
NO_STOCK_RISK = "지금은 부족한 상품이 없어요"
FEEDBACK_TOAST = "반영했어요"

#: 보기 전환 (부록 B.7). 기본값은 기존 점포장 화면이다.
#: 멀티페이지를 쓰지 않는다 — 발표 중 페이지 이동은 흐름이 끊긴다.
VIEW_MODES: tuple[str, str] = ("점포장 화면", "여러 매장 보기")

#: 상태별 표시 기호 (부록 B.6). 색만으로 구분하지 않는다 — 기호와 글자를 겹쳐 둔다.
GROUP_STATUS_MARKS: dict[str, str] = {"STOCK": "🔴", "PEAK": "🟡", "CALM": "⚪"}

#: 위젯 상태 키 (부록 B.7).
VIEW_MODE_KEY = "view_mode"
STORE_PICK_KEY = "store_pick"

#: 대기 키 — 관리자 화면의 [자세히]가 여기에 적어 두고, 다음 실행 맨 앞에서 옮긴다.
#: streamlit은 위젯이 만들어진 **뒤에** 그 키를 바꾸는 것을 막기 때문이다.
PENDING_KEYS: dict[str, str] = {
    "pending_store": STORE_PICK_KEY,
    "pending_view": VIEW_MODE_KEY,
}

GROUP_TOTAL_LABEL = "합계"
GROUP_EMPTY_STATE = "이 날짜의 요약이 아직 없어요. 다른 날짜를 선택해 주세요"

#: 직전 같은 기간이 없을 때의 표시 (부록 B.10) — 없는 비교를 지어내지 않는다.
PERIOD_NO_BASELINE = "비교할 지난 기간이 없어요"

#: 요일 기준선이 없을 때의 표시 (명세 7.4 폴백과 같은 태도).
DOW_NO_BASELINE = "평소와 견줄 자료가 아직 부족해요"

#: 피드백 버튼 (명세 9장) — "괜찮아요"는 거절이 아니라 사양의 어휘다
FEEDBACK_ACTIONS: dict[str, str] = {"확인했어요": "ACCEPT", "괜찮아요": "DECLINE"}

#: 최근 추이 차트가 보여 줄 일수 (명세 9장 "최근 14일 추이")
TREND_DAYS = 14

#: 관리자 재생성 구간 길이 (명세 9장 "최근 7일 재생성")
ADMIN_REGEN_DAYS = 7

#: 보고서의 매장별 상품 표시 수 (부록 B.10).
GROUP_TOP_ITEMS = 3

#: 보고서 기간 집계 기본 일수 (부록 B.10).
PERIOD_DAYS = 7

#: 기간 합계 표에 담을 매장 수 (부록 B.13 결정 2·4).
PERIOD_STORE_ROWS = 20

#: 그래프에 그릴 매장 계열 수 상한 (부록 B.13 결정 3).
#: 실제 환경은 매장 1,300개다 — 선 1,300개는 그림이 아니다.
#: 매장이 이보다 적으면 전부 그린다 (데모 3개는 그대로).
GROUP_CHART_SERIES = 5

#: 재고 반복을 셀 구간과 보여 줄 매장 수 (부록 B.13 결정 5).
SIGNAL_STREAK_DAYS = 30
SIGNAL_STREAK_ROWS = 10

#: 요일 패턴을 낼 기간 (주). 요일당 표본이 이 수만큼 쌓인다.
DOW_PATTERN_WEEKS = 12

#: 전체 합계 계열의 이름 (매장이 많을 때 대표선으로 쓴다).
TOTAL_SERIES_NAME = "전체 합계"

#: 표 아래 안내 문구.
SIGNAL_STREAK_NOTE = "며칠씩 이어지면 그날의 일이 아니라 준비 방식을 살펴볼 때입니다"
MONTHLY_NOTE = "달마다 날수가 달라 합계 대신 하루 평균으로 그렸습니다"
PERIOD_TRUNCATED_NOTE = " · 매출 상위 매장만 보여 줍니다"

#: 요일 이름 (표시 순서 고정 — 매장 수와 무관하게 7행).
DOW_ORDER: tuple[str, ...] = (
    "월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일",
)


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


def connection_help(url: str, error_text: str) -> str:
    """연결이 안 될 때 **운영자**에게 보여 줄 안내를 만든다 (명세 15장).

    이 화면은 점포장이 아니라 배포한 사람을 위한 것이다. 트레이스백만 뜨면
    무엇을 고쳐야 하는지 알 수 없어 발표 직전에 손을 쓸 수 없다.

    연결 문자열은 **가려서** 넣는다 — 비밀번호가 화면에 뜨면 안 된다 (ADR-0011).

    Args:
        url: 붙으려던 연결 URL.
        error_text: 드라이버가 준 오류 문구.

    Returns:
        고칠 방법이 담긴 안내문 (마크다운).
    """
    from src.load.publish import diagnose, mask_url

    return (
        f"데이터에 연결하지 못했어요.\n\n"
        f"- 연결 대상: `{mask_url(url)}`\n"
        f"- 확인할 점: {diagnose(error_text)}\n\n"
        f"배포 설정(Secrets)의 `{DB_URL_ENV}` 값을 확인해 주세요."
    )


def missing_connection_help() -> str:
    """연결 설정 자체가 없을 때의 안내를 만든다 (명세 15장).

    Returns:
        무엇을 넣어야 하는지 알려 주는 안내문 (마크다운).
    """
    return (
        f"배포 설정에 연결 정보가 없어 이 서버의 빈 파일을 보고 있어요.\n\n"
        f"Streamlit Cloud 앱의 **Settings → Secrets** 에 아래 한 줄을 넣어 주세요.\n\n"
        f"```toml\n{DB_URL_ENV} = \"postgresql://...\"\n```"
    )


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


def load_group_briefing(engine: Engine, saledate: str) -> dict[str, Any] | None:
    """저장된 여러 매장 요약을 읽는다 (부록 B.7) — JSON 1건 읽기가 전부다.

    Args:
        engine: 대상 엔진.
        saledate: ``YYYYMMDD``.

    Returns:
        그룹 요약 JSON. 없으면 None.
    """
    with engine.connect() as connection:
        raw = connection.execute(
            select(schema.BRIEFING_DAILY_GROUP.c.PAYLOAD_JSON).where(
                schema.BRIEFING_DAILY_GROUP.c.SALEDATE == saledate
            )
        ).scalar_one_or_none()

    return json.loads(raw) if raw is not None else None


def _store_names(engine: Engine) -> dict[str, str]:
    """점포코드 → 점포명 (그래프 범례에 코드가 아니라 이름이 뜨게 한다).

    Args:
        engine: 대상 엔진.

    Returns:
        점포코드를 키로 하는 이름 사전 (코드 오름차순).
    """
    stores = load_stores(engine)
    return dict(zip(stores["DEPT_CD"], stores["DEPT_NM"], strict=True))


def load_group_trend(
    engine: Engine, saledate: str, days: int = TREND_DAYS
) -> pd.DataFrame:
    """최근 N일 매장별 매출 흐름을 읽는다 (부록 B.10).

    집계는 DB가, 모양 바꾸기는 여기서 끝낸다 — 화면은 그리기만 한다.

    Args:
        engine: 대상 엔진.
        saledate: 기준일 ``YYYYMMDD``.
        days: 거슬러 볼 일수 (기준일 포함).

    Returns:
        날짜를 인덱스로, 매장명을 열로 갖는 프레임.
    """
    table = schema.MART_DAY_STORE
    start = shift_days(saledate, 0 - days + 1)

    with engine.connect() as connection:
        frame = pd.read_sql(
            select(table.c.SALEDATE, table.c.DEPT_CD, table.c.SALE_AMT)
            .where(table.c.SALEDATE.between(start, saledate))
            .order_by(table.c.SALEDATE, table.c.DEPT_CD),
            connection,
        )

    names = _store_names(engine)
    wide = frame.pivot(index="SALEDATE", columns="DEPT_CD", values="SALE_AMT")

    # 계열 수를 묶는다 (부록 B.13 결정 3) — 매장이 많으면 상위 N개 + 전체 합계선.
    if len(wide.columns) > GROUP_CHART_SERIES:
        leaders = wide.sum().nlargest(GROUP_CHART_SERIES).index
        wide = wide[list(leaders)].assign(**{TOTAL_SERIES_NAME: frame.groupby("SALEDATE")["SALE_AMT"].sum()})
        wide = wide.rename(columns=names)
    else:
        wide = wide.reindex(columns=list(names)).rename(columns=names)

    wide.index = [format_display_date(value) for value in wide.index]
    wide.index.name = "날짜"
    return wide


def load_group_hourly(engine: Engine, saledate: str) -> pd.DataFrame:
    """기준일의 매장별 시간대 매출을 읽는다 (부록 B.10).

    Args:
        engine: 대상 엔진.
        saledate: ``YYYYMMDD``.

    Returns:
        시간을 인덱스로, 매장명을 열로 갖는 프레임.
    """
    table = schema.MART_HOUR_STORE

    with engine.connect() as connection:
        frame = pd.read_sql(
            select(table.c.HOUR, table.c.DEPT_CD, table.c.SALE_AMT)
            .where(table.c.SALEDATE == saledate)
            .order_by(table.c.HOUR, table.c.DEPT_CD),
            connection,
        )

    names = _store_names(engine)
    wide = frame.pivot(index="HOUR", columns="DEPT_CD", values="SALE_AMT")

    # 매장이 많으면 겹쳐 볼 수 없다 — 전체 합계 하나로 보여 준다 (부록 B.13 결정 3).
    if len(wide.columns) > GROUP_CHART_SERIES:
        wide = frame.groupby("HOUR")["SALE_AMT"].sum().to_frame(TOTAL_SERIES_NAME)
    else:
        wide = wide.reindex(columns=list(names)).rename(columns=names)

    wide = wide.fillna(0)
    wide.index = [f"{hour}시" for hour in wide.index]
    wide.index.name = "시간"
    return wide


def load_group_top_items(
    engine: Engine, saledate: str, limit: int = GROUP_TOP_ITEMS
) -> pd.DataFrame:
    """기준일의 매장별 많이 팔린 상품을 읽는다 (부록 B.10).

    Args:
        engine: 대상 엔진.
        saledate: ``YYYYMMDD``.
        limit: 매장마다 보여 줄 상품 수.

    Returns:
        ``매장``·``상품``·``매출``·``수량`` 열을 갖는 프레임.
    """
    table = schema.MART_DAY_STORE_ITEM

    with engine.connect() as connection:
        frame = pd.read_sql(
            select(table.c.DEPT_CD, table.c.GOODS_NM, table.c.SALE_AMT, table.c.QTY)
            .where(table.c.SALEDATE == saledate)
            .order_by(table.c.DEPT_CD, table.c.SALE_AMT.desc()),
            connection,
        )

    names = _store_names(engine)
    ranked = frame.groupby("DEPT_CD", sort=False).head(limit)
    return pd.DataFrame(
        {
            "매장": ranked["DEPT_CD"].map(names),
            "상품": ranked["GOODS_NM"],
            "매출": ranked["SALE_AMT"],
            "수량": ranked["QTY"],
        }
    ).reset_index(drop=True)


def load_period_summary(
    engine: Engine,
    saledate: str,
    days: int = PERIOD_DAYS,
    limit: int = PERIOD_STORE_ROWS,
) -> dict[str, Any]:
    """최근 N일 매장별 누적과 직전 같은 기간 대비를 읽는다 (부록 B.10).

    **집계는 DB가 한다.** 화면은 결과를 표시만 한다 (부록 B.2의 경계).
    직전 기간에 데이터가 없으면 대비를 ``None`` 으로 둔다 — 없는 비교를 지어내지 않는다.

    Args:
        engine: 대상 엔진.
        saledate: 기준일 ``YYYYMMDD``.
        days: 집계 일수 (기준일 포함).
        limit: 목록에 담을 매장 수 (매출 많은 순). 합계는 **전 매장** 기준이다.

    Returns:
        ``days``·``from_date``·``sale_amt``·``deal_cnt``·``prev_sale_amt``·
        ``prev_diff_pct``·``stores``·``stores_truncated`` 를 담은 딕셔너리.
    """
    from sqlalchemy import func

    table = schema.MART_DAY_STORE
    start = shift_days(saledate, 0 - days + 1)
    prev_end = shift_days(start, -1)
    prev_start = shift_days(prev_end, 0 - days + 1)
    names = _store_names(engine)

    def totals(from_date: str, to_date: str) -> tuple[int, int, int]:
        """구간의 매출·손님·일수를 센다.

        Args:
            from_date: 시작일 ``YYYYMMDD``.
            to_date: 종료일 ``YYYYMMDD``.

        Returns:
            ``(매출, 손님 수, 행수)``.
        """
        with engine.connect() as connection:
            row = connection.execute(
                select(
                    func.coalesce(func.sum(table.c.SALE_AMT), 0),
                    func.coalesce(func.sum(table.c.DEAL_CNT), 0),
                    func.count(table.c.SALEDATE),
                ).where(table.c.SALEDATE.between(from_date, to_date))
            ).one()
        return int(row[0]), int(row[1]), int(row[2])

    sale_amt, deal_cnt, _ = totals(start, saledate)
    prev_sale_amt, _, prev_rows = totals(prev_start, prev_end)

    with engine.connect() as connection:
        by_store = connection.execute(
            select(
                table.c.DEPT_CD,
                func.coalesce(func.sum(table.c.SALE_AMT), 0),
                func.coalesce(func.sum(table.c.DEAL_CNT), 0),
            )
            .where(table.c.SALEDATE.between(start, saledate))
            .group_by(table.c.DEPT_CD)
            .order_by(func.sum(table.c.SALE_AMT).desc())
            # 자르기는 DB가 한다 (부록 B.13 결정 4) — 1,300행을 받아 head() 하지 않는다.
            .limit(limit + 1)
        ).all()

    return {
        "days": days,
        "from_date": start,
        "to_date": saledate,
        "sale_amt": sale_amt,
        "deal_cnt": deal_cnt,
        "prev_sale_amt": prev_sale_amt,
        # 직전 기간이 통째로 비어 있으면 비교 대상이 없다 (명세 7.4의 폴백과 같은 태도).
        "prev_diff_pct": (
            None if prev_rows == 0 or prev_sale_amt == 0
            else round((sale_amt - prev_sale_amt) / prev_sale_amt * 100, 1)
        ),
        "stores": [
            {
                "dept_cd": code,
                "dept_nm": names.get(code, code),
                "sale_amt": int(amount),
                "deal_cnt": int(count),
            }
            for code, amount, count in by_store[:limit]
        ],
        # 한 행 더 받아 왔으면 뒤에 더 있다는 뜻이다 (개수를 다시 세지 않는다).
        "stores_truncated": len(by_store) > limit,
    }


def load_signal_streak(
    engine: Engine,
    saledate: str,
    days: int = SIGNAL_STREAK_DAYS,
    limit: int = SIGNAL_STREAK_ROWS,
) -> pd.DataFrame:
    """최근 N일 동안 매장별로 어떤 신호가 며칠 있었는지 센다 (부록 B.13 결정 5).

    하루짜리 "오늘 재고 부족"은 그날의 사건이지만, 30일 중 13일이면
    **발주 기준 자체의 문제**다. 관리자가 위에 보고할 때 필요한 것은 후자다.

    집계와 자르기를 **DB가** 한다 — 매장이 1,300개여도 결과는 ``limit`` 행이다.

    Args:
        engine: 대상 엔진.
        saledate: 기준일 ``YYYYMMDD``.
        days: 거슬러 셀 일수 (기준일 포함).
        limit: 보여 줄 매장 수 (재고 부족이 많은 순).

    Returns:
        ``매장``·``재고 부족``·``시간대 쏠림``·``조용한 날`` 열을 갖는 프레임.
    """
    from sqlalchemy import case, func

    table = schema.MART_DAY_STORE_SIGNAL
    start = shift_days(saledate, 0 - days + 1)

    def days_with(status: str) -> object:
        """해당 상태였던 날 수를 세는 식을 만든다.

        Args:
            status: ``STOCK``·``PEAK``·``CALM``.

        Returns:
            SQL 집계 식.
        """
        return func.sum(case((table.c.STATUS == status, 1), else_=0))

    stock_days = days_with("STOCK")
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                table.c.DEPT_CD,
                stock_days,
                days_with("PEAK"),
                days_with("CALM"),
            )
            .where(table.c.SALEDATE.between(start, saledate))
            .group_by(table.c.DEPT_CD)
            .order_by(stock_days.desc(), table.c.DEPT_CD)
            .limit(limit)
        ).all()

    names = _store_names(engine)
    return pd.DataFrame(
        {
            "매장": [names.get(row[0], row[0]) for row in rows],
            "재고 부족": [int(row[1]) for row in rows],
            "시간대 쏠림": [int(row[2]) for row in rows],
            "조용한 날": [int(row[3]) for row in rows],
        }
    )


def load_dow_pattern(
    engine: Engine, saledate: str, weeks: int = DOW_PATTERN_WEEKS
) -> pd.DataFrame:
    """요일별 하루 평균 매출을 낸다 (부록 B.13 결정 5).

    매장 수와 무관하게 **항상 7행**이다. 매장이 적으면 매장별 열을,
    많으면 전체 합계 한 열을 준다 (결정 3).

    Args:
        engine: 대상 엔진.
        saledate: 기준일 ``YYYYMMDD``.
        weeks: 거슬러 볼 주 수.

    Returns:
        요일을 인덱스로 갖는 프레임.
    """
    table = schema.MART_DAY_STORE
    start = shift_days(saledate, 0 - weeks * 7 + 1)

    with engine.connect() as connection:
        frame = pd.read_sql(
            select(table.c.SALEDATE, table.c.DEPT_CD, table.c.SALE_AMT)
            .where(table.c.SALEDATE.between(start, saledate))
            .order_by(table.c.SALEDATE),
            connection,
        )

    frame["요일"] = frame["SALEDATE"].map(dow_name)
    names = _store_names(engine)

    if len(names) > GROUP_CHART_SERIES:
        wide = (
            frame.groupby(["SALEDATE", "요일"])["SALE_AMT"]
            .sum()
            .reset_index()
            .groupby("요일")["SALE_AMT"]
            .mean()
            .to_frame(TOTAL_SERIES_NAME)
        )
    else:
        wide = frame.pivot_table(
            index="요일", columns="DEPT_CD", values="SALE_AMT", aggfunc="mean"
        ).rename(columns=names)

    wide = wide.reindex(list(DOW_ORDER)).round(0).fillna(0).astype(int)
    wide.index.name = "요일"
    return wide


def load_monthly_trend(engine: Engine) -> pd.DataFrame:
    """월별 **하루 평균** 매출을 낸다 (부록 B.13 결정 5).

    합계로 보면 2월(28일)이 실제보다 낮게 보인다. 일수가 만든 착시를
    지표로 착각하게 두지 않는다.

    Args:
        engine: 대상 엔진.

    Returns:
        ``YYYY-MM`` 을 인덱스로, ``하루 평균 매출`` 열을 갖는 프레임.
    """
    from sqlalchemy import func

    table = schema.MART_DAY_STORE
    month = func.substr(table.c.SALEDATE, 1, 6)

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                month,
                func.sum(table.c.SALE_AMT),
                func.count(func.distinct(table.c.SALEDATE)),
            )
            .group_by(month)
            .order_by(month)
        ).all()

    return pd.DataFrame(
        {"하루 평균 매출": [round(int(total) / int(days)) for _, total, days in rows]},
        index=pd.Index([f"{key[:4]}-{key[4:]}" for key, _, _ in rows], name="월"),
    )


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
        "점포",
        options=list(names),
        format_func=lambda code: names[code],
        key=STORE_PICK_KEY,
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


def apply_pending_selection() -> None:
    """관리자 화면에서 고른 매장을 위젯 상태에 반영한다 (부록 B.7).

    streamlit은 위젯이 만들어진 **뒤에** 그 키를 바꾸는 것을 막는다
    (``StreamlitAPIException``). 그래서 버튼은 대기 키에만 적어 두고,
    다음 실행에서 **위젯을 만들기 전인** 여기서 옮긴다.
    """
    for pending_key, widget_key in PENDING_KEYS.items():
        if pending_key in st.session_state:
            st.session_state[widget_key] = st.session_state.pop(pending_key)


def _dow_note(row: dict[str, Any]) -> str:
    """평소 같은 요일과 견준 문구를 만든다 (부록 B.10).

    값은 배치가 만든 것을 쓰고, 여기서는 **고르기만** 한다 (산술 없음).

    Args:
        row: 그룹 요약의 매장 행.

    Returns:
        화면에 그대로 쓰는 문구.
    """
    if not row["dow_baseline_available"]:
        return DOW_NO_BASELINE
    return f"평소 같은 요일 대비 {row['dow_diff_pct']}%"


def _group_section(engine: Engine, saledate: str) -> None:
    """여러 매장을 한 장의 보고서로 보여 준다 (부록 B.7·B.10).

    중간 관리자가 팀장에게 올릴 수 있는 형태다 — 요약 → 매장별 → 흐름 →
    시간대 → 상품 → 기간 순으로 위에서 아래로 읽힌다.

    **이 함수는 숫자를 하나도 만들지 않는다.** 합계·비중은 배치가, 기간 집계는
    DB가 만든 것을 읽어 표시만 한다 (부록 B.2·B.10).
    정적 검사 테스트가 산술 연산을 막는다.

    Args:
        engine: 대상 엔진.
        saledate: 기준일 ``YYYYMMDD``.
    """
    payload = load_group_briefing(engine, saledate)
    if payload is None:
        st.info(GROUP_EMPTY_STATE)
        return

    period = load_period_summary(engine, saledate)

    st.markdown(
        f"### 📋 여러 매장 보고 &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"{format_display_date(payload['saledate'])} &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"{payload['store_count']}개 매장 &nbsp;&nbsp;|&nbsp;&nbsp; {MOCKUP_BADGE}"
    )
    st.markdown(f"#### {payload['attention_line']}")

    # 1. 총평 — 기준일 합계와 지난주 같은 기간 대비
    summary_cols = st.columns(4)
    summary_cols[0].metric("어제 매출", f"{payload['total_sale_amt']:,}원")
    summary_cols[1].metric("손님", f"{payload['total_deal_cnt']:,}명")
    summary_cols[2].metric("1인당", f"{payload['group_avg_ticket']:,}원")
    summary_cols[3].metric(
        f"최근 {period['days']}일 매출",
        f"{period['sale_amt']:,}원",
        delta=(
            PERIOD_NO_BASELINE
            if period["prev_diff_pct"] is None
            else f"{period['prev_diff_pct']}% (지난 {period['days']}일 대비)"
        ),
    )
    st.divider()

    # 2. 매장별 요약
    st.markdown("#### 매장별")
    for row in payload["stores"]:
        detail, action = st.columns([5, 1], vertical_alignment="center")
        with detail:
            st.markdown(
                f"**{row['dept_nm']}** &nbsp;&nbsp; {row['sale_amt']:,}원 "
                f"&nbsp;·&nbsp; 손님 {row['deal_cnt']:,}명 "
                f"&nbsp;·&nbsp; 1인당 {row['avg_ticket']:,}원 "
                f"&nbsp;·&nbsp; 비중 {row['share_pct']}%"
            )
            st.markdown(
                f"{GROUP_STATUS_MARKS[row['status']]} &nbsp;{row['status_text']}"
                f" &nbsp;&nbsp;·&nbsp;&nbsp; {_dow_note(row)}"
            )
        with action:
            if st.button("자세히", key=f"goto_{row['dept_cd']}", width="stretch"):
                # 관리자가 "어느 매장부터"를 정한 뒤 바로 그 매장을 볼 수 있어야 한다.
                # 위젯 키를 여기서 직접 바꾸면 streamlit이 막는다 — 대기 키에 적어 둔다.
                st.session_state["pending_store"] = row["dept_cd"]
                st.session_state["pending_view"] = VIEW_MODES[0]
                st.rerun()

    if payload["stores_truncated"]:
        st.caption(
            f"매출 상위 {payload['stores_shown']}개만 보여 줍니다 "
            f"(전체 {payload['store_count']:,}개)"
        )

    st.markdown(
        f"**{GROUP_TOTAL_LABEL}** &nbsp;&nbsp; {payload['total_sale_amt']:,}원 "
        f"&nbsp;·&nbsp; 손님 {payload['total_deal_cnt']:,}명 "
        f"&nbsp;·&nbsp; 1인당 {payload['group_avg_ticket']:,}원"
    )

    # 매장이 많으면 목록보다 분포가 더 많은 것을 말한다 (부록 B.13 결정 2).
    if payload["stores_truncated"]:
        quartiles = payload["sale_amt_quartiles"]
        counts = payload["status_counts"]
        st.markdown(
            f"**매장 분포** &nbsp;&nbsp; 가운데 {quartiles['median']:,}원 "
            f"&nbsp;·&nbsp; 하위 4분의 1 {quartiles['p25']:,}원 이하 "
            f"&nbsp;·&nbsp; 상위 4분의 1 {quartiles['p75']:,}원 이상"
        )
        st.markdown(
            f"**오늘 상태** &nbsp;&nbsp; 🔴 재고 주의 {counts['STOCK']:,}곳 "
            f"&nbsp;·&nbsp; 🟡 시간대 쏠림 {counts['PEAK']:,}곳 "
            f"&nbsp;·&nbsp; ⚪ 조용한 날 {counts['CALM']:,}곳"
        )
    st.divider()

    # 3. 매출 흐름 — 요청한 매장별 그래프
    st.markdown(f"#### 매장별 매출 흐름 (최근 {TREND_DAYS}일)")
    st.line_chart(load_group_trend(engine, saledate), height=260)

    # 4. 시간대 비교
    st.markdown("#### 시간대별 매출")
    st.bar_chart(load_group_hourly(engine, saledate), height=260)

    # 5. 매장별 많이 팔린 상품
    st.markdown("#### 매장별 많이 팔린 상품")
    st.dataframe(
        load_group_top_items(engine, saledate),
        width="stretch",
        hide_index=True,
        column_config={
            "매출": st.column_config.NumberColumn(format="%d원"),
            "수량": st.column_config.NumberColumn(format="%d개"),
        },
    )

    # 6. 기간 합계
    st.markdown(f"#### 최근 {period['days']}일 매장별 합계")
    st.dataframe(
        pd.DataFrame(
            {
                "매장": [row["dept_nm"] for row in period["stores"]],
                "매출": [row["sale_amt"] for row in period["stores"]],
                "손님": [row["deal_cnt"] for row in period["stores"]],
            }
        ),
        width="stretch",
        hide_index=True,
        column_config={
            "매출": st.column_config.NumberColumn(format="%d원"),
            "손님": st.column_config.NumberColumn(format="%d명"),
        },
    )
    period_note = PERIOD_TRUNCATED_NOTE if period["stores_truncated"] else ""
    st.caption(
        f"집계 기간: {format_display_date(period['from_date'])} ~ "
        f"{format_display_date(period['to_date'])}{period_note}"
    )
    st.divider()

    # 7. 재고 부족이 반복되는 매장 — 하루 신호가 아니라 구조를 본다
    st.markdown(f"#### 최근 {SIGNAL_STREAK_DAYS}일 신호가 잦은 매장")
    st.dataframe(
        load_signal_streak(engine, saledate),
        width="stretch",
        hide_index=True,
        column_config={
            "재고 부족": st.column_config.NumberColumn(format="%d일"),
            "시간대 쏠림": st.column_config.NumberColumn(format="%d일"),
            "조용한 날": st.column_config.NumberColumn(format="%d일"),
        },
    )
    st.caption(SIGNAL_STREAK_NOTE)

    # 8. 요일 패턴
    st.markdown(f"#### 요일별 하루 평균 매출 (최근 {DOW_PATTERN_WEEKS}주)")
    st.bar_chart(load_dow_pattern(engine, saledate), height=260)

    # 9. 월별 흐름 — 합계가 아니라 일평균 (부록 B.13 결정 5)
    st.markdown("#### 월별 하루 평균 매출")
    st.line_chart(load_monthly_trend(engine), height=260)
    st.caption(MONTHLY_NOTE)
    st.divider()

    # 10. 내려받기 — 팀장에게 건넬 파일
    from src.report import group_report

    st.download_button(
        "📄 이 보고서 내려받기",
        data=group_report.report_bytes(engine, saledate),
        file_name=f"여러매장_보고_{saledate}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )
    st.caption(FOOTER_NOTE)


def main() -> None:
    """화면 전체를 그린다."""
    st.set_page_config(page_title=PAGE_TITLE, page_icon="🏪", layout="centered")

    # 어떤 위젯보다 먼저 옮겨야 한다 (부록 B.7).
    apply_pending_selection()

    configured = database_url()
    engine = get_engine(configured)

    try:
        stores = load_stores(engine)
    except SQLAlchemyError as error:
        # 연결·스키마 문제는 삼키지 않는다 (명세 14장). 대신 운영자가 고칠 수 있는
        # 말로 바꿔서 보여 준다 — 배포 사고에서 트레이스백만으로는 손을 쓸 수 없었다.
        if configured:
            st.error(connection_help(configured, str(error)))
        else:
            st.error(missing_connection_help())
        return

    if stores.empty:
        st.warning(
            "데이터가 아직 없어요. 터미널에서 아래 명령을 먼저 실행해 주세요.\n\n"
            "`python -m src.load.pipeline --from 20250701 --to 20260731`"
        )
        return

    # 보기 전환은 사이드바 맨 위에 둔다 (부록 B.7).
    view_mode = st.sidebar.radio("보기", VIEW_MODES, key=VIEW_MODE_KEY)

    dept_cd, dept_nm, saledate = _sidebar(engine, stores)
    _admin_section(engine, dept_cd)

    if not saledate:
        st.info(EMPTY_STATE)
        return

    if view_mode == VIEW_MODES[1]:
        _group_section(engine, saledate)
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
