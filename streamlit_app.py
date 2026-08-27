"""Streamlit Community Cloud 진입점 (명세 15장).

`streamlit run` 은 **실행하는 스크립트가 있는 폴더**를 import 경로 맨 앞에 넣는다.
화면 본체는 `src/app/main.py` 라서, 그 파일을 직접 실행하면 경로에 들어가는 것은
`src/app/` 뿐이고 저장소 루트는 빠진다. 그러면 본체 첫 줄의
`from src.common.config import ...` 가 `ModuleNotFoundError: No module named 'src'` 로 죽는다.

이 파일은 저장소 루트에 있으므로, 같은 규칙이 이번에는 **루트를 경로에 넣어 준다.**
`streamlit_app.py` 는 Streamlit Cloud가 기본으로 찾는 이름이기도 해서
배포 화면에서 경로를 따로 적을 필요가 없다.

로컬 실행은 지금까지처럼 `streamlit run src/app/main.py` 도 그대로 된다
(작업 디렉토리가 루트라 경로가 이미 잡힌다).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 경로를 잡은 **뒤에** 화면 본체를 불러온다. 순서가 뒤집히면 위 설명대로 죽는다.
from src.app.main import main  # noqa: E402

main()
