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
    # docling을 가짜로 대체: doc.md만 쓰고 tables 없음.
    class FakeDoc:
        tables = []
        def save_as_markdown(self, path, image_mode=None):
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


def test_convert_writes_table_csv_and_counts_n_tables(tmp_path, monkeypatch):
    import pandas as pd

    class FakeTable:
        def export_to_dataframe(self, doc=None):
            return pd.DataFrame({"a": [1, 2], "b": [3, 4]})

    class FakeDoc:
        tables = [FakeTable()]
        def save_as_markdown(self, path, image_mode=None):
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
