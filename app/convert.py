import hashlib
import html
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
CONVERTER_REV = 2


def opts_hash(include_images: bool, include_tables_csv: bool) -> str:
    key = f"rev={CONVERTER_REV};img={int(include_images)};csv={int(include_tables_csv)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _build_converter(low_mem: bool = False):
    # 지연 import: 테스트가 torch 없이 돌게 함.
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.datamodel.settings import settings
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # 저사양 호스트(3GB 워커) 메모리 최적화 — 실측으로 검증한 레버는 page_batch_size=1
    # (페이지를 1장씩 처리해 피크 메모리를 대폭 낮춘다)과 do_ocr=False다.
    #
    # 백엔드는 docling 기본값(DoclingParseDocumentBackend)을 쓴다. 한때 더 가벼워 보이는
    # PyPdfiumDocumentBackend로 바꿨었으나, 그 백엔드는 텍스트 셀 추출까지 pdfium의
    # word 단위 분할에 맡겨 한글처럼 글자 bbox가 겹치는 조판에서 어절 끝 음절을 다음
    # 단어에 다시 붙인다("저물고," → "저물고, 고,"). 본문·표·CSV가 모두 오염됐다.
    # 실측: 11p 한글 문서 peak RSS 1870MB(pypdfium) vs 1991MB(기본) — 3GB 안에 여유.
    settings.perf.page_batch_size = 1

    opts = PdfPipelineOptions()
    opts.do_ocr = False                       # 텍스트 PDF → OCR 모델 미로딩(~2GB 절감)
    opts.do_table_structure = True
    opts.table_structure_options.mode = TableFormerMode.ACCURATE
    opts.images_scale = 1.25
    # low_mem: 위 최적화로도 부족한 극단적 문서의 재시도. 그림 크롭 생성을 꺼(가장 큰
    # 메모리 절감) 텍스트·표만이라도 통과시킨다.
    opts.generate_picture_images = not low_mem
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


def convert(pdf_path, out_dir, *, include_images: bool, include_tables_csv: bool,
            low_mem: bool = False):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "doc.md"

    result = _build_converter(low_mem=low_mem).convert(str(pdf_path))
    doc = result.document

    # low_mem에선 그림 크롭을 생성하지 않으므로 이미지 참조도 남기지 않는다(텍스트·표만).
    emit_images = include_images and not low_mem
    try:
        # docling_core is a light, torch-free dependency of docling itself;
        # optional here so unit tests (fake converter, no docling installed) still run.
        from docling_core.types.doc import ImageRefMode
        image_mode = ImageRefMode.REFERENCED if emit_images else ImageRefMode.PLACEHOLDER
    except ImportError:
        image_mode = "referenced" if emit_images else "placeholder"
    # artifacts_dir="images"로 직접 지정 → doc.md에 상대경로(images/...)가 그대로 기록됨
    # (폴더 rename + 텍스트 치환은 절대경로가 남는 버그가 있어 제거).
    doc.save_as_markdown(str(md_path), artifacts_dir=Path("images"), image_mode=image_mode)
    # docling이 본문을 HTML 이스케이프한 채 마크다운에 내보낸다("R&amp;D"). 되돌린다.
    md_path.write_text(html.unescape(md_path.read_text(encoding="utf-8")), encoding="utf-8")

    n_tables = len(getattr(doc, "tables", None) or [])
    tables_dir = out_dir / "tables"
    if include_tables_csv and n_tables:
        tables_dir.mkdir(exist_ok=True)
        for i, table in enumerate(doc.tables, 1):
            df = table.export_to_dataframe(doc=doc)
            df.to_csv(tables_dir / f"table-{i:02d}.csv", index=False)

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
