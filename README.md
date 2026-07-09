# pdf2md

PDF를 업로드하면 **Markdown**으로 변환해 내려받는 웹서비스. 표(병합셀·중첩 헤더)가 많은
정부문서·연구보고서에 맞춰 [Docling](https://github.com/docling-project/docling)으로
표 구조까지 살려 변환한다. 여러 파일을 한 번에 올리면 큐에 쌓아 순차 처리하고 진행 상황을
실시간으로 보여준다. GPU 없이, 저사양 서버(워커 3GB)에서 돈다.

![engine](https://img.shields.io/badge/engine-Docling-blue) ![python](https://img.shields.io/badge/python-3.12-green) ![gpu](https://img.shields.io/badge/GPU-불필요-lightgrey)

## 주요 기능

- **다중 업로드 → 순차 큐** — 파일 여러 개를 올리면 워커 1개가 하나씩 처리. 전역 대기 순번
  (`앞에 N개 대기`)과 "다른 변환 처리 중" 하트비트로 동시 사용 상황도 정직하게 표시.
- **실시간 진행 상황** — SSE로 상태·진행률 push. (진행률은 페이지 수 기반 추정치)
- **표 → CSV, 그림 → 이미지, 전체 → ZIP** — `doc.md` + `images/` + `tables/*.csv`를 ZIP으로.
- **미리보기 · 마크다운 복사 · 완료분 전체 내려받기**.
- **해시 캐시** — 같은 파일·같은 옵션이면 변환을 건너뛰고 즉시 결과 반환.
- **멀티유저** — 로그인 없이 쿠키 세션으로 사용자별 격리. `X-Admin-Key`로 전체 조회.
- **노션풍 UI** — 밝고 부드러운 단일 페이지, 다크 모드 지원, 폐쇄망 대비 폰트·라이브러리 self-host.

## 빠른 시작

```bash
cp .env.example .env        # 필요 시 PDF2MD_ADMIN_KEY 설정
docker compose up -d
```

- 웹: http://localhost:8001
- 원격 서버면 SSH 터널로: `ssh -L 8001:localhost:8001 <user>@<서버>` 후 브라우저에서 위 주소.

> 첫 빌드는 Docling 모델을 이미지에 굽느라 수 분 걸린다(이미지 ~12GB). 이후 기동은 즉시.

## 설정 (`.env`)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `PDF2MD_PORT` | `8001` | 웹 host 포트 |
| `PDF2MD_ADMIN_KEY` | (빈값) | 관리자 전체조회 키. 비우면 관리자 기능 **비활성** |
| `PDF2MD_SEC_PER_PAGE` | `1.5` | 진행률 추정용 초/페이지 (실측으로 보정) |
| `PDF2MD_DATA` | `/data` | 데이터 루트 (compose가 `./data`에 마운트) |

## 아키텍처

```
docker compose (이미지 1개, 서비스 2개)
┌──────────────┐        ┌──────────────┐
│  web (8001)  │        │  worker      │
│  FastAPI+UI  │        │  Docling ×1  │
└──────┬───────┘        └───────┬──────┘
       └────── data/app.db ─────┘   ← SQLite = 큐 + DB (WAL)
              data/uploads/  data/results/
```

- **Redis·Celery·Postgres·nginx·Node 없음.** 워커 1개라 브로커 불필요, 프론트는 빌드 없는
  정적 파일이라 웹서버도 불필요.
- 워커는 SQLite를 폴링해 `queued` 잡을 하나씩 처리. 순차 처리 = 요구사항이자 메모리 상한 장치.
- `web`/`worker`는 동일 이미지, 커맨드만 다름. `mem_limit`: web 2GB, worker 3GB.

### 변환 엔진

Docling, CPU 전용. 저사양 호스트에 맞춰 튜닝:

- `do_ocr=False` (텍스트 PDF 전제 → OCR 모델 미로딩, ~2GB 절감)
- `TableFormerMode.ACCURATE` (표 정확도 우선)
- **`PyPdfiumDocumentBackend` + `page_batch_size=1`** — 페이지를 1장씩 처리하고 가벼운 PDF
  백엔드를 써서, 14MB·27페이지 이미지 조밀 문서도 **3GB 안에서 풀 품질로** 변환(실측 검증).
- OOM 등으로 변환이 실패하면 **저사양 모드(그림 추출 off)로 1회 재시도**, 그래도 실패하면
  명확한 메시지로 `failed` 처리(무한 재시도·큐 정지 방지).

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/` | 웹 UI |
| `POST` | `/api/jobs` | multipart 업로드(다중). 폼필드 `include_images`, `include_tables_csv` |
| `GET` | `/api/jobs` | `{jobs, busy}` — 내 잡 목록(+admin 시 전체), 대기 잡엔 `ahead` |
| `GET` | `/api/events` | SSE. `{jobs, busy}` 변경분 push |
| `GET` | `/api/jobs/{id}/preview` | 마크다운 원문 (text/plain) |
| `GET` | `/api/jobs/{id}/download` | 결과 `result.zip` |
| `GET` | `/api/download-all` | 완료 잡들을 파일명별 폴더로 묶은 단일 ZIP |

세션은 `sid` 쿠키(httpOnly)로 자동 발급. 관리자 요청은 `X-Admin-Key` 헤더로.

## 제약·가드레일

- 파일당 **100MB / 500페이지** 상한, 매직바이트(`%PDF`) 검사, 세션당 대기 잡 20개 상한.
- 저장 파일명은 항상 SHA-256(요청 경로 신뢰 안 함). 다운로드/미리보기 경로는 DB에서만 해석.
- 결과는 **24시간 보관** 후 워커가 참조 카운트 기준으로 정리.
- 스캔본(이미지 PDF) OCR·수식 LaTeX 변환은 미지원.

## 개발 / 테스트

로컬에 시스템 pytest·docling이 없어도 [uv](https://docs.astral.sh/uv/)로 단위 테스트를 돌린다
(docling은 지연 import + 테스트에서 monkeypatch라 torch 없이 실행됨):

```bash
uv run --with pytest --with fastapi --with python-multipart --with httpx \
       --with pymupdf --with pandas python -m pytest tests/test_pdf2md.py -q
```

실제 변환은 Docker 안에서 pip 설치된 docling으로 동작한다.

```
app/
  config.py    # 환경변수·경로·상한
  db.py        # SQLite 잡 큐 (스키마·CRUD·캐시·정리)
  convert.py   # Docling 변환 + 이미지/CSV 추출 + ZIP 패키징
  worker.py    # 폴링 루프: 잡 선점→변환→상태갱신, 보관 정리
  web.py       # FastAPI: 업로드·목록·다운로드·미리보기·SSE
static/        # 노션풍 UI (index.html, app.js, style.css) + vendored marked.js·Noto Sans
docs/superpowers/  # 설계 문서·구현 계획
```

## 라이선스

[MIT](LICENSE)
