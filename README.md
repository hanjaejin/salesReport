# 30초 매장 브리핑 (데모 MIN)

역 편의점 점포장에게 매일 아침 **3줄 브리핑**(어제 결과 / 오늘 준비 / 특이 신호)을 배달하고,
**일일 보고서를 자동 생성**해 주는 서비스의 데모.

합성 데이터(점포 3곳 × 13개월) → SQLite → 집계 → 브리핑 → Streamlit 화면 → xlsx 보고서까지
전 과정이 실제로 흐른다.

- **명세(유일한 원천)**: [`doc/30초매장브리핑_바이브코딩_구현설계서_v1.3.1.md`](doc/30초매장브리핑_바이브코딩_구현설계서_v1.3.1.md)
- **작업 규칙**: [`CLAUDE.md`](CLAUDE.md)
- **명세 부록**: [`doc/30초매장브리핑_구현설계서_부록A_결품예상.md`](doc/30초매장브리핑_구현설계서_부록A_결품예상.md) — 결품 예상 카드(G3)
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
약 **1분 30초** 걸린다 (영수증 463,544건 · 상품 685,917행 · 결제 486,840행 ·
재고 스냅샷 142,560행 · 브리핑 1,188건).

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
pytest -q                                                    # 전체 테스트 (약 1분)
python -m src.load.pipeline --from 20260701 --to 20260722     # 멱등 재실행 시연
```

CLI 옵션:

| 옵션 | 설명 |
|---|---|
| `--from` / `--to` | 기간 `YYYYMMDD` (필수) |
| `--stores` | 점포코드 쉼표 구분. 생략하면 전 점포 |
| `--db` | SQLite 경로. 생략하면 `data/pos_mockup.db` |
| `--deploy` | 구축 후 원장을 비워 배포용으로 줄인다 (오프라인 배포용 대안 경로 — [ADR-0009](doc/adr/0009-배포용-DB는-읽기-모델만-담는다.md)) |

연결 대상은 코드를 고치지 않고 바꾼다 — `POS_BRIEFING_DB_URL` 환경변수에 연결 URL을 넣으면
파이프라인·화면·보고서가 모두 그 DB를 쓴다. 없으면 로컬 `data/pos_mockup.db`다.

---

## 오프라인 스냅샷 (발표 백업 — 명세 15장 보험 2)

```bash
python -m src.report.snapshot --date 20260609
```

`data/snapshot/`에 **인터넷·서버 없이 열리는 HTML 5장**을 만든다 — 목차 + 브리핑·자세히·보고서·관리자.
총 31KB이고 외부 참조(CDN·그림·웹폰트)가 하나도 없어, **폴더째 USB에 담아 더블클릭하면 열린다.**
차트는 인라인 SVG로 직접 그린다 ([ADR-0010](doc/adr/0010-정적-스냅샷은-자체완결-HTML로-만들고-저장소에-넣는다.md)).

| 장 | 내용 |
|---|---|
| `index.html` | 목차 |
| `01_브리핑.html` | 세 점포의 3줄 — 점포마다 다른 문장 |
| `02_자세히.html` | 매출·손님 수·1인당 구매액, 시간대 차트, TOP5, **곧 떨어질 수 있는 상품**, 최근 흐름 |
| `03_보고서.html` | 내려받는 일일 보고 xlsx와 같은 내용 (요약·TOP5·시간대·재고) |
| `04_관리자.html` | 최근 7일 재생성 전/후 수치 비교 (멱등 증거) |

| 옵션 | 설명 |
|---|---|
| `--date` | 기준일 `YYYYMMDD` (필수) |
| `--out` | 저장 디렉토리 (기본 `data/snapshot`) |
| `--no-regen` | 관리자 장의 재생성 측정을 건너뛴다 (기본은 실제로 돌려 전/후를 잰다) |

> 관리자 장은 기본적으로 `load_period()`를 **실제로 호출**해 전/후를 측정한다.
> 멱등이라 데이터는 바뀌지 않으며, 값이 달라지면 스냅샷에 그대로 적는다.

---

## 구조

```text
project02_salesReport/
├── src/
│   ├── common/{config,logger,dateutil}.py   공통 설정·로거·날짜
│   ├── extract/{base,sample,oracle_stub}.py Extractor 계약과 구현체
│   ├── generate/synth.py                    합성 데이터 생성 (판매·재고)
│   ├── load/{schema,pipeline,publish}.py    DDL·적재 파이프라인·원격 발행
│   ├── mart/{aggregate,briefing}.py         집계·브리핑 문장 생성
│   ├── report/{daily_report,snapshot}.py    xlsx 보고서·오프라인 HTML 스냅샷
│   └── app/main.py                          Streamlit 화면
├── tests/
├── data/
│   ├── (pos_mockup.db·씨앗 엑셀)            git 제외 — ADR-0001
│   └── snapshot/                            발표 백업 HTML (커밋 대상 — ADR-0010)
└── doc/             명세·흐름도·ADR
```

데이터는 왼쪽에서 오른쪽으로만 흐른다.
`extract` → `load` → `FACT` → `aggregate` → `MART` → `briefing` → `BRIEFING_DAILY` → 화면.

씨앗은 실샘플 영수증 1일치(점포 202246, 3,412행)다. 여기서 뽑은 상품 사전 120종이
[`src/generate/seed_catalog.json`](src/generate/seed_catalog.json)에 동결돼 있고,
13개월치는 그 사전으로 만든 가상 데이터다 ([ADR-0002](doc/adr/0002-상품-사전은-실샘플-추출-후-JSON-동결.md)).

---

## 설계상 지키는 것

1. **숫자는 코드만 만든다** — 문장 템플릿은 계산 JSON 값의 치환만 한다. 산술 연산 없음.
   렌더 함수에 산술이 없다는 것을 AST 정적 검사가 지킨다.
2. **grain 분리** — `FACT_RECEIPT_ITEM`과 `FACT_PAYMENT`를 직접 조인해 금액을 합산하지 않는다
   (매출 중복). 정적 검사 테스트가 이를 지킨다.
3. **멱등** — 같은 기간 재적재 결과가 동일하다.
4. **파생 시드** — 난수는 `(기본 시드, 점포, 날짜)` 파생 시드의 독립 생성기에서 나온다.
   부분 구간만 재생성해도 같은 데이터가 나온다 ([ADR-0005](doc/adr/0005-파생-시드는-내장-hash-대신-blake2b를-쓴다.md)).
5. **실컬럼명 동결** — 테이블·컬럼명이 기간계 원본과 같다. Oracle 데이터가 그대로 들어온다.
6. **개인정보 컬럼 미생성** — 카드번호·회원번호류는 스키마에도 없다.
7. **서버측 생성** — 브리핑 문장은 배치에서 만들어 DB에 저장한다. 화면은 읽기만 한다.
   사용자 기기에서 LLM 실행·API 키 입력·추가 설치가 **어떤 단계에서도 없다.**

---

## 데모 리허설 (명세 12장)

### 권장 기준일: **2026-06-09**

2줄의 세 가지 상태(결품·시간대·침묵)를 한 화면 세트에서 모두 보여 주는 날이다.

| 점포 | 카드 | 브리핑 |
|---|---|---|
| 중앙역 대형점 (L) | G4·**G3 결품** | 어제 2,999,800원 — 평소 화요일보다 33.4% 좋았어요 🔺<br>크리오)휴대용칫솔치약세트 외 6개 상품의 재고가 얼마 남지 않았어요 — 오늘 채워 두는 게 좋아요 |
| 동부역 중형점 (M) | **G6 침묵** | 어제 1,182,900원 — 평소 화요일보다 31.4% 좋았어요 🔺<br>오늘은 평소 준비대로 하시면 충분해요 |
| 간이역 소형점 (S) | G4·**G2 시간대** | 어제 226,750원 — 평소 화요일보다 7.5% 좋았어요 🔺<br>아침(07~09시)에 하루 매출의 27.3%가 나와요 — 그 전에 진열을 확인해 보세요 |

데모 서사가 자연스럽게 이어진다: **대형점은 잘 팔려서 재고가 부족하고, 중형점은 조용하고,
소형점은 아침 집중을 알린다.** 매출과 재고가 결합된 판단이 한눈에 보인다.

> **왜 날짜를 고르는가**: 2줄은 G3(결품)·G2(시간대)·G6(침묵)가 나눠 갖고,
> 어느 것이 나오는지는 그날의 데이터가 정한다. 세 상태를 한 번에 보여 주는 날이
> 396일 중 **72일**이다 ([부록 A](doc/30초매장브리핑_구현설계서_부록A_결품예상.md) 실측).
>
> 다른 후보일: `20260624`, `20260531`, `20260506`, `20260426`, `20251216`, `20251118`

### 체크리스트

- [ ] 3개 점포의 브리핑이 서로 다르다 (기준일 2026-06-09)
- [ ] 2줄이 결품·시간대·침묵 세 가지로 갈린다 (G3 / G2 / G6)
- [ ] 브리핑 숫자 = 자세히 화면 숫자
- [ ] `--from 20260701 --to 20260722` 재실행 후 화면 값 동일
- [ ] 보고서 xlsx 열어서 숫자 확인
- [ ] 화면에 전문용어 없음, 목업 배지 상시
- [ ] 첫 브리핑 표시 3초 이내
- [ ] 관리자 [최근 7일 재생성] 실행 전/후 수치 동일
- [ ] `data/snapshot/index.html`을 인터넷 끊고 열어 4장 모두 확인 (보험 2)

---

## 클라우드 배포 (명세 15장)

데이터는 **Supabase(PostgreSQL)** 에 두고, 저장소에는 코드·스키마·씨앗 사전만 둔다
([ADR-0011](doc/adr/0011-데이터는-Supabase에-두고-저장소에는-코드만-둔다.md)).
`data/pos_mockup.db`는 계속 git 제외 대상이다.

### 1. 로컬에서 구축

```bash
python -m src.load.pipeline --from 20250701 --to 20260731
```

### 2. 읽기 모델을 Supabase로 발행

```bash
# 대상 URL은 환경변수로 준다 — 명령행에 적으면 셸 이력에 비밀번호가 남는다
export POS_BRIEFING_TARGET_URL="postgresql://postgres.<ref>:<password>@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
python -m src.load.publish
```

```powershell
# Windows / PowerShell
$env:POS_BRIEFING_TARGET_URL = "postgresql://postgres.<ref>:<password>@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
python -m src.load.publish
```

스키마 생성부터 적재까지 이 한 줄로 끝난다. 옮기는 것은 **132,789행**(마트 3종 + 브리핑 + 점포)이고,
원장 463,544건은 보내지 않는다 — 화면이 읽지 않고, 필요하면 로컬에서 되살릴 수 있기 때문이다.

> ⚠️ 연결 문자열은 Supabase 대시보드의 **Session pooler**(포트 **5432**)를 쓴다.
> Transaction pooler(6543)는 준비된 구문을 지원하지 않아 대량 적재에서 실패한다.

### 3. Streamlit Cloud 설정

앱 설정의 **Secrets**에 한 줄을 넣는다.

```toml
POS_BRIEFING_DB_URL = "postgresql://postgres.<ref>:<password>@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
```

이 값이 없으면 앱은 로컬 `data/pos_mockup.db`로 떨어지므로, 로컬 개발은 설정 없이 그대로 된다.
연결 문자열은 로그·화면 어디에도 표시되지 않는다.

### 4. 배포 구성 (3중 백업)

| 구성 | 내용 |
|---|---|
| 주력 | Streamlit Community Cloud → `https://….streamlit.app`. 진입점 `src/app/main.py` |
| 보험 1 | Hugging Face Spaces (같은 저장소·Streamlit 무료) → URL 2개 체제 |
| 보험 2 | 녹화 영상(2분 30초) + **정적 HTML 스냅샷** `data/snapshot/` (생성 완료) — USB + 저장소 이중 보관 |
| QR 체험 | 슬라이드에 URL QR 1장 — 청중이 자기 폰으로 직접 열어 보는 연출 |

발표 노트북 요구사항은 **브라우저 + USB 포트가 전부**다 (설치 0).

> ⚠️ **보험 1의 한계**: 데이터가 Supabase 한 곳에 있으므로 주력(Streamlit Cloud)과
> 보험 1(HF Spaces)이 **같은 단일 장애점을 공유**한다. Supabase가 멈추면 URL 두 개가 다 빈다.
> 이 경우 보험 2(오프라인 스냅샷)로 완주한다 — 그래서 스냅샷이 선택이 아니라 필수다
> ([ADR-0011](doc/adr/0011-데이터는-Supabase에-두고-저장소에는-코드만-둔다.md) 참조).

### 5. 발표일 운영

- **전날**: URL 2개 접속 확인 / USB의 영상·스냅샷 열림 확인 / **Supabase 프로젝트가 깨어 있는지 확인**
  (무료 티어는 미사용 시 프로젝트를 재운다 — 데이터가 클라우드에 있으므로 이 확인이 필수다)
- **30분 전**: URL 2개를 탭으로 열어 콜드스타트 깨우고 유지
- **직전**: 주력 탭에서 브리핑·관리자 버튼 각 1회 워밍업, 보험1 탭 대기
- **발표 중**: 주력=클라우드 URL, QR 슬라이드로 청중 폰 접속 유도
