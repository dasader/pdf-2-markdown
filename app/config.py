import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("PDF2MD_DATA", "data"))
DB_PATH = DATA_DIR / "app.db"
UPLOADS_DIR = DATA_DIR / "uploads"
RESULTS_DIR = DATA_DIR / "results"

ADMIN_KEY = os.environ.get("PDF2MD_ADMIN_KEY", "")

MAX_BYTES = 100 * 1024 * 1024
MAX_PAGES = 500
MAX_QUEUED_PER_SESSION = 20
# 표본 페이지의 텍스트가 이보다 적으면 스캔본(이미지) PDF로 본다. OCR을 끈 파이프라인
# 이라 스캔본은 빈 doc.md로 '성공'해버린다. 페이지 번호만 찍힌 스캔본까지 잡되 짧은
# 정상 문서는 통과시키는 선. ponytail: 실측으로 조정하는 손잡이, 자동 판별은 과잉.
MIN_TEXT_CHARS = int(os.environ.get("PDF2MD_MIN_TEXT_CHARS", "10"))
SEC_PER_PAGE = float(os.environ.get("PDF2MD_SEC_PER_PAGE", "1.5"))
RETENTION_SEC = 24 * 3600


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOADS_DIR, RESULTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
