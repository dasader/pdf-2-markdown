"""변환 성능 실측. 컨테이너 안에서 돈다 — `make bench PDF=경로` 로 부른다.

기본: 같은 문서를 N회 처리하며 시간·RSS·peak 추이를 본다. 워커는 한 프로세스에서
잡을 계속 처리하므로 1회차가 아니라 '정상상태' 값이 실제 운영값이다(실측: 44p 문서
첫 잡 peak 1.64GB → 정상상태 2.31GB, 10잡쯤에서 포화).

--sweep: queue_max_size / batch_size 조합을 훑는다. 2/1/1(현행)이 시간·메모리 모두
최적이라는 게 실측 결론이므로, 재확인이 필요할 때만 쓴다.
"""
import gc
import hashlib
import sys
import time

sys.path.insert(0, "/srv")


def _mem(key):
    for line in open("/proc/self/status"):
        if line.startswith(key):
            return int(line.split()[1]) / 1024  # MB


def _run(conv, pdf):
    t = time.time()
    doc = conv.convert(pdf).document
    md = doc.export_to_markdown()
    return time.time() - t, md, len(getattr(doc, "tables", None) or [])


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # 워커가 잡마다 부르는 바로 그 호출을 그대로 가져다 쓴다 — 따로 바인딩하면
    # 워커 쪽이 바뀌었을 때 벤치가 다른 조건을 재게 된다.
    from app import convert
    from app.worker import _malloc_trim as trim
    pdf = args[0]
    n = int(args[1]) if len(args) > 1 else 3

    if "--sweep" in sys.argv:
        from docling.datamodel.base_models import InputFormat
        print(f"{'queue/layout/table':<20}{'시간':>9}{'peak':>9}  md5")
        for q, b in ((2, 1), (2, 4), (4, 4), (8, 4), (100, 4)):
            c = convert._build_converter(picture_images=False)
            o = c.format_to_options[InputFormat.PDF].pipeline_options
            o.queue_max_size, o.layout_batch_size, o.table_batch_size = q, b, b
            dt, md, _ = _run(c, pdf)
            del c
            gc.collect()
            trim(0)
            print(f"{f'{q}/{b}/{b}':<20}{dt:8.1f}s{_mem('VmHWM:'):8.0f}MB  "
                  f"{hashlib.md5(md.encode()).hexdigest()[:8]}", flush=True)
        return

    print(f"{'회차':<6}{'시간':>9}{'rss':>9}{'peak':>9}{'표':>5}  md5")
    for i in range(1, n + 1):
        c = convert._build_converter(picture_images=False)   # 워커와 동일: 잡마다 새로
        dt, md, n_tab = _run(c, pdf)
        del c
        gc.collect()
        trim(0)
        print(f"{i:<6}{dt:8.1f}s{_mem('VmRSS:'):8.0f}MB{_mem('VmHWM:'):8.0f}MB{n_tab:5d}  "
              f"{hashlib.md5(md.encode()).hexdigest()[:8]}", flush=True)


if __name__ == "__main__":
    main()
