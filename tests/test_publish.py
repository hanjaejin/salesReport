"""읽기 모델 발행 검증 — ADR-0011 (Supabase/PostgreSQL 배포).

원격 DB 없이도 로직을 전부 검증할 수 있도록 SQLite → SQLite 복사로 시험한다.
PostgreSQL과 다른 것은 **연결 URL뿐**이므로(명세 3장의 방언 독립 규율),
여기서 통과하면 원격에서도 같은 코드가 돈다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine, func, select

from src.common import config
from src.common.config import DB_URL_ENV, get_engine, is_sqlite, resolve_database_url
from src.extract.sample import SampleExtractor
from src.load import pipeline, publish, schema

FROM_DATE = "20260701"
TO_DATE = "20260705"


@pytest.fixture(scope="module")
def source_engine(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    """구축이 끝난 원본 DB (모듈 1회)."""
    engine = get_engine(tmp_path_factory.mktemp("publish") / "source.db")
    pipeline.load_period(SampleExtractor(), FROM_DATE, TO_DATE, engine=engine)
    return engine


def _count(engine: Engine, table: object) -> int:
    """테이블 행수를 센다.

    Args:
        engine: 대상 엔진.
        table: Core 테이블.

    Returns:
        행수.
    """
    with engine.connect() as connection:
        return int(
            connection.execute(select(func.count()).select_from(table)).scalar_one()  # type: ignore[arg-type]
        )


def _read(engine: Engine, table: object, order_by: list[str]) -> pd.DataFrame:
    """테이블을 정렬해 읽는다.

    Args:
        engine: 대상 엔진.
        table: Core 테이블.
        order_by: 정렬 컬럼.

    Returns:
        정렬된 DataFrame.
    """
    with engine.connect() as connection:
        frame = pd.read_sql(select(table), connection)  # type: ignore[arg-type]
    return frame.sort_values(order_by).reset_index(drop=True)


# --- 연결 URL 해석 (ADR-0011) ------------------------------------------------


def test_path_resolves_to_sqlite_url(tmp_path: Path) -> None:
    """파일 경로는 SQLite URL로 바뀐다 (기존 동작 유지)."""
    url = resolve_database_url(tmp_path / "a.db")

    assert url.startswith("sqlite:///")
    assert url.endswith("a.db")


def test_connection_url_passes_through() -> None:
    """연결 URL은 그대로 쓴다 — 코드를 고치지 않고 원격으로 옮긴다."""
    url = "postgresql://user:secret@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"

    assert resolve_database_url(url) == url


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """환경변수로 대상 DB를 바꿀 수 있다 (배포 환경 주입 경로)."""
    monkeypatch.setenv(DB_URL_ENV, "postgresql://u:p@host:5432/db")

    assert resolve_database_url() == "postgresql://u:p@host:5432/db"


def test_explicit_argument_beats_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """명시한 인자가 환경변수보다 우선한다 (테스트가 환경에 오염되지 않게)."""
    monkeypatch.setenv(DB_URL_ENV, "postgresql://u:p@host:5432/db")

    assert resolve_database_url(tmp_path / "b.db").startswith("sqlite:///")


def test_default_is_local_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """환경변수가 없으면 로컬 SQLite로 떨어진다 (개발 기본값)."""
    monkeypatch.delenv(DB_URL_ENV, raising=False)

    assert resolve_database_url().startswith("sqlite:///")


def test_dialect_detection(tmp_path: Path) -> None:
    """방언 판별이 동작한다 (VACUUM 같은 분기의 근거)."""
    assert is_sqlite(get_engine(tmp_path / "c.db")) is True


# --- 발행 대상 --------------------------------------------------------------


def test_read_model_excludes_facts() -> None:
    """발행 대상은 화면이 읽는 테이블뿐이다 — 원장은 보내지 않는다 (ADR-0009)."""
    names = [table.name for table in publish.READ_MODEL_TABLES]

    assert names == [
        "DIM_STORE",
        "MART_DAY_STORE",
        "MART_HOUR_STORE",
        "MART_DAY_STORE_ITEM",
        "BRIEFING_DAILY",
        "BRIEFING_DAILY_GROUP",
    ]
    for forbidden in ("FACT_RECEIPT", "FACT_RECEIPT_ITEM", "FACT_PAYMENT"):
        assert forbidden not in names


def test_publish_copies_read_model(source_engine: Engine, tmp_path: Path) -> None:
    """발행이 읽기 모델을 그대로 옮긴다."""
    target = get_engine(tmp_path / "target.db")

    counts = publish.publish(source_engine, target)

    for table in publish.READ_MODEL_TABLES:
        assert counts[table.name] == _count(source_engine, table)
        assert _count(target, table) == _count(source_engine, table)


def test_published_rows_are_identical(source_engine: Engine, tmp_path: Path) -> None:
    """옮긴 값이 원본과 한 행도 다르지 않다."""
    target = get_engine(tmp_path / "identical.db")
    publish.publish(source_engine, target)

    for table, key in (
        (schema.DIM_STORE, ["DEPT_CD"]),
        (schema.MART_DAY_STORE, ["SALEDATE", "DEPT_CD"]),
        (schema.BRIEFING_DAILY, ["SALEDATE", "DEPT_CD"]),
    ):
        pd.testing.assert_frame_equal(
            _read(source_engine, table, key), _read(target, table, key)
        )


def test_publish_creates_schema_on_empty_target(source_engine: Engine, tmp_path: Path) -> None:
    """빈 대상에도 스키마를 만들고 채운다 — 사전 준비가 필요 없다."""
    target = get_engine(tmp_path / "fresh.db")

    publish.publish(source_engine, target)

    from sqlalchemy import inspect

    tables = set(inspect(target).get_table_names())
    assert {table.name for table in publish.READ_MODEL_TABLES} <= tables


def test_publish_does_not_send_facts(source_engine: Engine, tmp_path: Path) -> None:
    """원장 테이블은 만들어지되 비어 있다 (관리자 재생성이 채울 자리)."""
    target = get_engine(tmp_path / "nofacts.db")

    publish.publish(source_engine, target)

    assert _count(source_engine, schema.FACT_RECEIPT) > 0
    assert _count(target, schema.FACT_RECEIPT) == 0


def test_publish_is_idempotent(source_engine: Engine, tmp_path: Path) -> None:
    """두 번 발행해도 행이 늘지 않는다 (덮어쓰기)."""
    target = get_engine(tmp_path / "twice.db")

    first = publish.publish(source_engine, target)
    second = publish.publish(source_engine, target)

    assert first == second
    for table in publish.READ_MODEL_TABLES:
        assert _count(target, table) == first[table.name]


def test_publish_replaces_stale_rows(source_engine: Engine, tmp_path: Path) -> None:
    """대상에 남아 있던 옛 값이 살아남지 않는다."""
    target = get_engine(tmp_path / "stale.db")
    publish.publish(source_engine, target)

    with target.begin() as connection:
        connection.execute(
            schema.MART_DAY_STORE.update().values(SALE_AMT=999_999_999)
        )

    publish.publish(source_engine, target)

    key = ["SALEDATE", "DEPT_CD"]
    pd.testing.assert_frame_equal(
        _read(source_engine, schema.MART_DAY_STORE, key),
        _read(target, schema.MART_DAY_STORE, key),
    )


def test_publish_respects_batch_size(source_engine: Engine, tmp_path: Path) -> None:
    """작은 배치로도 결과가 같다 (원격 전송은 배치로 나눠 보낸다)."""
    target = get_engine(tmp_path / "batched.db")

    counts = publish.publish(source_engine, target, batch_size=97)

    assert counts[schema.MART_HOUR_STORE.name] == _count(
        source_engine, schema.MART_HOUR_STORE
    )


def test_invalid_batch_size_rejected(source_engine: Engine, tmp_path: Path) -> None:
    """배치 크기 0 이하는 즉시 실패한다."""
    with pytest.raises(ValueError, match="batch_size"):
        publish.publish(source_engine, get_engine(tmp_path / "bad.db"), batch_size=0)


def test_publish_refuses_same_database(source_engine: Engine) -> None:
    """원본과 대상이 같으면 데이터를 지우기 전에 멈춘다."""
    with pytest.raises(ValueError, match="같은"):
        publish.publish(source_engine, source_engine)


def test_empty_source_is_rejected(tmp_path: Path) -> None:
    """빈 원본으로 발행하면 대상을 비워 버리므로 막는다."""
    empty = get_engine(tmp_path / "empty.db")
    schema.create_all(empty)

    with pytest.raises(ValueError, match="비어"):
        publish.publish(empty, get_engine(tmp_path / "victim.db"))


# --- CLI -------------------------------------------------------------------


def test_cli_requires_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """대상 URL이 없으면 실패한다 — 실수로 로컬에 덮어쓰지 않게."""
    monkeypatch.delenv(publish.TARGET_URL_ENV, raising=False)

    assert publish.main([]) != 0


def test_cli_publishes_to_target(
    source_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI가 원본에서 대상으로 옮긴다."""
    target_path = tmp_path / "cli_target.db"
    source_url = str(source_engine.url)

    monkeypatch.setattr(publish, "resolve_database_url", lambda target=None: source_url)

    exit_code = publish.main(["--target", f"sqlite:///{target_path}"])

    assert exit_code == 0
    assert _count(get_engine(target_path), schema.BRIEFING_DAILY) > 0


def test_cli_reads_target_from_env(
    source_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """대상 URL을 환경변수로 줄 수 있다 — 비밀을 명령행에 남기지 않는 경로."""
    target_path = tmp_path / "env_target.db"
    source_url = str(source_engine.url)

    monkeypatch.setattr(publish, "resolve_database_url", lambda target=None: source_url)
    monkeypatch.setenv(publish.TARGET_URL_ENV, f"sqlite:///{target_path}")

    assert publish.main([]) == 0
    assert _count(get_engine(target_path), schema.BRIEFING_DAILY) > 0


def test_cli_masks_credentials_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """로그에 접속 비밀번호가 남지 않는다."""
    masked = publish.mask_url(
        "postgresql://postgres.abcd:SuperSecret123@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    )

    assert "SuperSecret123" not in masked
    assert "aws-0-ap-northeast-2.pooler.supabase.com" in masked
    assert "postgres.abcd" not in masked


def test_mask_url_handles_plain_sqlite() -> None:
    """비밀이 없는 URL은 그대로 보여 준다."""
    assert publish.mask_url("sqlite:///data/pos_mockup.db") == "sqlite:///data/pos_mockup.db"


# --- 연결 사전 점검 (ADR-0011 보강) ------------------------------------------


def test_remote_engine_limits_connect_wait() -> None:
    """원격 연결은 기다리다 포기한다 — 발표 직전에 무한 대기하는 사고를 막는다."""
    args = config._connect_args("postgresql://u:p@host:5432/db")

    assert args["connect_timeout"] == config.CONNECT_TIMEOUT_SEC


def test_sqlite_engine_takes_no_connect_args() -> None:
    """로컬 SQLite에는 연결 인자를 붙이지 않는다 (드라이버가 모르는 키다)."""
    assert config._connect_args("sqlite:///data/pos_mockup.db") == {}


def test_preflight_reports_reachable_target(tmp_path: Path) -> None:
    """닿는 대상이면 방언을 확인해 돌려준다 — 무엇에 붙었는지 눈으로 본다."""
    engine = get_engine(tmp_path / "reachable.db")

    assert "sqlite" in publish.preflight(engine)


def test_preflight_explains_wrong_pooler_host() -> None:
    """`tenant/user not found` 는 비밀번호가 아니라 **호스트**가 틀렸다는 뜻이다."""
    hint = publish.diagnose("FATAL:  (ENOTFOUND) tenant/user postgres.abcd not found")

    assert "호스트" in hint
    assert "pooler" in hint


def test_preflight_explains_unresolvable_direct_host() -> None:
    """Direct connection 은 IPv6 전용이라 IPv4 망에서 이름이 풀리지 않는다."""
    hint = publish.diagnose('could not translate host name "db.abcd.supabase.co" to address')

    assert "Session pooler" in hint


def test_preflight_explains_bad_password() -> None:
    """비밀번호 오류는 비밀번호 오류라고 말한다 — 엉뚱한 곳을 뒤지지 않게."""
    hint = publish.diagnose('FATAL:  password authentication failed for user "postgres"')

    assert "비밀번호" in hint


def test_diagnose_falls_back_to_generic_hint() -> None:
    """모르는 오류도 조용히 넘기지 않는다 (빈 except 금지)."""
    assert publish.diagnose("연결이 알 수 없는 이유로 끊겼습니다") != ""


def test_cli_stops_before_copying_when_target_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """대상에 닿지 않으면 복사를 시작하지 않고 진단과 함께 멈춘다."""
    def _fail(_engine: Engine) -> str:
        raise ConnectionError("대상에 닿지 않습니다 — 호스트를 확인하세요")

    monkeypatch.setattr(publish, "preflight", _fail)
    monkeypatch.setenv(publish.TARGET_URL_ENV, "postgresql://u:p@nowhere.invalid:5432/postgres")

    assert publish.main([]) == 3


def test_read_model_includes_group_briefing() -> None:
    """부록 B.9: 관리자 화면이 읽으므로 그룹 요약도 발행 대상이다."""
    names = [table.name for table in publish.READ_MODEL_TABLES]

    assert "BRIEFING_DAILY_GROUP" in names


def test_published_group_briefing_matches_source(source_engine: Engine, tmp_path: Path) -> None:
    """그룹 요약이 원본과 같은 내용으로 발행된다."""
    target = get_engine(tmp_path / "group_target.db")

    publish.publish(source_engine, target)

    assert _read(target, schema.BRIEFING_DAILY_GROUP, ["SALEDATE"]).equals(
        _read(source_engine, schema.BRIEFING_DAILY_GROUP, ["SALEDATE"])
    )
