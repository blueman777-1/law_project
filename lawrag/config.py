"""환경 설정. .env 를 읽는다 (커밋되지 않음)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# 법제처 OPEN API 인증. 신청 이메일의 @ 앞부분이며 별도 발급 키가 없다.
LAW_OC = os.getenv("LAW_OC", "")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/lawrag")
EMBED_MODEL = os.getenv("EMBED_MODEL", "minishlab/potion-multilingual-128M")
EMBED_DIM = int(os.getenv("EMBED_DIM", "256"))

# DRF 호출 간격. 짧은 시간 내 과도한 호출 시 이용 제한을 받는다.
DRF_SLEEP = max(0.3, float(os.getenv("DRF_SLEEP", "0.5")))
