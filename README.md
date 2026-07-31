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

#### 공문서 마크다운 후처리 (`convert.postprocess`)

docling 출력을 그대로 쓰면 공문서 조판 특유의 잡음이 남는다. 규칙은 순서가 곧 의미다:

| 규칙 | 하는 일 |
|---|---|
| 불릿 기호 | `□ㅁ■◇◆`→0단계, `ㅇ○◦●▷▶`+`¡Ÿ`→1단계, `▪-`+soft hyphen(U+00AD)→2단계로 들여쓰기 변환. 같은 기호를 두 번 찍은 조판(`- ▪ ▪ 제목`)과 기호 뒤 공백이 없는 조판(`- ▪내용`)도 처리. `①⇨`는 뜻이 있어 보존 |
| 심볼폰트 잔재 | 한글 문서에 나올 수 없는 글자가 불릿으로 추출된다: `l`·`Ÿ`·`¡`, 그리고 사유영역(PUA, U+E000~F8FF) 글리프. 줄머리면 목록으로, 표 셀 안이면 `•`로, PUA는 뜻이 없어 제거 |
| 기호 없는 항목 | docling이 원문 하위 불릿 `-`를 마크다운 불릿으로 흡수해 기호를 지운다 → 들여쓰기만 믿으면 부모와 자식이 뒤집힌다. 기호 없는 항목은 직전 기호 항목의 자식으로 (순번 기호 `①⇨`로 시작하면 제외) |
| 각주·비고 | `*`, `**`, `※`로 시작하는 줄은 목록 항목이 아니다 → 불릿 해제 + `*` 이스케이프 + 앞에 빈 줄(없으면 앞 항목에 삼켜진다) |
| 목록 깊이 | 목록 블록마다 0부터 다시 매김. **부모 없는 4칸 들여쓰기는 마크다운에서 코드 블록**이라, docling이 옮긴 PDF 여백을 그대로 두면 본문이 통째로 코드로 렌더된다 |
| 제목 병합 | 번호만 있는 제목(`## 2`)을 뒤따르는 제목(`## 평가방향`)에 합침 |
| 제목 계층 | 로마숫자→h1, `1.`→h2, `1-3.`/`가.`→h3, 제목 앞 기호(`■`→h2, `ㅇ¡`→h3). 무번호 제목은 **직전 신호 제목의 한 단계 아래**(맨 앞이면 문서 제목=h1)이고 같은 섹션 안에서는 그 레벨을 유지 — 고정 레벨을 주면 무번호가 기호 제목의 부모인 문서에서 계층이 뒤집힌다. **docling이 이미 계층을 구분했거나 번호·기호가 하나도 없는 문서는 건드리지 않는다** |
| 잘린 항목 | 앞 항목이 쉼표·`및`·`혹은`·`하여`·관형형으로 끝나거나 **뒷 항목이 어미·조사로 시작**(`하여…`, `등 …`)하면 이어붙임. 들여쓰기가 달라도 잇는다 |
| 자간 복원 | 음절 자간(`글 로 벌`→`글로벌`) → 구두점 공백(`산 · 학`→`산·학`) → 어절 자간(`체계성  -  부처`→`체계성 - 부처`) |

표 행(`|`)은 기호 치환을 빼면 모든 규칙에서 제외한다(셀 구분·정렬 보존).
잡지 못하는 것: `…부처의` / `…한다` 뒤에서 잘린 줄. 끝 음절이 조사와 같은 명사(`성과`, `결과`,
`정의`)가 흔해 형태소 분석 없이는 오검이 나고, 멀쩡한 두 항목을 붙이는 쪽이 더 나쁘다.
표 셀 안에서 여러 항목이 한 줄로 뭉치는 것도 그대로 둔다(셀 구조를 깨는 편이 더 나쁘다).

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/` | 웹 UI |
| `POST` | `/api/jobs` | multipart 업로드(다중). 폼필드 `include_images`, `include_tables_csv` |
| `POST` | `/api/convert` | **동기 변환.** PDF 1개(`file`) → 마크다운 본문 (text/plain) |
| `GET` | `/api/jobs` | `{jobs, busy}` — 내 잡 목록(+admin 시 전체), 대기 잡엔 `ahead` |
| `GET` | `/api/events` | SSE. `{jobs, busy}` 변경분 push |
| `GET` | `/api/jobs/{id}/preview` | 마크다운 원문 (text/plain) |
| `GET` | `/api/jobs/{id}/download` | 결과 `result.zip` |
| `GET` | `/api/download-all` | 완료 잡들을 파일명별 폴더로 묶은 단일 ZIP |

세션은 `sid` 쿠키(httpOnly)로 자동 발급. 관리자 요청은 `X-Admin-Key` 헤더로.

### 에이전트 연동 — `POST /api/convert`

UI 흐름(업로드 → 폴링 → preview)은 쿠키 세션을 이어가야 하지만, 외부 에이전트·스크립트는
이 엔드포인트 하나면 된다. 쿠키도 폴링도 필요 없다.

```bash
curl -F file=@doc.pdf http://<host>:8001/api/convert     # → 마크다운 본문
```

| 폼필드 | 기본값 | 설명 |
|---|---|---|
| `file` | (필수) | PDF 1개 |
| `include_images` | `false` | `true`면 doc.md에 `images/...` 상대경로가 남는다(본문만 받는 쪽엔 깨진 링크) |
| `include_tables_csv` | `false` | 표는 옵션과 무관하게 본문에 마크다운 표로 들어간다 |
| `timeout` | `300` | 초. 초과하면 `202 {job_id}` — 회수는 `preview`(쿠키 또는 `X-Admin-Key`) |

검증 실패(비PDF·스캔본·페이지 초과 등)는 `422`에 사유가 그대로 담긴다. 업로드 검증·해시
캐시·큐는 `/api/jobs`와 같은 경로를 타므로 같은 파일 재요청은 캐시로 즉시 반환된다.
워커가 1개라 **앞선 잡이 있으면 그만큼 대기**한다(1.5초/페이지 추정, 500p면 12분).

## 제약·가드레일

- 업로드 검증: 매직바이트(`%PDF`) → **100MB / 500페이지** 상한 → 0페이지·손상·암호걸림
  → **텍스트 레이어 없음(스캔본)**. 모두 업로드 시점에 걸러 `failed` + 사유로 즉시 응답한다
  (변환을 몇 분 돌린 끝에 빈 결과를 받는 일이 없다). 세션당 대기 잡 20개 상한.
- 저장 파일명은 항상 SHA-256(요청 경로 신뢰 안 함). 다운로드/미리보기 경로는 DB에서만 해석.
- 결과는 **24시간 보관** 후 워커가 참조 카운트 기준으로 정리.
- 스캔본(이미지 PDF) OCR·수식 LaTeX 변환은 미지원(업로드 단계에서 거부).

## 개발 / 테스트

로컬에 시스템 pytest·docling이 없어도 [uv](https://docs.astral.sh/uv/)로 단위 테스트를 돌린다
(docling은 지연 import + 테스트에서 monkeypatch라 torch 없이 실행됨):

```bash
uv run --with pytest --with fastapi --with python-multipart --with httpx \
       --with pypdfium2 --with pandas python -m pytest tests/test_pdf2md.py -q
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
