# 30초 매장 브리핑 (데모 MIN)

역 편의점 점포장에게 매일 아침 **3줄 브리핑**(어제 결과 / 오늘 준비 / 특이 신호)을 배달하고,
**일일 보고서를 자동 생성**해 주는 서비스의 데모.

합성 데이터(점포 3곳 × 13개월) → SQLite → 집계 → 브리핑 → Streamlit 화면 → xlsx 보고서까지
전 과정이 실제로 흐른다.

- **명세(유일한 원천)**: [`doc/30초매장브리핑_바이브코딩_구현설계서_v1.3.1.md`](doc/30초매장브리핑_바이브코딩_구현설계서_v1.3.1.md)
- **작업 규칙**: [`CLAUDE.md`](CLAUDE.md)
- **결정 기록**: [`doc/adr/`](doc/adr/README.md) — 명세에 없어 새로 내린 결정만 기록

> 🧪 화면에 보이는 모든 수치는 **합성(목업) 데이터**입니다. 실제 매출이 아닙니다.

---

## 두 명령으로 실행하기

### 0. 준비 (최초 1회)

Python **3.11 이상**이 필요하다. 의존성이 이미 깔려 있다면 이 절은 건너뛴다.

```bash
# POSIX (macOS / Linux)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

```powershell
# Windows / PowerShell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 1. 데이터 구축

```bash
python -m src.load.pipeline --from 20250701 --to 20260731
```

`data/pos_mockup.db`에 13개월치(점포 3곳)를 생성·적재하고 마트와 브리핑까지 만든다.
같은 기간을 몇 번 다시 돌려도 **결과가 같다**(날짜 단위 DELETE→INSERT 멱등).

### 2. 화면 열기

```bash
streamlit run src/app/main.py
```

브라우저에서 점포를 고르면 저장된 브리핑 3줄이 바로 뜬다.
화면은 **표시만** 한다 — 계산도, 외부 호출도 하지 않는다.

---

## 검증

```bash
pytest -q                                             # 전체 테스트
python -m src.load.pipeline --from 20260701 --to 20260722   # 멱등 재실행 시연
```

---

## 구조

```text
project02_salesReport/
├── src/
│   ├── common/{config,logger,dateutil}.py   공통 설정·로거·날짜
│   ├── extract/{base,sample,oracle_stub}.py Extractor 계약과 구현체
│   ├── generate/synth.py                    합성 데이터 생성
│   ├── load/{schema,pipeline}.py            DDL·적재 파이프라인
│   ├── mart/{aggregate,briefing}.py         집계·브리핑 문장 생성
│   ├── report/daily_report.py               xlsx 일일 보고서
│   └── app/main.py                          Streamlit 화면
├── tests/
├── data/            DB 파일·씨앗 엑셀 (git 제외 — ADR-0001)
└── doc/             명세·흐름도·ADR
```

데이터는 왼쪽에서 오른쪽으로만 흐른다.
`extract` → `load` → `FACT` → `aggregate` → `MART` → `briefing` → `BRIEFING_DAILY` → 화면.

---

## 설계상 지키는 것

1. **숫자는 코드만 만든다** — 문장 템플릿은 계산 JSON 값의 치환만 한다. 산술 연산 없음.
2. **grain 분리** — `FACT_RECEIPT_ITEM`과 `FACT_PAYMENT`를 직접 조인해 금액을 합산하지 않는다
   (매출 중복). 정적 검사 테스트가 이를 지킨다.
3. **멱등** — 같은 기간 재적재 결과가 동일하다.
4. **파생 시드** — 난수는 `(기본 시드, 점포, 날짜)` 파생 시드의 독립 생성기에서 나온다.
   부분 구간만 재생성해도 같은 데이터가 나온다 ([ADR-0005](doc/adr/0005-파생-시드는-내장-hash-대신-blake2b를-쓴다.md)).
5. **실컬럼명 동결** — 테이블·컬럼명이 기간계 원본과 같다. Oracle 데이터가 그대로 들어온다.
6. **개인정보 컬럼 미생성** — 카드번호·회원번호류는 스키마에도 없다.
7. **서버측 생성** — 브리핑 문장은 배치에서 만들어 DB에 저장한다. 화면은 읽기만 한다.
   사용자 기기에서 LLM 실행·API 키 입력·추가 설치가 **어떤 단계에서도 없다.**
