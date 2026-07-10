import shutil
import time
import traceback
from pathlib import Path

from app import config, db, convert

_SWEEP_EVERY = 300  # 초
_MAX_ATTEMPTS = 2   # attempts가 이만큼이면 (매번 워커를 죽인 문서로 보고) 실패 처리


def process_one(conn) -> bool:
    job = db.claim_next_queued(conn)
    if job is None:
        return False
    attempts = job["attempts"] or 0
    if attempts >= _MAX_ATTEMPTS:
        # 이 문서는 이미 두 번(1차 정상 + 2차 저사양) 워커를 죽였다. 무한 재시도 대신 실패.
        db.finish_job(conn, job["id"], status="failed",
                      error="변환에 반복 실패했습니다. 문서가 너무 크거나 복잡해 "
                            "처리하지 못했습니다(메모리 한도).")
        return True
    sha, opts = job["sha256"], job["opts_hash"]
    pdf_path = config.UPLOADS_DIR / f"{sha}.pdf"
    out_dir = config.RESULTS_DIR / f"{sha}-{opts}"
    # ponytail: opts 4조합 역산, 조합이 늘면 컬럼 추가
    include_images, include_csv = next(
        ((i, c) for i in (True, False) for c in (True, False)
         if convert.opts_hash(i, c) == opts), (False, False))
    # 1차(attempts 0)는 정상, 재시도(attempts>=1)는 저사양 모드 — OOM으로 죽었던
    # 문서라도 그림 추출을 꺼 텍스트·표만이라도 메모리 안에 통과시킨다.
    low_mem = attempts >= 1
    try:
        n_tables, n_images = convert.convert(
            pdf_path, out_dir, include_images=include_images,
            include_tables_csv=include_csv, low_mem=low_mem)
        db.finish_job(conn, job["id"], status="done", result_dir=str(out_dir),
                       n_tables=n_tables, n_images=n_images)
    except Exception:
        db.finish_job(conn, job["id"], status="failed",
                      error=traceback.format_exc(limit=3))
    return True


def sweep(conn) -> None:
    db.delete_expired(conn)
    # 고아 파일 정리: 참조되지 않는 upload/result 삭제
    keep_shas = db.referenced_shas(conn)
    for f in config.UPLOADS_DIR.glob("*.pdf"):
        if f.stem not in keep_shas:
            f.unlink(missing_ok=True)
    keep_dirs = {Path(d).name for d in db.referenced_result_dirs(conn)}
    for d in config.RESULTS_DIR.iterdir():
        if d.is_dir() and d.name not in keep_dirs:
            shutil.rmtree(d, ignore_errors=True)


def run() -> None:
    config.ensure_dirs()
    db.init_db()
    conn = db.connect()
    db.requeue_running(conn)  # 이전 실행에서 죽은 running 잡 복구 (워커는 하나뿐)
    last_sweep = 0.0
    while True:
        worked = process_one(conn)
        now = time.time()
        if now - last_sweep > _SWEEP_EVERY:
            sweep(conn); last_sweep = now
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    run()
