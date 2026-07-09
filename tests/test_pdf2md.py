import time
import pytest
from app import config, db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    config.ensure_dirs()
    db.init_db()
    c = db.connect()
    yield c
    c.close()


def _job(conn, jid, session="s1", sha="abc", opts="o1", status="queued", result_dir=None):
    db.create_job(conn, id=jid, session_id=session, filename="f.pdf", sha256=sha,
                  opts_hash=opts, status=status, page_total=3, result_dir=result_dir)


def test_create_and_get_job(conn):
    _job(conn, "j1")
    row = db.get_job(conn, "j1")
    assert row["status"] == "queued"
    assert row["filename"] == "f.pdf"


def test_list_jobs_session_isolation(conn):
    _job(conn, "j1", session="s1")
    _job(conn, "j2", session="s2")
    mine = db.list_jobs(conn, "s1", admin=False)
    assert [r["id"] for r in mine] == ["j1"]
    all_jobs = db.list_jobs(conn, "s1", admin=True)
    assert {r["id"] for r in all_jobs} == {"j1", "j2"}


def test_find_cached_returns_done_only(conn):
    _job(conn, "j1", sha="X", opts="O", status="queued")
    assert db.find_cached(conn, "X", "O") is None
    _job(conn, "j2", sha="X", opts="O", status="done", result_dir="/r/X-O")
    hit = db.find_cached(conn, "X", "O")
    assert hit["result_dir"] == "/r/X-O"


def test_claim_next_queued_atomic(conn):
    _job(conn, "j1", status="queued")
    claimed = db.claim_next_queued(conn)
    assert claimed["id"] == "j1"
    assert db.get_job(conn, "j1")["status"] == "running"
    assert db.claim_next_queued(conn) is None  # 더 없음


def test_count_queued(conn):
    _job(conn, "j1", session="s1", status="queued")
    _job(conn, "j2", session="s1", status="done")
    assert db.count_queued(conn, "s1") == 1


def test_finish_job_sets_n_tables_n_images(conn):
    _job(conn, "j1", status="running")
    db.finish_job(conn, "j1", status="done", n_tables=3, n_images=5)
    row = db.get_job(conn, "j1")
    assert row["n_tables"] == 3
    assert row["n_images"] == 5
    # calling again without n_tables/n_images leaves them unchanged
    db.finish_job(conn, "j1", status="done")
    row = db.get_job(conn, "j1")
    assert row["n_tables"] == 3
    assert row["n_images"] == 5


def test_active_before_counts_queued_and_running_only(conn):
    db.create_job(conn, id="j1", session_id="s1", filename="f.pdf", sha256="a",
                   opts_hash="o", status="queued", page_total=1, result_dir=None)
    conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (100.0, "j1"))
    db.create_job(conn, id="j2", session_id="s1", filename="f.pdf", sha256="a",
                  opts_hash="o", status="running", page_total=1, result_dir=None)
    conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (200.0, "j2"))
    db.create_job(conn, id="j3", session_id="s1", filename="f.pdf", sha256="a",
                  opts_hash="o", status="done", page_total=1, result_dir=None)
    conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (300.0, "j3"))
    conn.commit()
    # before 250: j1(queued, 100) and j2(running, 200) count; j3 is done -> excluded
    assert db.active_before(conn, 250.0) == 2
    # before 150: only j1
    assert db.active_before(conn, 150.0) == 1
    # before 100: nothing strictly before
    assert db.active_before(conn, 100.0) == 0


def test_worker_busy(conn):
    _job(conn, "j1", status="queued")
    assert db.worker_busy(conn) is False
    db.claim_next_queued(conn)
    assert db.worker_busy(conn) is True


def test_requeue_running_resets_orphans(conn):
    _job(conn, "j1", status="queued")
    db.claim_next_queued(conn)
    assert db.get_job(conn, "j1")["status"] == "running"
    assert db.requeue_running(conn) == 1
    row = db.get_job(conn, "j1")
    assert row["status"] == "queued"
    assert row["started_at"] is None


from pathlib import Path
from app import convert

FIX = Path(__file__).parent / "fixtures" / "sample.pdf"


def test_is_pdf_magic_bytes():
    assert convert.is_pdf(b"%PDF-1.7 ...")
    assert not convert.is_pdf(b"PK\x03\x04zip")


def test_page_count(_=None):
    assert convert.page_count(FIX) == 1


def test_opts_hash_stable_and_distinct():
    a = convert.opts_hash(True, True)
    assert a == convert.opts_hash(True, True)
    assert a != convert.opts_hash(False, True)
    assert a != convert.opts_hash(True, False)


def test_convert_packages_zip(tmp_path, monkeypatch):
    # docling을 가짜로 대체: doc.md만 쓰고 tables/pictures 없음.
    class FakeDoc:
        tables = []
        pictures = []
        def save_as_markdown(self, path, artifacts_dir=None, image_mode=None):
            Path(path).write_text("# hi\n")
    class FakeResult:
        document = FakeDoc()
    class FakeConverter:
        def __init__(self, *a, **k): pass
        def convert(self, p): return FakeResult()

    monkeypatch.setattr(convert, "_build_converter", lambda: FakeConverter())
    out = tmp_path / "X-O"
    result = convert.convert(FIX, out, include_images=True, include_tables_csv=True)
    assert (out / "doc.md").exists()
    assert (out / "result.zip").exists()
    import zipfile
    names = zipfile.ZipFile(out / "result.zip").namelist()
    assert "doc.md" in names
    assert result == (0, 0)


def test_convert_counts_n_images_regardless_of_include_images(tmp_path, monkeypatch):
    # n_images는 include_images=False여도 문서의 실제 그림 개수를 반영해야 함(n_tables와 대칭).
    class FakeDoc:
        tables = []
        pictures = [object(), object()]
        def save_as_markdown(self, path, artifacts_dir=None, image_mode=None):
            Path(path).write_text("# hi\n")
    class FakeResult:
        document = FakeDoc()
    class FakeConverter:
        def __init__(self, *a, **k): pass
        def convert(self, p): return FakeResult()

    monkeypatch.setattr(convert, "_build_converter", lambda: FakeConverter())
    out = tmp_path / "Z-O"
    n_tables, n_images = convert.convert(
        FIX, out, include_images=False, include_tables_csv=False)
    assert n_images == 2
    assert n_tables == 0
    # include_images=False -> no images/ dir written
    assert not (out / "images").exists()


def test_convert_writes_table_csv_and_counts_n_tables(tmp_path, monkeypatch):
    import pandas as pd

    class FakeTable:
        def export_to_dataframe(self, doc=None):
            return pd.DataFrame({"a": [1, 2], "b": [3, 4]})

    class FakeDoc:
        tables = [FakeTable()]
        pictures = []
        def save_as_markdown(self, path, artifacts_dir=None, image_mode=None):
            Path(path).write_text("# hi\n")
    class FakeResult:
        document = FakeDoc()
    class FakeConverter:
        def __init__(self, *a, **k): pass
        def convert(self, p): return FakeResult()

    monkeypatch.setattr(convert, "_build_converter", lambda: FakeConverter())
    out = tmp_path / "Y-O"
    n_tables, n_images = convert.convert(
        FIX, out, include_images=False, include_tables_csv=True)
    assert n_tables == 1
    assert n_images == 0
    assert (out / "tables" / "table-01.csv").exists()
    import zipfile
    names = zipfile.ZipFile(out / "result.zip").namelist()
    assert "tables/table-01.csv" in names


def test_convert_image_refs_are_relative_real_docling_core(tmp_path, monkeypatch):
    # 회귀 테스트: 실제 docling_core DoclingDocument로 이미지 1개를 만들어
    # save_as_markdown(REFERENCED)의 참조 경로가 절대경로가 아니라
    # "images/..." 상대경로인지 확인한다 (Finding 1의 재발 방지).
    docling_core = pytest.importorskip("docling_core")
    PIL = pytest.importorskip("PIL")
    from PIL import Image
    from docling_core.types.doc import DoclingDocument

    real_doc = DoclingDocument(name="test")
    img = Image.new("RGB", (4, 4), color="red")
    from docling_core.types.doc.document import ImageRef
    real_doc.add_picture(image=ImageRef.from_pil(img, dpi=72))

    class FakeResult:
        document = real_doc
    class FakeConverter:
        def __init__(self, *a, **k): pass
        def convert(self, p): return FakeResult()

    monkeypatch.setattr(convert, "_build_converter", lambda: FakeConverter())
    out = tmp_path / "W-O"
    n_tables, n_images = convert.convert(
        FIX, out, include_images=True, include_tables_csv=False)

    md = (out / "doc.md").read_text(encoding="utf-8")
    assert "images/" in md
    assert "/tmp" not in md
    assert str(out) not in md  # no absolute path leaked into the markdown
    assert not md.count("](/")  # no reference starts with a leading slash
    assert (out / "images").is_dir()
    assert any((out / "images").iterdir())
    assert n_images == 1


from app import worker


def test_process_one_success(conn, tmp_path, monkeypatch):
    _job(conn, "j1", sha="Y", opts="O", status="queued")
    # 업로드 파일 존재해야 함
    (config.UPLOADS_DIR / "Y.pdf").write_bytes(b"%PDF-1.7")
    called = {}
    def fake_convert(pdf, out, **kw):
        called["out"] = str(out); Path(out).mkdir(parents=True, exist_ok=True)
        return (3, 1)
    monkeypatch.setattr(worker.convert, "convert", fake_convert)
    monkeypatch.setattr(worker.convert, "opts_hash", lambda *a: "O")

    assert worker.process_one(conn) is True
    row = db.get_job(conn, "j1")
    assert row["status"] == "done"
    assert row["result_dir"] == called["out"]
    assert row["n_tables"] == 3
    assert row["n_images"] == 1


def test_process_one_failure_records_error(conn, monkeypatch):
    _job(conn, "j1", sha="Z", opts="O", status="queued")
    (config.UPLOADS_DIR / "Z.pdf").write_bytes(b"%PDF-1.7")
    def boom(*a, **k): raise RuntimeError("docling exploded")
    monkeypatch.setattr(worker.convert, "convert", boom)
    monkeypatch.setattr(worker.convert, "opts_hash", lambda *a: "O")

    assert worker.process_one(conn) is True
    row = db.get_job(conn, "j1")
    assert row["status"] == "failed"
    assert "docling exploded" in row["error"]


def test_process_one_empty(conn):
    assert worker.process_one(conn) is False


def test_sweep_deletes_expired_and_orphans(conn, monkeypatch):
    # 오래된 잡 + 그 파일
    (config.UPLOADS_DIR / "OLD.pdf").write_bytes(b"%PDF")
    old_res = config.RESULTS_DIR / "OLD-O"; old_res.mkdir()
    (old_res / "doc.md").write_text("x")
    _job(conn, "old", sha="OLD", opts="O", status="done", result_dir=str(old_res))
    # created_at을 과거로
    conn.execute("UPDATE jobs SET created_at=? WHERE id='old'",
                 (db.now() - config.RETENTION_SEC - 10,)); conn.commit()

    worker.sweep(conn)
    assert db.get_job(conn, "old") is None
    assert not (config.UPLOADS_DIR / "OLD.pdf").exists()
    assert not old_res.exists()


def test_sweep_preserves_shared_files_of_live_cachehit_job(conn):
    # 캐시 히트로 result_dir/sha256을 공유하는 최신 job이 있으면,
    # 만료된 옛 job이 지워져도 참조 카운팅으로 공유 파일은 살아남아야 한다.
    (config.UPLOADS_DIR / "SHARED.pdf").write_bytes(b"%PDF")
    shared_res = config.RESULTS_DIR / "SHARED-O"; shared_res.mkdir()
    (shared_res / "doc.md").write_text("x")

    _job(conn, "old", sha="SHARED", opts="O", status="done", result_dir=str(shared_res))
    conn.execute("UPDATE jobs SET created_at=? WHERE id='old'",
                 (db.now() - config.RETENTION_SEC - 10,)); conn.commit()

    # 캐시 히트: 같은 sha256/opts_hash/result_dir, 최근 created_at(기본값)
    _job(conn, "live", sha="SHARED", opts="O", status="done", result_dir=str(shared_res))

    worker.sweep(conn)
    assert db.get_job(conn, "old") is None
    assert db.get_job(conn, "live") is not None
    assert (config.UPLOADS_DIR / "SHARED.pdf").exists()
    assert shared_res.exists()


# --- Task 4: FastAPI 웹 ---

import zipfile
from io import BytesIO
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(config, "ADMIN_KEY", "secret")
    config.ensure_dirs(); db.init_db()
    from app import web
    return TestClient(web.app)


def _pdf_bytes():
    return FIX.read_bytes()


def test_index_sets_session_cookie(client):
    r1 = client.get("/")
    assert r1.status_code == 200
    sid1 = r1.cookies.get("sid")
    assert sid1

    r2 = client.get("/")  # client jar already carries sid1 from r1
    assert r2.status_code == 200
    # cookie is reused, not rotated (no new Set-Cookie changing the value)
    sid2 = r2.cookies.get("sid", sid1)
    assert sid2 == sid1


def test_upload_creates_queued_job(client):
    r = client.post("/api/jobs",
                    files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    assert r.status_code == 200
    jobs = r.json()
    assert len(jobs) == 1 and jobs[0]["status"] == "queued"
    assert "sid" in r.cookies


def test_upload_rejects_non_pdf(client):
    r = client.post("/api/jobs",
                    files={"files": ("x.pdf", b"PK\x03\x04not a pdf", "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    jobs = r.json()
    assert jobs[0]["status"] == "failed"
    assert "PDF" in (jobs[0]["error"] or "")


def test_cache_hit_second_upload_skips(client):
    f = {"files": ("a.pdf", _pdf_bytes(), "application/pdf")}
    d = {"include_images": "true", "include_tables_csv": "true"}
    r1 = client.post("/api/jobs", files=f, data=d)
    # 첫 잡을 done으로 만들고 결과 디렉토리 생성
    conn = db.connect()
    j1 = r1.json()[0]
    res_dir = config.RESULTS_DIR / f"{j1['sha256']}-{j1['opts_hash']}"
    res_dir.mkdir(parents=True); (res_dir / "doc.md").write_text("# x")
    db.finish_job(conn, j1["id"], status="done", result_dir=str(res_dir),
                  n_tables=2, n_images=4)
    conn.close()
    r2 = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")}, data=d)
    j2 = r2.json()[0]
    assert j2["status"] == "done"  # 캐시 히트 → 즉시 done
    # 캐시 히트 잡은 원본의 표/이미지 카운트를 복사해야 함
    assert j2["n_tables"] == 2
    assert j2["n_images"] == 4


def test_session_isolation_download_404(client):
    r1 = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                     data={"include_images": "true", "include_tables_csv": "true"})
    jid = r1.json()[0]["id"]
    other = TestClient(client.app)  # 새 세션
    assert other.get(f"/api/jobs/{jid}/download").status_code == 404


def test_admin_key_sees_all(client):
    client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                data={"include_images": "true", "include_tables_csv": "true"})
    other = TestClient(client.app)
    r = other.get("/api/jobs", headers={"X-Admin-Key": "secret"})
    assert len(r.json()["jobs"]) >= 1


def test_jobs_response_has_busy_and_ahead(client):
    r1 = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                     data={"include_images": "true", "include_tables_csv": "true"})
    conn = db.connect()
    claimed = db.claim_next_queued(conn)  # 다른 워커가 이미 하나를 실행 중이라고 가정
    assert claimed is not None
    conn.close()

    other = TestClient(client.app)  # 다른 세션
    r2 = other.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    jid2 = r2.json()[0]["id"]

    r = other.get("/api/jobs")
    body = r.json()
    assert body["busy"] is True
    mine = [j for j in body["jobs"] if j["id"] == jid2]
    assert mine and mine[0]["status"] == "queued"
    assert mine[0]["ahead"] >= 1


def test_download_all_zips_done_jobs(client):
    r1 = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                     data={"include_images": "true", "include_tables_csv": "true"})
    j1 = r1.json()[0]
    conn = db.connect()
    res_dir = config.RESULTS_DIR / f"{j1['sha256']}-{j1['opts_hash']}"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "doc.md").write_text("# hello")
    with zipfile.ZipFile(res_dir / "result.zip", "w") as z:
        z.writestr("doc.md", "# hello")
    db.finish_job(conn, j1["id"], status="done", result_dir=str(res_dir),
                  n_tables=0, n_images=0)
    conn.close()

    r = client.get("/api/download-all")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(BytesIO(r.content))
    assert any(n.endswith("doc.md") for n in zf.namelist())

    fresh = TestClient(client.app)  # 결과 없는 새 세션
    assert fresh.get("/api/download-all").status_code == 404


def test_download_all_rejects_dotdot_filename(client):
    # filename=".." must not let a zip entry escape its folder (zip-slip).
    conn = db.connect()
    res_dir = config.RESULTS_DIR / "dotdot-O"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "doc.md").write_text("# hello")
    db.create_job(conn, id="jdd", session_id="s-dotdot", filename="..", sha256="dd",
                  opts_hash="O", status="queued", page_total=1)
    db.finish_job(conn, "jdd", status="done", result_dir=str(res_dir))
    conn.close()

    client.cookies.set("sid", "s-dotdot")
    r = client.get("/api/download-all")
    assert r.status_code == 200
    zf = zipfile.ZipFile(BytesIO(r.content))
    for n in zf.namelist():
        assert not n.startswith("../")
        assert ".." not in Path(n).parts
        assert not Path(n).is_absolute()


def test_upload_rejects_oversize(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_BYTES", 3)
    r = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    job = r.json()[0]
    assert job["status"] == "failed"
    assert "100MB" in job["error"]


def test_upload_rejects_too_many_pages(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_PAGES", 0)
    r = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")},
                    data={"include_images": "true", "include_tables_csv": "true"})
    job = r.json()[0]
    assert job["status"] == "failed"
    assert "500페이지" in job["error"]


def test_upload_rejects_over_queue_cap(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_QUEUED_PER_SESSION", 1)
    d = {"include_images": "true", "include_tables_csv": "true"}
    r1 = client.post("/api/jobs", files={"files": ("a.pdf", _pdf_bytes(), "application/pdf")}, data=d)
    assert r1.json()[0]["status"] == "queued"
    r2 = client.post("/api/jobs", files={"files": ("b.pdf", _pdf_bytes(), "application/pdf")}, data=d)
    job2 = r2.json()[0]
    assert job2["status"] == "failed"
    assert "대기 잡이 너무 많습니다" in job2["error"]


def test_web_self_initializes_storage_without_worker(tmp_path, monkeypatch):
    # worker가 아직 ensure_dirs()/init_db()를 실행하지 않은 상황을 재현: 이 테스트는
    # (client 픽스처와 달리) db.init_db()를 직접 호출하지 않는다. web이 자기 스토리지를
    # 스스로 초기화하지 못하면 DB 파일이 없어 /api/jobs가 500을 낸다.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    assert not (tmp_path / "app.db").exists()

    from app import web
    with TestClient(web.app) as c:  # lifespan 실행 -> ensure_dirs()+init_db()
        assert config.DB_PATH.exists()
        r = c.get("/api/jobs")
        assert r.status_code == 200
        assert r.json() == {"jobs": [], "busy": False}
