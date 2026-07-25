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
#   rev 5: PDF 자간(letter-spacing)으로 음절이 벌어진 텍스트 되붙이기("글 로 벌"→"글로벌")
#          + 구두점 주변 과잉 공백 정리("산 · 학 · 연"→"산·학·연", "( 연 )"→"(연)")
#          + 심볼폰트 불릿 'l'(▪) 정리("- l 내용"→"- 내용")
CONVERTER_REV = 5


def opts_hash(include_images: bool, include_tables_csv: bool) -> str:
    key = f"rev={CONVERTER_REV};img={int(include_images)};csv={int(include_tables_csv)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# 공문서 불릿 기호 → 목록 깊이. docling은 이 기호를 본문 글자로 남기므로 "- ㅇ 내용"
# 처럼 불릿이 겹쳐 보인다. 기호를 지우고 그 계층을 들여쓰기로 옮긴다.
_BULLET_DEPTH = {"□": 0, "ㅁ": 0, "■": 0,
                 "ㅇ": 1, "○": 1, "◦": 1, "●": 1,
                 "▪": 2, "-": 2}
_BULLET_RE = re.compile(rf"^- ([{''.join(_BULLET_DEPTH)}])\s*(.*)$")

# 심볼폰트 불릿(Wingdings 'l'=속 채운 사각 ▪)이 본문 글자 'l'로 추출돼 "- l 내용"으로
# 나온다(공문서 하위 불릿에 흔함). 반드시 뒤 공백/줄끝을 요구해 실제 'l'로 시작하는
# 낱말("- long term ...")을 오검하지 않는다. 부모 없는 최상위라 0단계로 둔다.
_LBULLET_RE = re.compile(r"^- l(?: (.*))?$")


def _fix_bullets(md: str) -> str:
    out = []
    for line in md.split("\n"):
        m = _BULLET_RE.match(line)          # 표 행은 '|'로 시작해 매칭되지 않는다
        if m:
            text = m.group(2).strip()
            if not text:
                continue                    # 기호만 있고 내용이 없는 줄 — 본문은 다음 줄에 온다
            line = "  " * _BULLET_DEPTH[m.group(1)] + "- " + text
        elif (lm := _LBULLET_RE.match(line)):
            text = (lm.group(1) or "").strip()
            if not text:
                continue
            line = "- " + text
        out.append(line)
    return "\n".join(out)


# PDF 자간(letter-spacing)으로 한 음절씩 벌어진 텍스트를 되붙인다. 공문서 조판은 강조
# 구간에서 어절 사이를 두 칸, 음절 사이를 한 칸으로 벌리므로 두 칸=어절 경계(→한 칸),
# 한 칸=자간(→제거)로 본다. 음절이 1~2칸 간격으로 4개 이상 이어질 때만 손대므로 정상
# 국문(어절 사이만 한 칸)은 건드리지 않는다. 어절 경계는 1~3칸(강조 조판은 어절을 세 칸
# 까지 벌린다)까지 인정 — 다중 공백은 모두 한 칸으로 접힌다. 다음 음절이 여러 칸이라도
# 정상 어절은 낱 음절이 아니므로 {3,}(4음절 연속) 조건에 걸리지 않아 안전하다.
# ponytail: 자간 경계까지 한 칸뿐이면 어절이 붙는다. 실무 대부분은 두 칸 이상이라 무시.
# (?<![가-힣]): 정상 어절 끝 음절("대한민국 과 학..."의 '국')에서 런이 시작돼 앞 단어를
# 삼키지 않게 한다. 자간 음절은 앞뒤가 공백/비한글로 떨어진 낱 음절이어야 한다.
_SPACED_RUN = re.compile(r"(?<![가-힣])[가-힣](?: {1,3}[가-힣]){3,}")


def _despace(md: str) -> str:
    def collapse(m):  # 두 칸 → 표식, 한 칸 → 삭제, 표식 → 한 칸
        return m.group(0).replace("  ", "\x00").replace(" ", "").replace("\x00", " ")
    return _SPACED_RUN.sub(collapse, md)


# 구두점 주변 과잉 공백 정리(자간과 별개 아티팩트): 가운뎃점("산 · 학 · 연"), 괄호·낫표
# 안쪽 패딩("( 연 )", "｢ 법 ｣"), 연도 약물음표("' 24"→"'24")를 붙인다. 공백만 다루고
# 줄바꿈은 건드리지 않는다. straight quote(' ")의 열림/닫힘은 판별이 모호해 제외한다.
_MIDDOT = re.compile(r" *([·‧⸱・･]) *")   # ･=U+FF65 반각(공문서에서 흔함)
_OPEN = re.compile(r"([｢「『（(\[]) +")
_CLOSE = re.compile(r" +([｣」』）)\]])")
_YEAR = re.compile(r"' +(\d)")


def _tighten(md: str) -> str:
    md = _MIDDOT.sub(r"\1", md)
    md = _OPEN.sub(r"\1", md)
    md = _CLOSE.sub(r"\1", md)
    return _YEAR.sub(r"'\1", md)


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
    md_path.write_text(_tighten(_despace(_fix_bullets(md))), encoding="utf-8")

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
