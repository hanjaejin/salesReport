"""화면 계층 검증 — 명세 9장·불변식 7.

화면은 **표시만** 한다. 재계산·외부 호출·LLM이 없다는 것을 정적 검사로 고정하고,
데이터 접근 함수가 저장된 값을 그대로 돌려주는지 확인한다.
"""

from __future__ import annotations

import ast
import os
import inspect as inspect_module
import json
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from src.common.config import get_engine
from src.extract.sample import SampleExtractor
from src.load import pipeline, schema

SALEDATE = "20260703"
DEPT_CD = "901001"


@pytest.fixture(scope="module")
def built_engine(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    """화면이 읽을 수 있는 상태의 엔진 (모듈 1회)."""
    engine = get_engine(tmp_path_factory.mktemp("app") / "app.db")
    pipeline.load_period(SampleExtractor(), "20260628", "20260705", engine=engine)
    return engine


# --- 불변식 7: 화면은 계산하지 않는다 ---------------------------------------


def test_app_makes_no_external_calls() -> None:
    """명세 14장: 외부 CDN·API·모델 다운로드가 없다 (폐쇄망 대비).

    데이터베이스 연결은 여기서 말하는 "외부 호출"이 아니다 — 제품의 데이터 계층이며,
    폐쇄망에서는 망 안의 PostgreSQL을 가리키면 된다 (ADR-0011). 금지하는 것은
    화면이 제3자 서비스에 HTTP로 붙거나 모델을 내려받는 일이다.
    """
    from src.app import main

    source = Path(inspect_module.getfile(main)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {
        "requests", "httpx", "urllib", "urllib3", "socket", "http",
        "openai", "anthropic", "transformers", "torch", "boto3",
    }
    assert not (imported & forbidden), f"외부 호출 모듈을 import 한다: {imported & forbidden}"


def _renderable_strings(module: object) -> list[str]:
    """화면에 도달할 수 있는 문자열 리터럴만 모은다.

    docstring은 제외한다 — 명세 14장이 금지한 것은 "화면에 전문용어 **노출**"이고,
    개발자용 설명은 화면에 나가지 않는다. 대신 그 밖의 모든 문자열 리터럴은
    st.* 로 흘러갈 수 있으므로 전부 검사한다.

    Args:
        module: 검사할 모듈.

    Returns:
        문자열 리터럴 목록.
    """
    tree = ast.parse(Path(inspect_module.getfile(module)).read_text(encoding="utf-8"))

    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }

    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_app_shows_no_jargon() -> None:
    """명세 9장·14장: 화면에 노출되는 문구에 전문용어가 없다."""
    from src.app import main

    texts = " ".join(_renderable_strings(main))

    for word in ("객단가", "증감률", "AI", "LLM", "머신러닝", "예측", "분석"):
        assert word not in texts, f"금지 용어 '{word}' 가 화면 문자열에 있다"


def test_app_never_recomputes_briefing_lines() -> None:
    """불변식 7: 화면이 브리핑 문장을 다시 만들지 않는다.

    ``briefing`` 모듈의 렌더·계산 함수를 화면이 부르면 이 테스트가 깨진다.
    화면은 오직 저장된 ``briefing_lines`` 를 출력해야 한다.
    """
    from src.app import main

    source = Path(inspect_module.getfile(main)).read_text(encoding="utf-8")

    for forbidden in (
        "render_line", "build_payload", "build_cards", "pick_signal",
        "build_marts", "build_briefings", "aggregate.", "briefing.",
    ):
        assert forbidden not in source, (
            f"화면이 '{forbidden}' 를 참조한다 — 표시 계층이 계산하고 있다"
        )


def test_mockup_badge_is_always_present() -> None:
    """명세 9장·12장: 목업 배지가 상시 노출된다."""
    from src.app import main

    assert "목업" in main.MOCKUP_BADGE

    source = Path(inspect_module.getfile(main)).read_text(encoding="utf-8")
    assert "MOCKUP_BADGE" in source


def test_footer_disclaims_accounting_use() -> None:
    """명세 9장 하단 고지 문구."""
    from src.app import main

    assert main.FOOTER_NOTE == "본 화면의 수치는 운영 참고용입니다. 정산·회계 기준이 아닙니다."


# --- 데이터 접근 ------------------------------------------------------------


def test_load_stores_returns_three(built_engine: Engine) -> None:
    """점포 선택 목록이 3곳이다 (명세 9장 사이드바)."""
    from src.app import main

    stores = main.load_stores(built_engine)

    assert list(stores["DEPT_CD"]) == ["901001", "901002", "901003"]


def test_load_briefing_returns_saved_payload(built_engine: Engine) -> None:
    """화면이 읽는 값이 DB에 저장된 것과 정확히 같다 (재계산 없음)."""
    from src.app import main

    payload = main.load_briefing(built_engine, SALEDATE, DEPT_CD)

    with built_engine.connect() as connection:
        raw = connection.execute(
            select(schema.BRIEFING_DAILY.c.PAYLOAD_JSON).where(
                schema.BRIEFING_DAILY.c.SALEDATE == SALEDATE,
                schema.BRIEFING_DAILY.c.DEPT_CD == DEPT_CD,
            )
        ).scalar_one()

    assert payload == json.loads(raw)


def test_load_briefing_missing_day_returns_none(built_engine: Engine) -> None:
    """명세 9장 빈 상태: 없는 날짜는 예외가 아니라 None을 돌려준다."""
    from src.app import main

    assert main.load_briefing(built_engine, "20991231", DEPT_CD) is None


def test_available_dates_are_sorted(built_engine: Engine) -> None:
    """기준일 선택 범위가 정렬돼 있다 (기본값은 최신일 — 명세 9장)."""
    from src.app import main

    dates = main.load_available_dates(built_engine, DEPT_CD)

    assert dates == sorted(dates)
    assert dates[-1] == "20260705"


def test_load_trend_covers_requested_window(built_engine: Engine) -> None:
    """최근 14일 추이가 기준일까지를 포함한다."""
    from src.app import main

    trend = main.load_trend(built_engine, SALEDATE, DEPT_CD, days=5)

    assert not trend.empty
    assert trend["SALEDATE"].max() == SALEDATE
    assert len(trend) <= 5
    assert list(trend["SALEDATE"]) == sorted(trend["SALEDATE"])


def test_record_feedback_appends_row(built_engine: Engine) -> None:
    """피드백이 FEEDBACK_LOG에 쌓인다 (명세 9장)."""
    from src.app import main

    def count() -> int:
        """현재 피드백 행수."""
        with built_engine.connect() as connection:
            return len(connection.execute(select(schema.FEEDBACK_LOG.c.TS)).all())

    before = count()
    main.record_feedback(built_engine, SALEDATE, DEPT_CD, "G2", "ACCEPT")
    main.record_feedback(built_engine, SALEDATE, DEPT_CD, "G2", "DECLINE")

    assert count() == before + 2


def test_feedback_actions_match_ddl_values() -> None:
    """명세 4장: ACTION은 ACCEPT/DECLINE 이다."""
    from src.app import main

    assert set(main.FEEDBACK_ACTIONS.values()) == {"ACCEPT", "DECLINE"}


def test_load_totals_matches_mart(built_engine: Engine) -> None:
    """관리자 비교용 총계가 마트 합계와 같다."""
    from sqlalchemy import func

    from src.app import main

    totals = main.load_totals(built_engine, "20260701", "20260705")

    with built_engine.connect() as connection:
        expected = connection.execute(
            select(
                func.sum(schema.MART_DAY_STORE.c.SALE_AMT),
                func.sum(schema.MART_DAY_STORE.c.DEAL_CNT),
            ).where(schema.MART_DAY_STORE.c.SALEDATE.between("20260701", "20260705"))
        ).one()

    assert totals["sale_amt"] == expected[0]
    assert totals["deal_cnt"] == expected[1]


# --- 표시 보조 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("diff_pct", "expected"),
    [
        (8.0, "🔺 8.0% 늘었어요"),
        (-3.1, "🔻 -3.1% 줄었어요"),
        (0.0, "➖ 그대로예요"),
        (None, "비교할 날이 없어요"),
    ],
)
def test_arrow_text(diff_pct: float | None, expected: str) -> None:
    """전일 대비 화살표가 이모지·부호·문장 셋으로 표현된다 (명세 9장 스타일)."""
    from src.app import main

    assert main.arrow_text(diff_pct) == expected


def test_format_display_date() -> None:
    """화면 날짜 표기는 YYYY-MM-DD 이다 (명세 9장 상단 고정줄)."""
    from src.app import main

    assert main.format_display_date("20260703") == "2026-07-03"


def test_no_deprecated_streamlit_api() -> None:
    """폐기 예정 API를 쓰지 않는다 — 클라우드가 최신 Streamlit을 깔아도 깨지지 않게.

    명세 9장은 ``use_container_width=True`` 를 지정했지만 Streamlit이 이를 폐기하고
    ``width="stretch"`` 로 옮겼다. 명세가 요구한 것은 "버튼을 가로로 꽉 채운다"는
    **동작**이므로, 현재 API로 같은 동작을 낸다 (ADR-0009).
    """
    from src.app import main

    source = Path(inspect_module.getfile(main)).read_text(encoding="utf-8")

    assert "use_container_width" not in source
    assert 'width="stretch"' in source


# --- 연결 대상 (ADR-0011) ----------------------------------------------------


def test_database_url_returns_none_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """secrets.toml 이 없는 로컬 실행에서 예외 없이 None을 돌려준다.

    개발자 PC에 실제 `secrets.toml` 이 있어도 결과가 달라지면 안 되므로,
    "파일이 없는 상태"를 명시적으로 흉내 낸다.
    """
    from src.app import main

    class _MissingSecretsFile:
        """`secrets.toml` 이 없을 때의 streamlit 동작을 흉내 낸다."""

        def get(self, _key: str) -> None:
            """조회하면 파일이 없다고 알린다.

            Args:
                _key: 조회할 키 (쓰지 않는다).

            Raises:
                FileNotFoundError: 항상.
            """
            raise FileNotFoundError("secrets.toml 없음")

    # streamlit 의 Secrets 객체는 속성 대입을 막으므로 모듈의 `st` 를 통째로 바꾼다.
    monkeypatch.setattr(main.st, "secrets", _MissingSecretsFile(), raising=False)

    assert main.database_url() is None


def test_database_url_reads_streamlit_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """배포에서는 Streamlit secrets의 연결 문자열을 쓴다."""
    import streamlit as st

    from src.app import main

    monkeypatch.setattr(
        st, "secrets", {main.DB_URL_ENV: "postgresql://u:p@host:5432/postgres"}
    )

    assert main.database_url() == "postgresql://u:p@host:5432/postgres"


def test_app_never_displays_connection_string() -> None:
    """연결 문자열을 화면에 찍지 않는다 — 자격 증명이 새면 안 된다."""
    from src.app import main

    source = Path(inspect_module.getfile(main)).read_text(encoding="utf-8")

    assert "st.caption(f\"데이터: {DB_PATH.name if is_sqlite(engine)" in source
    for leak in ("st.write(database_url", "st.caption(database_url", "st.text(database_url"):
        assert leak not in source


def test_admin_regen_window_is_seven_days() -> None:
    """명세 9장: 관리자 재생성은 최근 7일 구간이다."""
    from src.app import main

    assert main.ADMIN_REGEN_DAYS == 7
    assert main.TREND_DAYS == 14


# --- 관리자 재생성이 실제로 멱등인지 ----------------------------------------


def test_admin_regeneration_keeps_totals(built_engine: Engine) -> None:
    """명세 9장·12장: 재생성 전후 총매출·거래건수가 같다 (화면 멱등 시연의 근거)."""
    from src.app import main

    before = main.load_totals(built_engine, "20260701", "20260705")

    pipeline.load_period(SampleExtractor(), "20260701", "20260705", engine=built_engine)

    after = main.load_totals(built_engine, "20260701", "20260705")

    assert before == after


# --- 클라우드 진입점 (명세 15장) ---------------------------------------------


def _repo_root() -> Path:
    """저장소 루트 경로를 돌려준다.

    Returns:
        ``streamlit_app.py`` 가 놓이는 저장소 루트.
    """
    return Path(__file__).resolve().parents[1]


def test_cloud_entry_point_exists() -> None:
    """저장소 루트에 진입점이 있다 — Streamlit Cloud의 기본 파일명이다."""
    assert (_repo_root() / "streamlit_app.py").is_file()


def test_cloud_entry_point_puts_repo_root_on_path() -> None:
    """진입점은 import 하기 **전에** 저장소 루트를 경로에 넣는다.

    `streamlit run` 은 실행 스크립트의 폴더만 경로에 넣는다. 그 순서가 뒤집히면
    클라우드에서 `No module named 'src'` 로 죽는다.
    """
    source = (_repo_root() / "streamlit_app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    path_setup_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "insert"
    )
    app_import_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src.app")
    )

    assert path_setup_line < app_import_line


def test_app_imports_when_only_repo_root_on_path(tmp_path: Path) -> None:
    """저장소 루트만 경로에 있으면 어디서 실행해도 화면이 import 된다.

    진입점이 보장하려는 조건을 그대로 재현한다 — 작업 디렉토리는 무관해야 한다.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, r'{_repo_root()}'); "
            "import src.app.main; print('OK')",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
    )

    assert result.stdout.strip() == "OK", result.stderr[-500:]


def test_cloud_entry_point_renders_without_exception(built_engine: Engine) -> None:
    """진입점을 실제로 실행해 화면이 예외 없이 그려지는지 본다 (배포 경로 그대로).

    import 경로·secrets 읽기·질의·렌더링이 한 번에 걸리는 유일한 지점이라,
    클라우드에서 처음 터지는 사고를 여기서 잡는다.
    """
    from streamlit.testing.v1 import AppTest

    app_test = AppTest.from_file(str(_repo_root() / "streamlit_app.py"), default_timeout=60)
    app_test.secrets["POS_BRIEFING_DB_URL"] = str(built_engine.url)

    app_test.run()

    assert not app_test.exception, [error.value for error in app_test.exception]
    assert any("오늘의 브리핑" in block.value for block in app_test.markdown)


def test_app_explains_missing_connection_instead_of_crashing(tmp_path: Path) -> None:
    """연결이 안 되면 트레이스백 대신 **무엇을 고쳐야 하는지** 보여 준다.

    클라우드 배포에서 실제로 겪은 사고다. 표가 없는 빈 DB를 만나면
    `load_stores` 가 그대로 터져 운영자가 원인을 알 수 없었다 (명세 15장).
    """
    from streamlit.testing.v1 import AppTest

    empty_db = tmp_path / "empty.db"
    empty_db.touch()

    app_test = AppTest.from_file(str(_repo_root() / "streamlit_app.py"), default_timeout=60)
    app_test.secrets["POS_BRIEFING_DB_URL"] = f"sqlite:///{empty_db}"

    app_test.run()

    assert not app_test.exception, [error.value for error in app_test.exception]
    shown = " ".join(block.value for block in app_test.error) + " ".join(
        block.value for block in app_test.warning
    )
    assert "POS_BRIEFING_DB_URL" in shown


def test_connection_failure_never_shows_credentials(tmp_path: Path) -> None:
    """진단 화면에도 비밀번호가 새지 않는다 (ADR-0011)."""
    from src.app import main

    message = main.connection_help(
        "postgresql://postgres.abcd:SuperSecret123@aws-1-x.pooler.supabase.com:5432/postgres",
        "could not translate host name",
    )

    assert "SuperSecret123" not in message
    assert "postgres.abcd" not in message
