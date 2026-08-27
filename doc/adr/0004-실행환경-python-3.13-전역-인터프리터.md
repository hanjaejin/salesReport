# ADR-0004. 실행 환경은 Python 3.13 전역 인터프리터를 기준으로 한다

- 상태: 채택
- 일자: 2026-08-27
- 관련 마일스톤: M0 / M1

## 배경

명세 3장은 Python 3.11+를 요구하고, `doc/CLAUDE.md`의 검증 명령은 `python -m venv .venv` 후
`pip install -r requirements.txt`를 전제한다. 그러나 현재 개발 머신에는 필요한 라이브러리가
전역에 이미 설치돼 있다.

| 항목 | 실측 |
|---|---|
| Python | 3.13.9 |
| pandas | 2.3.3 |
| SQLAlchemy | 2.0.43 |
| streamlit | 1.51.0 |
| pytest | 8.4.2 |
| openpyxl | 3.1.5 |

플랫폼은 Windows 11 / PowerShell이며, `doc/CLAUDE.md`의 검증 명령은 bash 문법(`source .venv/bin/activate`)으로
적혀 있어 그대로는 동작하지 않는다.

## 결정

1. **개발·검증은 전역 인터프리터(Python 3.13.9)로 수행한다.** venv를 새로 만들지 않는다.
2. **`requirements.txt`는 작성해 커밋한다.** 클라우드 배포(명세 15장)와 타 개발자의 재현에 필요하다.
   버전은 하한만 고정(`>=`)해 Streamlit Community Cloud의 해석 여지를 남긴다.
3. **README의 실행 절차는 Windows/PowerShell과 POSIX를 나란히 적는다.** `doc/CLAUDE.md`의
   bash 한 줄만으로는 이 머신에서 재현되지 않기 때문이다.
4. 코드는 **3.11에서도 동작하는 문법만** 쓴다. 3.12+ 전용 문법(PEP 695 제네릭 등)을 쓰지 않는다.

## 근거

- 명세가 요구한 것은 "3.11 이상"이고 3.13.9는 이를 만족한다. venv는 명세 요구가 아니라
  `doc/CLAUDE.md`가 예시로 든 절차이며, 요구의 실질(의존성이 갖춰진 3.11+ 환경)은 이미 충족돼 있다.
- 하루 안에 완성이 목표인 데모에서 venv 구축·재설치에 쓰는 시간은 회수되지 않는다.
- 다만 `requirements.txt` 없이는 **Streamlit Community Cloud가 빌드할 수 없다** — 배포는 명세 15장의
  필수 산출물이므로 파일 자체는 반드시 만든다.

## 결과

- 긍정: 즉시 착수 가능. 배포 경로는 `requirements.txt`로 확보.
- 부정: 전역 환경의 다른 패키지 버전 변화가 이 프로젝트에 새어 들어올 수 있다.
  → `requirements.txt`에 하한을 명시하고, M5 완료 시 `pip freeze` 실측값을 README에 부기한다.
- 부정: 3.11 호환은 **테스트로 검증하지 않는다**(3.11 인터프리터가 없다). 문법 규율로만 지킨다.

## 명세와의 관계

명세 3장 "Python 3.11+"을 만족한다. `doc/CLAUDE.md`의 venv 절차는 이 ADR로 대체하되,
같은 파일이 요구한 `requirements.txt`·README 실행법은 그대로 이행한다.
