"""날짜 유틸 — 이 프로젝트의 날짜는 전부 ``YYYYMMDD`` 문자열이다.

기간계 원본(TB_POD208 등)이 SALEDATE를 문자열로 들고 있고, 명세 4장 DDL도
``SALEDATE TEXT``로 동결돼 있다. 경계에서만 ``datetime.date``로 바꾸고,
모듈 사이를 오가는 값은 항상 문자열로 유지한다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

#: 요일 인덱스(월=0)를 한국어 이름으로 (명세 7.2 ``dow_name``)
_DOW_NAMES: tuple[str, ...] = (
    "월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일",
)

DATE_FMT = "%Y%m%d"


def parse_date(yyyymmdd: str) -> date:
    """``YYYYMMDD`` 문자열을 ``date``로 바꾼다.

    Args:
        yyyymmdd: 8자리 날짜 문자열.

    Returns:
        해당 날짜.

    Raises:
        ValueError: 형식이 8자리 날짜가 아닐 때.
    """
    return datetime.strptime(yyyymmdd, DATE_FMT).date()


def format_date(value: date) -> str:
    """``date``를 ``YYYYMMDD`` 문자열로 바꾼다.

    Args:
        value: 변환할 날짜.

    Returns:
        8자리 날짜 문자열.
    """
    return value.strftime(DATE_FMT)


def shift_days(yyyymmdd: str, days: int) -> str:
    """날짜를 일수만큼 이동한다.

    Args:
        yyyymmdd: 기준 날짜.
        days: 이동할 일수 (음수면 과거).

    Returns:
        이동한 날짜의 ``YYYYMMDD`` 문자열.
    """
    return format_date(parse_date(yyyymmdd) + timedelta(days=days))


def date_range(from_date: str, to_date: str) -> list[str]:
    """두 날짜 사이의 모든 날짜를 오름차순으로 만든다 (양끝 포함).

    Args:
        from_date: 시작일 ``YYYYMMDD``.
        to_date: 종료일 ``YYYYMMDD``.

    Returns:
        ``YYYYMMDD`` 문자열 리스트. ``from_date > to_date`` 이면 빈 리스트.
    """
    start = parse_date(from_date)
    end = parse_date(to_date)
    if start > end:
        return []
    return [format_date(start + timedelta(days=offset)) for offset in range((end - start).days + 1)]


def dow_index(yyyymmdd: str) -> int:
    """요일 인덱스를 반환한다 (월=0 … 일=6).

    Args:
        yyyymmdd: 대상 날짜.

    Returns:
        0~6 사이의 요일 인덱스.
    """
    return parse_date(yyyymmdd).weekday()


def dow_name(yyyymmdd: str) -> str:
    """한국어 요일 이름을 반환한다 (명세 7.2 ``dow_name``).

    Args:
        yyyymmdd: 대상 날짜.

    Returns:
        "월요일" ~ "일요일".
    """
    return _DOW_NAMES[dow_index(yyyymmdd)]


def previous_same_dow(yyyymmdd: str, weeks: int) -> list[str]:
    """직전 N주의 같은 요일 날짜를 최근 순으로 만든다 (명세 7.2 요일 기준선).

    기준일 자신은 포함하지 않는다.

    Args:
        yyyymmdd: 기준 날짜.
        weeks: 거슬러 올라갈 주 수.

    Returns:
        ``[1주 전, 2주 전, …]`` 순의 ``YYYYMMDD`` 리스트.
    """
    return [shift_days(yyyymmdd, -7 * week) for week in range(1, weeks + 1)]
