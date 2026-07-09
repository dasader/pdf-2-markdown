import hashlib
import shutil
import zipfile
from pathlib import Path

import fitz  # PyMuPDF


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_pdf(head: bytes) -> bool:
    return head[:5] == b"%PDF-"


def page_count(path) -> int:
    with fitz.open(path) as doc:
        return doc.page_count


def opts_hash(include_images: bool, include_tables_csv: bool) -> str:
    key = f"img={int(include_images)};csv={int(include_tables_csv)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _build_converter():
    # 지연 import: 테스트가 torch 없이 돌게 함.
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_ocr = False                       # 텍스트 PDF → OCR 모델 미로딩(~2GB 절감)
    opts.do_table_structure = True
    opts.table_structure_options.mode = TableFormerMode.ACCURATE
    opts.images_scale = 2.0
    opts.generate_picture_images = True
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
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
    doc.save_as_markdown(str(md_path), image_mode=image_mode)

    # save_as_markdown(REFERENCED)는 doc_artifacts/ 폴더에 이미지를 쓴다 → images/로 정규화
    artifacts = out_dir / f"{md_path.stem}_artifacts"
    images_dir = out_dir / "images"
    if include_images and artifacts.exists():
        if images_dir.exists():
            shutil.rmtree(images_dir)
        artifacts.rename(images_dir)
        # md 안의 참조 경로 치환
        md = md_path.read_text(encoding="utf-8").replace(
            f"{md_path.stem}_artifacts/", "images/")
        md_path.write_text(md, encoding="utf-8")

    n_tables = len(getattr(doc, "tables", None) or [])
    tables_dir = out_dir / "tables"
    if include_tables_csv and n_tables:
        tables_dir.mkdir(exist_ok=True)
        for i, table in enumerate(doc.tables, 1):
            df = table.export_to_dataframe(doc=doc)
            df.to_csv(tables_dir / f"table-{i:02d}.csv", index=False)

    # n_images: images/ 에 실제로 쓰인 파일 수(정확). include_images=False면 저장된 파일이
    # 없으므로 0 — 그림 개수 자체를 세려면 doc.pictures가 필요하지만 비용 대비 실익이
    # 낮아 여기서는 "실제 산출물 개수"를 UI 칩에 보여준다는 원칙을 택함.
    n_images = sum(1 for f in images_dir.rglob("*") if f.is_file()) if images_dir.exists() else 0

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
