"""적재 파이프라인 검증 — 명세 8장과 10장의 멱등·상계 테스트.

멱등은 이 데모의 라이브 시연 항목(명세 12장)이라 말이 아니라 테스트로 지킨다.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine, func, select

from src.common.config import get_engine
from src.extract.sample import SampleExtractor
from src.load import pipeline, schema

FROM_DATE = "20260701"
TO_DATE = "20260705"
SMALL_STORE = ["901003"]


@pytest.fixture()
def engine(tmp_path: Path) -> Engine:
    """테스트 전용 SQLite 엔진."""
    return get_engine(tmp_path / "pipeline.db")


def _fact_snapshot(engine: Engine) -> dict[str, tuple[int, int]]:
    """FACT 3종의 (행수, 금액합) 요약을 만든다.

    Args:
        engine: 대상 엔진.

    Returns:
        테이블명 → ``(행수, 금액합)``.
    """
    targets = {
        "FACT_RECEIPT": (schema.FACT_RECEIPT, schema.FACT_RECEIPT.c.DEALAMOUNT),
        "FACT_RECEIPT_ITEM": (schema.FACT_RECEIPT_ITEM, schema.FACT_RECEIPT_ITEM.c.SALEAMOUNT),
        "FACT_PAYMENT": (schema.FACT_PAYMENT, schema.FACT_PAYMENT.c.TENDERAMOUNT),
    }

    snapshot: dict[str, tuple[int, int]] = {}
    with engine.connect() as connection:
        for name, (table, amount_column) in targets.items():
            row = connection.execute(
                select(func.count().label("n"), func.coalesce(func.sum(amount_column), 0))
            .select_from(table)
            ).one()
            snapshot[name] = (int(row[0]), int(row[1]))
    return snapshot


def _read_table(engine: Engine, table: object, order_by: list[str]) -> pd.DataFrame:
    """테이블 전체를 정렬해 읽는다 (비교용).

    Args:
        engine: 대상 엔진.
        table: 읽을 Core 테이블.
        order_by: 정렬 컬럼명.

    Returns:
        정렬된 DataFrame.
    """
    with engine.connect() as connection:
        frame = pd.read_sql(select(table), connection)  # type: ignore[arg-type]
    return frame.sort_values(order_by).reset_index(drop=True)


# --- 명세 10장 필수 테스트 -------------------------------------------------


def test_load_idempotent(engine: Engine) -> None:
    """명세 10장: 같은 기간을 2회 적재해도 FACT 건수·합계가 같다."""
    extractor = SampleExtractor(dept_cds=SMALL_STORE)

    first_result = pipeline.load_period(extractor, FROM_DATE, TO_DATE, engine=engine)
    first = _fact_snapshot(engine)

    second_result = pipeline.load_period(extractor, FROM_DATE, TO_DATE, engine=engine)
    second = _fact_snapshot(engine)

    assert first == second
    assert (first_result.receipts, first_result.items, first_result.payments) == (
        second_result.receipts,
        second_result.items,
        second_result.payments,
    )


def test_load_idempotent_row_by_row(engine: Engine) -> None:
    """건수·합계뿐 아니라 전 행이 글자 단위로 같다."""
    extractor = SampleExtractor(dept_cds=SMALL_STORE)
    key = ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"]

    pipeline.load_period(extractor, FROM_DATE, TO_DATE, engine=engine)
    first = _read_table(engine, schema.FACT_RECEIPT, key)

    pipeline.load_period(extractor, FROM_DATE, TO_DATE, engine=engine)
    second = _read_table(engine, schema.FACT_RECEIPT, key)

    pd.testing.assert_frame_equal(first, second)


def test_cancel_nets_out(engine: Engine) -> None:
    """명세 10장: 취소를 포함한 합산이 원거래를 정확히 상계한다."""
    pipeline.load_period(
        SampleExtractor(dept_cds=SMALL_STORE), FROM_DATE, TO_DATE, engine=engine
    )

    receipts = _read_table(engine, schema.FACT_RECEIPT, ["SALEDATE", "POSNO", "DEALNO"])
    cancels = receipts[receipts["CANCELTYPE"] == "1"]
    assert not cancels.empty, "표본 기간에 취소가 없어 상계를 검증할 수 없다"

    originals = receipts.merge(
        cancels[["DEPT_CD", "ORGSALEDATE", "ORGPOSNO", "ORGDEALNO", "DEALAMOUNT"]].rename(
            columns={
                "ORGSALEDATE": "SALEDATE",
                "ORGPOSNO": "POSNO",
                "ORGDEALNO": "DEALNO",
                "DEALAMOUNT": "CANCEL_AMOUNT",
            }
        ),
        on=["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"],
    )

    assert len(originals) == len(cancels), "취소마다 원거래가 정확히 하나씩 있어야 한다"
    assert (originals["DEALAMOUNT"] + originals["CANCEL_AMOUNT"] == 0).all()


def test_partial_regen_deterministic(tmp_path: Path) -> None:
    """명세 10장: 일부 구간만 재생성한 결과가 전체 생성 중 그 구간과 같다.

    파생 시드(ADR-0005)가 제대로 작동하는지 보는 핵심 테스트다.
    """
    key = ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"]
    extractor = SampleExtractor(dept_cds=SMALL_STORE)

    wide_engine = get_engine(tmp_path / "wide.db")
    pipeline.load_period(extractor, "20260701", "20260710", engine=wide_engine)

    narrow_engine = get_engine(tmp_path / "narrow.db")
    pipeline.load_period(extractor, "20260703", "20260705", engine=narrow_engine)

    wide = _read_table(engine=wide_engine, table=schema.FACT_RECEIPT, order_by=key)
    wide_slice = (
        wide[wide["SALEDATE"].between("20260703", "20260705")].reset_index(drop=True)
    )
    narrow = _read_table(engine=narrow_engine, table=schema.FACT_RECEIPT, order_by=key)

    assert not narrow.empty
    pd.testing.assert_frame_equal(wide_slice, narrow)


def test_partial_reload_does_not_disturb_other_dates(engine: Engine) -> None:
    """구간 재적재가 기간 밖 날짜를 건드리지 않는다 (날짜 단위 DELETE)."""
    key = ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"]
    extractor = SampleExtractor(dept_cds=SMALL_STORE)

    pipeline.load_period(extractor, "20260701", "20260710", engine=engine)
    before = _read_table(engine, schema.FACT_RECEIPT, key)
    outside_before = before[~before["SALEDATE"].between("20260703", "20260705")]

    pipeline.load_period(extractor, "20260703", "20260705", engine=engine)
    after = _read_table(engine, schema.FACT_RECEIPT, key)
    outside_after = after[~after["SALEDATE"].between("20260703", "20260705")]

    pd.testing.assert_frame_equal(
        outside_before.reset_index(drop=True), outside_after.reset_index(drop=True)
    )
    assert len(after) == len(before)


# --- 삭제→적재 동작 --------------------------------------------------------


def test_load_period_replaces_existing_rows(engine: Engine) -> None:
    """기존 행을 지우고 다시 넣는다 (오염된 값이 살아남지 않는다)."""
    extractor = SampleExtractor(dept_cds=SMALL_STORE)
    pipeline.load_period(extractor, FROM_DATE, FROM_DATE, engine=engine)

    # 적재된 값을 일부러 훼손한다.
    with engine.begin() as connection:
        connection.execute(
            schema.FACT_RECEIPT.update()
            .where(schema.FACT_RECEIPT.c.SALEDATE == FROM_DATE)
            .values(DEALAMOUNT=999_999)
        )
    polluted = _fact_snapshot(engine)["FACT_RECEIPT"][1]

    pipeline.load_period(extractor, FROM_DATE, FROM_DATE, engine=engine)
    restored = _fact_snapshot(engine)["FACT_RECEIPT"][1]

    assert restored != polluted, "재적재가 훼손된 값을 덮어쓰지 못했다"


def test_load_period_only_touches_target_stores(engine: Engine) -> None:
    """점포를 지정해 재적재해도 다른 점포 데이터는 그대로다."""
    key = ["DEPT_CD", "SALEDATE", "POSNO", "DEALNO"]

    pipeline.load_period(SampleExtractor(), FROM_DATE, TO_DATE, engine=engine)
    before = _read_table(engine, schema.FACT_RECEIPT, key)
    others_before = before[before["DEPT_CD"] != "901003"].reset_index(drop=True)

    pipeline.load_period(
        SampleExtractor(dept_cds=SMALL_STORE), FROM_DATE, TO_DATE, engine=engine
    )
    after = _read_table(engine, schema.FACT_RECEIPT, key)
    others_after = after[after["DEPT_CD"] != "901003"].reset_index(drop=True)

    pd.testing.assert_frame_equal(others_before, others_after)


# --- LoadResult / 대사 -----------------------------------------------------


def test_load_result_matches_database(engine: Engine) -> None:
    """LoadResult의 건수가 실제 적재 결과와 일치한다."""
    result = pipeline.load_period(
        SampleExtractor(dept_cds=SMALL_STORE), FROM_DATE, TO_DATE, engine=engine
    )
    snapshot = _fact_snapshot(engine)

    assert result.receipts == snapshot["FACT_RECEIPT"][0]
    assert result.items == snapshot["FACT_RECEIPT_ITEM"][0]
    assert result.payments == snapshot["FACT_PAYMENT"][0]
    assert result.days == 5
    assert result.elapsed_sec >= 0.0


def test_reconciliation_reports_no_mismatch(engine: Engine) -> None:
    """대사(품질 게이트)가 전 일자에서 통과한다 (흐름도 FLOW 07)."""
    pipeline.load_period(SampleExtractor(), FROM_DATE, TO_DATE, engine=engine)

    report = pipeline.reconcile(engine, FROM_DATE, TO_DATE)

    assert len(report) == 5
    assert report["ITEMCNT_OK"].all()
    assert report["TENDERCNT_OK"].all()
    assert report["AMOUNT_OK"].all()


def test_reconciliation_detects_broken_data(engine: Engine) -> None:
    """대사가 실제로 불일치를 잡아낸다 (통과만 하는 껍데기가 아니다)."""
    pipeline.load_period(
        SampleExtractor(dept_cds=SMALL_STORE), FROM_DATE, TO_DATE, engine=engine
    )

    with engine.begin() as connection:
        connection.execute(
            schema.FACT_RECEIPT.update()
            .where(schema.FACT_RECEIPT.c.SALEDATE == FROM_DATE)
            .values(ITEMCNT=99)
        )

    report = pipeline.reconcile(engine, FROM_DATE, TO_DATE)
    failed = report[~report["ITEMCNT_OK"]]

    assert list(failed["SALEDATE"]) == [FROM_DATE]


def test_dim_store_is_loaded(engine: Engine) -> None:
    """점포 마스터가 함께 적재된다 (화면의 점포 선택에 필요)."""
    pipeline.load_period(SampleExtractor(), FROM_DATE, FROM_DATE, engine=engine)

    stores = _read_table(engine, schema.DIM_STORE, ["DEPT_CD"])

    assert list(stores["DEPT_CD"]) == ["901001", "901002", "901003"]
    assert list(stores["SIZE_GRADE"]) == ["L", "M", "S"]


def test_dim_store_load_is_idempotent(engine: Engine) -> None:
    """점포 마스터를 두 번 적재해도 행이 늘지 않는다."""
    extractor = SampleExtractor()
    pipeline.load_period(extractor, FROM_DATE, FROM_DATE, engine=engine)
    pipeline.load_period(extractor, FROM_DATE, FROM_DATE, engine=engine)

    assert len(_read_table(engine, schema.DIM_STORE, ["DEPT_CD"])) == 3


def test_reversed_range_is_rejected(engine: Engine) -> None:
    """시작일이 종료일보다 뒤면 조용히 넘어가지 않고 실패한다."""
    with pytest.raises(ValueError, match="기간"):
        pipeline.load_period(SampleExtractor(), "20260705", "20260701", engine=engine)


# --- CLI -------------------------------------------------------------------


def test_cli_parses_spec_arguments() -> None:
    """명세 8장 CLI 형식을 그대로 받는다."""
    args = pipeline.parse_args(
        ["--from", "20250701", "--to", "20260731", "--stores", "901001,901002"]
    )

    assert args.from_date == "20250701"
    assert args.to_date == "20260731"
    assert args.stores == ["901001", "901002"]


def test_cli_stores_defaults_to_all() -> None:
    """--stores를 생략하면 전 점포가 대상이다."""
    args = pipeline.parse_args(["--from", "20260701", "--to", "20260701"])

    assert args.stores is None


def test_cli_main_loads_database(tmp_path: Path) -> None:
    """CLI 진입점이 실제로 DB를 만든다."""
    db_path = tmp_path / "cli.db"

    exit_code = pipeline.main(
        ["--from", FROM_DATE, "--to", FROM_DATE, "--stores", "901003", "--db", str(db_path)]
    )

    assert exit_code == 0
    assert db_path.exists()

    snapshot = _fact_snapshot(get_engine(db_path))
    assert snapshot["FACT_RECEIPT"][0] > 0


def test_cli_rejects_bad_date(tmp_path: Path) -> None:
    """날짜 형식이 틀리면 0이 아닌 종료 코드로 알린다."""
    exit_code = pipeline.main(
        ["--from", "2026-07-01", "--to", FROM_DATE, "--db", str(tmp_path / "bad.db")]
    )

    assert exit_code != 0


# --- 배포용 슬림화 (ADR-0009) ----------------------------------------------


def test_slim_for_deploy_empties_facts_but_keeps_read_model(tmp_path: Path) -> None:
    """ADR-0009: 원장은 비우고 화면이 읽는 마트·브리핑은 남긴다."""
    from src.mart import briefing

    engine = get_engine(tmp_path / "deploy.db")
    pipeline.load_period(SampleExtractor(dept_cds=SMALL_STORE), FROM_DATE, TO_DATE, engine=engine)

    before = _fact_snapshot(engine)
    assert before["FACT_RECEIPT"][0] > 0

    marts_before = _read_table(engine, schema.MART_DAY_STORE, ["SALEDATE", "DEPT_CD"])
    briefings_before = _read_table(engine, schema.BRIEFING_DAILY, ["SALEDATE", "DEPT_CD"])

    pipeline.slim_for_deploy(engine)

    after = _fact_snapshot(engine)
    assert after["FACT_RECEIPT"][0] == 0
    assert after["FACT_RECEIPT_ITEM"][0] == 0
    assert after["FACT_PAYMENT"][0] == 0

    pd.testing.assert_frame_equal(
        marts_before, _read_table(engine, schema.MART_DAY_STORE, ["SALEDATE", "DEPT_CD"])
    )
    pd.testing.assert_frame_equal(
        briefings_before,
        _read_table(engine, schema.BRIEFING_DAILY, ["SALEDATE", "DEPT_CD"]),
    )
    assert briefing.SCHEMA_VERSION >= 1


def test_slimmed_db_still_supports_admin_regeneration(tmp_path: Path) -> None:
    """ADR-0009: 원장이 비어 있어도 관리자 재생성이 같은 숫자를 복원한다."""
    engine = get_engine(tmp_path / "regen.db")
    extractor = SampleExtractor(dept_cds=SMALL_STORE)

    pipeline.load_period(extractor, FROM_DATE, TO_DATE, engine=engine)
    marts_before = _read_table(engine, schema.MART_DAY_STORE, ["SALEDATE", "DEPT_CD"])

    pipeline.slim_for_deploy(engine)
    pipeline.load_period(extractor, FROM_DATE, TO_DATE, engine=engine)

    pd.testing.assert_frame_equal(
        marts_before, _read_table(engine, schema.MART_DAY_STORE, ["SALEDATE", "DEPT_CD"])
    )
    assert _fact_snapshot(engine)["FACT_RECEIPT"][0] > 0


def test_cli_deploy_flag_slims_database(tmp_path: Path) -> None:
    """CLI --deploy 가 구축 직후 원장을 비운다."""
    db_path = tmp_path / "cli_deploy.db"

    exit_code = pipeline.main(
        ["--from", FROM_DATE, "--to", FROM_DATE, "--stores", "901003",
         "--db", str(db_path), "--deploy"]
    )

    assert exit_code == 0
    engine = get_engine(db_path)
    assert _fact_snapshot(engine)["FACT_RECEIPT"][0] == 0
    assert len(_read_table(engine, schema.BRIEFING_DAILY, ["SALEDATE"])) == 1


def test_cli_without_deploy_keeps_facts(tmp_path: Path) -> None:
    """--deploy 를 주지 않으면 원장이 그대로 남는다 (기본 동작 보호)."""
    db_path = tmp_path / "cli_full.db"

    pipeline.main(
        ["--from", FROM_DATE, "--to", FROM_DATE, "--stores", "901003", "--db", str(db_path)]
    )

    assert _fact_snapshot(get_engine(db_path))["FACT_RECEIPT"][0] > 0
