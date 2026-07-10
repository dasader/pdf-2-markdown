import hashlib
import html
import re
import zipfile
from pathlib import Path

import pypdfium2  # docling이 이미 의존하는 PDF 백엔드


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_pdf(head: bytes) -> bool:
    return head[:5] == b"%PDF-"


def page_count(path) -> int:
    doc = pypdfium2.PdfDocument(path)
    try:
        return len(doc)
    finally:
        doc.close()


# 변환 결과물이 달라지는 변경을 하면 올린다. opts_hash에 섞이므로 find_cached가 옛
# 결과를 더는 찾지 못해 캐시가 자연히 무효화된다(수동 삭제 불필요).
#   rev 2: docling 기본 백엔드로 복귀(pypdfium 백엔드가 한글 음절을 중복 삽입) +
#          마크다운 HTML 언이스케이프
#   rev 3: generate_picture_images 복구 — rev 2 캐시에는 그림이 없다
#   rev 4: 표 CSV를 utf-8-sig로 저장(Excel 한글 깨짐) + 공문서 불릿 기호를 들여쓰기로
CONVERTER_REV = 4


def opts_hash(include_images: bool, include_tables_csv: bool) -> str:
    key = f"rev={CONVERTER_REV};img={int(include_images)};csv={int(include_tables_csv)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# 공문서 불릿 기호 → 목록 깊이. docling은 이 기호를 본문 글자로 남기므로 "- ㅇ 내용"
# 처럼 불릿이 겹쳐 보인다. 기호를 지우고 그 계층을 들여쓰기로 옮긴다.
_BULLET_DEPTH = {"□": 0, "ㅁ": 0, "■": 0,
                 "ㅇ": 1, "○": 1, "◦": 1, "●": 1,
                 "▪": 2, "-": 2}
_BULLET_RE = re.compile(rf"^- ([{''.join(_BULLET_DEPTH)}])\s*(.*)$")


def _fix_bullets(md: str) -> str:
    out = []
    for line in md.split("\n"):
        m = _BULLET_RE.match(line)          # 표 행은 '|'로 시작해 매칭되지 않는다
        if m:
            text = m.group(2).strip()
            if not text:
                continue                    # 기호만 있고 내용이 없는 줄 — 본문은 다음 줄에 온다
            line = "  " * _BULLET_DEPTH[m.group(1)] + "- " + text
        out.append(line)
    return "\n".join(out)


def _build_converter():
    # 지연 import: 테스트가 torch 없이 돌게 함.
    from docling.datamodel.backend_options import PdfBackendOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.datamodel.settings import settings
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # 백엔드는 docling 기본값(DoclingParseDocumentBackend)을 쓴다. 한때 더 가벼운
    # PyPdfiumDocumentBackend로 바꿨었으나, 그 백엔드는 텍스트 셀 추출까지 pdfium의
    # word 단위 분할에 맡겨 한글처럼 글자 bbox가 겹치는 조판에서 어절 끝 음절을 다음
    # 단어에 다시 붙인다("저물고," → "저물고, 고,"). 본문·표·CSV가 모두 오염됐다.
    #
    # 메모리(3GB 워커): do_ocr=False가 가장 큰 레버(~2GB 절감). page_batch_size=1은
    # 여러 페이지를 동시에 들지 않게 하지만, 한 페이지가 통째로 무거우면 못 막는다.
    # ponytail: 이미지 객체가 수십만 개인 병리적 페이지(실측: 27p 문서의 한 페이지에
    # 609,831개)는 백엔드의 비트맵 파싱만으로 4GB를 써 3GB 안에 못 들어온다. 그런
    # 문서는 worker가 재시도 없이 실패시킨다(_MAX_ATTEMPTS=1). 살려야 한다면 워커
    # mem_limit을 7GB 이상으로 올려야 한다 — 실측 peak 6.1GB.
    settings.perf.page_batch_size = 1

    opts = PdfPipelineOptions()
    opts.do_ocr = False                       # 텍스트 PDF → OCR 모델 미로딩(~2GB 절감)
    opts.do_table_structure = True
    opts.table_structure_options.mode = TableFormerMode.ACCURATE
    opts.images_scale = 1.25
    opts.generate_picture_images = True       # docling 기본값이 False — 끄면 그림이 통째로 누락

    # enforce_same_font=False: 기본값(True)은 폰트가 바뀌는 자리에서 텍스트 셀을 쪼갠다.
    # 공문서는 낫표·괄호를 본문과 다른 폰트로 찍는 일이 흔해, "｢국가전략기술 선정(안)｣을
    # 별지와 같이"가 본문과 "｢ ( ) ｣" 두 줄로 갈렸다. 실측: 커버리지 72.0%→78.0%,
    # 60.7%→61.7%, 표 개수·음절 중복 변화 없음.
    backend_options = PdfBackendOptions(enforce_same_font=False)
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(
            pipeline_options=opts, backend_options=backend_options)}
    )


def convert(pdf_path, out_dir, *, include_images: bool, include_tables_csv: bool):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "doc.md"

    result = _build_converter().convert(str(pdf_path))
    doc = result.document

    try:
        # docling_core is a light, torch-free dependency of docling itself;
        # optional here so unit tests (fake converter, no docling installed) still run.
        from docling_core.types.doc import ImageRefMode
        image_mode = ImageRefMode.REFERENCED if include_images else ImageRefMode.PLACEHOLDER
    except ImportError:
        image_mode = "referenced" if include_images else "placeholder"
    # artifacts_dir="images"로 직접 지정 → doc.md에 상대경로(images/...)가 그대로 기록됨
    # (폴더 rename + 텍스트 치환은 절대경로가 남는 버그가 있어 제거).
    doc.save_as_markdown(str(md_path), artifacts_dir=Path("images"), image_mode=image_mode)
    # docling이 본문을 HTML 이스케이프한 채 마크다운에 내보낸다("R&amp;D"). 되돌린다.
    md = html.unescape(md_path.read_text(encoding="utf-8"))
    md_path.write_text(_fix_bullets(md), encoding="utf-8")

    n_tables = len(getattr(doc, "tables", None) or [])
    tables_dir = out_dir / "tables"
    if include_tables_csv and n_tables:
        tables_dir.mkdir(exist_ok=True)
        for i, table in enumerate(doc.tables, 1):
            df = table.export_to_dataframe(doc=doc)
            # utf-8-sig(BOM): Excel은 BOM이 없으면 CSV를 시스템 인코딩(한국어 Windows는
            # CP949)으로 읽어 한글이 깨진다. BOM 3바이트가 UTF-8임을 알려준다.
            df.to_csv(tables_dir / f"table-{i:02d}.csv", index=False, encoding="utf-8-sig")

    # n_images: 문서의 실제 그림 개수(옵션과 무관하게 정확) — n_tables와 대칭.
    n_images = len(getattr(doc, "pictures", None) or [])

    # ZIP 패키징
    zip_path = out_dir / "result.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(md_path, "doc.md")
        for sub in ("images", "tables"):
            d = out_dir / sub
            if d.exists():
                for f in sorted(d.rglob("*")):
                    if f.is_file():
                        z.write(f, str(f.relative_to(out_dir)))

    return n_tables, n_images
