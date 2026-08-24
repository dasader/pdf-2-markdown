import ctypes
import shutil
import time
import traceback
from pathlib import Path

from app import config, db, convert

_SWEEP_EVERY = 300  # 초
# 워커를 한 번이라도 죽인 문서는 재시도하지 않는다. 저사양 재시도(그림 크롭 off)를
# 두던 자리인데, 실측상 메모리를 전혀 못 줄여(6.1GB → 6.2GB) 워커만 한 번 더 죽었다.
# 메모리 폭발의 주범은 그림 크롭이 아니라 백엔드의 페이지 비트맵 파싱이다.
_MAX_ATTEMPTS = 1

# 잡이 끝나도 torch/docling이 잡았던 임시 텐서가 glibc 아레나에 남아 OS로 반환되지
# 않는다. 누수는 아니고(10잡쯤에서 포화) 재사용도 되지만, 그만큼 mem_limit 여유가
# 깎인 채로 다음 잡을 시작한다 — 44p 문서 첫 잡 peak 1.64GB, 정상상태 2.31GB.
# 실측(44p 12회 A/B): 잡간 상주 1917MB → 1116MB(-42%), peak 2312MB → 1869MB(-19%),
# 잡 시간은 28.2s → 27.5s로 차이 없다. 무료로 되찾는 여유다.
# glibc가 아닌 libc에서는 심볼이 없으므로 워커를 죽이지 않고 조용히 건너뛴다.
try:
    _malloc_trim = ctypes.CDLL("libc.so.6").malloc_trim
except (OSError, AttributeError):  # pragma: no cover - 이 이미지는 debian/glibc 고정
    _malloc_trim = None


def process_one(conn) -> bool:
    job = db.claim_next_queued(conn)
    if job is None:
        return False
    attempts = job["attempts"] or 0
    if attempts >= _MAX_ATTEMPTS:
        # 이 문서는 이미 워커를 죽였다(OOM). 다시 시도해도 같은 결과다.
        db.finish_job(conn, job["id"], status="failed",
                      error="문서가 너무 무거워 변환하지 못했습니다. 페이지에 이미지·도형이 "
                            "과도하게 많으면 메모리 한도를 넘길 수 있습니다.")
        return True
    sha, opts = job["sha256"], job["opts_hash"]
    pdf_path = config.UPLOADS_DIR / f"{sha}.pdf"
    out_dir = config.RESULTS_DIR / f"{sha}-{opts}"
    # ponytail: opts 4조합 역산, 조합이 늘면 컬럼 추가
    pair = next(((i, c) for i in (True, False) for c in (True, False)
                 if convert.opts_hash(i, c) == opts), None)
    if pair is None:
        # CONVERTER_REV가 올라간 뒤 남아있던 옛 queued 잡. 옵션을 복원할 수 없으므로
        # 조용히 기본값으로 변환해 요청과 다른 결과를 내보내는 대신 실패시킨다.
        db.finish_job(conn, job["id"], status="failed",
                      error="변환기가 업데이트되었습니다. 다시 업로드해 주세요.")
        return True
    include_images, include_csv = pair
    try:
        n_tables, n_images = convert.convert(
            pdf_path, out_dir, include_images=include_images,
            include_tables_csv=include_csv)
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
        if worked and _malloc_trim is not None:
            _malloc_trim(0)
        now = time.time()
        if now - last_sweep > _SWEEP_EVERY:
            sweep(conn)
            last_sweep = now
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    run()
