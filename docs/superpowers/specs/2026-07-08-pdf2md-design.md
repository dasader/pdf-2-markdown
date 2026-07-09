# pdf2md — PDF → Markdown 변환 웹서비스 설계

작성일: 2026-07-08

## 목적

PDF를 업로드하면 Markdown으로 변환해 내려받는 웹서비스. 여러 파일을 한 번에
올리면 큐에 쌓아 순차 처리하고, 진행 상황을 실시간으로 보여준다. 여러 사람이
동시에 써도 서로의 작업이 섞이지 않는다. Docker로 배포한다.

## 제약

- 호스트: 6코어 / RAM 10GB(가용 5.7GB) / **GPU 없음**. 다른 서비스 여러 개와 공유.
- 대상 문서: 텍스트 레이어가 있는 PDF. 정부문서·정부연구기관 보고서가 다수.
  **표(병합셀·중첩 헤더)가 많고 수식은 거의 없다.**

## 엔진 선택: Docling

| | pymupdf4llm | Marker | **Docling** |
|---|---|---|---|
| 방식 | 규칙 기반 | Surya 모델군 | 레이아웃 모델 + TableFormer |
| GPU | 불필요 | 사실상 필수 | 불필요 |
| CPU 속도 | 수십 ms/page | 매우 느림 | ~0.6–3 s/page |
| 표 정확도 (TEDS) | 낮음 (병합셀 취약) | 75–80% | **91%+** (ACCURATE) |
| 강점 | 속도 | 수식(LaTeX) | **표·다단 레이아웃** |

- Marker의 유일한 우위는 수식인데 이 코퍼스엔 수식이 없다. 반대로 Marker의
  약점이 정확히 표다. GPU도 없다 → 탈락.
- pymupdf4llm은 병합셀 표에서 깨진다. 이 코퍼스의 핵심 자산이 표다 → 탈락.
- Docling의 느림(페이지당 1~3초)은 큐 + 진행 표시 UX로 흡수된다.

**설정**: `do_ocr=False` (텍스트 PDF이므로 OCR 엔진 미로딩 → 약 2GB 절감),
`do_table_structure=True`, `TableFormerMode.ACCURATE`.
워커 1개 기준 상주 RAM 약 2GB (레이아웃 ~1GB + TableFormer ~0.6GB).

## 아키텍처

이미지 1개, compose 서비스 2개.

```
┌──────────────┐        ┌──────────────┐
│  web         │        │  worker      │
│  FastAPI     │        │  Docling ×1  │
│  + 정적 UI    │        │  RAM ~2GB    │
└──────┬───────┘        └───────┬──────┘
       └────── data/app.db ─────┘   ← SQLite = 큐 + DB
              data/uploads/  data/results/
```

- **Redis / Celery / Postgres / nginx / Node 없음.** 워커가 1개인데 메시지 브로커를
  둘 이유가 없다. 프론트는 빌드 없는 정적 HTML 한 장이라 별도 웹서버도 불필요.
- `web`은 API + 정적 파일 서빙을 겸한다.
- `worker`는 1초 폴링으로 `queued` 잡을 하나 집어 처리한다. 순차 처리는 요구사항이자
  메모리 상한 장치.
- 두 서비스는 동일 이미지를 쓰고 커맨드만 다르다. `data/` 볼륨을 공유한다.
- 프로세스를 분리한 이유: Docling이 GIL을 잡는 동안 SSE 응답이 끊기지 않게 하기 위함.

### 진행률의 정확도

Docling은 페이지 단위 콜백을 제공하지 않는다.

- **큐 전체 진행**("3/7번째 파일")은 **정확**하다.
- **파일별 진행바**는 PyMuPDF로 즉시 읽은 페이지 수 × 실측 초/페이지 기반의 **추정치**다.
  UI에 "예상"으로 표기한다. 완료 시 100%로 확정.
- 페이지를 청크로 잘라 변환하면 실제 진행률을 얻을 수 있으나, 페이지 경계에서 표가
  깨지므로 채택하지 않는다.

초/페이지 계수는 실측으로 보정 가능하게 환경변수로 둔다 (`SEC_PER_PAGE`, 기본 1.5).

### 동시 사용자와 큐 순번 (중요)

워커는 1개이고 **전역 FIFO**(`created_at` 순)로 모든 세션의 잡을 처리한다. 각 사용자는
**자기 잡만** 본다. 따라서 순진하게 "내 잡 기준" 순번을 보이면, 다른 사용자의 잡이
처리되는 동안 내 화면엔 진행 중인 게 없어 **멈춘 것처럼 보인다.**

두 가지로 정직하게 해결한다:

1. **대기 순번은 전역 기준.** 각 대기 잡의 순번 = `created_at`이 더 이른 `queued`+`running`
   잡의 전체 개수(다른 사용자 것 포함). UI는 "앞에 N개 대기" / "곧 시작"으로 표기.
   개수만 노출하며 **다른 사용자의 파일명·내용은 절대 보이지 않는다.**
2. **전역 처리 하트비트.** 워커가 바쁘지만 그게 내 잡이 아닐 때, 상단에
   "다른 변환을 처리하는 중입니다. 곧 순서가 됩니다." 표시. 얼어붙음이 아니라 대기임을 알린다.

트레이드오프: 다른 사용자의 잡 **존재·개수**는 드러난다(이름·내용은 아님). 사내·지인
공유 도구에서는 "서버가 살아 있고 줄 서 있다"는 안심 신호로 작동하므로 채택한다.

## 데이터 모델

SQLite 테이블 하나.

```sql
CREATE TABLE jobs (
  id          TEXT PRIMARY KEY,   -- uuid4
  session_id  TEXT NOT NULL,
  filename    TEXT NOT NULL,      -- 원본 표시용 이름
  sha256      TEXT NOT NULL,
  opts_hash   TEXT NOT NULL,      -- 변환 옵션 정규화 해시
  status      TEXT NOT NULL,      -- queued | running | done | failed
  page_total  INTEGER,
  started_at  REAL,
  finished_at REAL,
  error       TEXT,
  result_dir  TEXT,
  n_tables    INTEGER,            -- 완료 요약 칩용 (표 개수)
  n_images    INTEGER,            -- 완료 요약 칩용 (이미지 개수)
  created_at  REAL NOT NULL
);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_cache  ON jobs(sha256, opts_hash, status);
CREATE INDEX idx_jobs_session ON jobs(session_id, created_at);
```

WAL 모드로 열어 web/worker 동시 접근을 허용한다.

### 파일 배치

```
data/uploads/<sha256>.pdf                       # 해시 파일명 → 중복 업로드 자동 dedupe
data/results/<sha256>-<opts_hash>/
    doc.md
    images/p3-1.png ...
    tables/table-01.csv ...
    result.zip
```

### 해시 캐시

업로드 즉시 SHA-256을 계산한다. `(sha256, opts_hash)`에 `status='done'`인 잡이 있으면
변환을 건너뛰고 새 잡을 곧바로 `done`으로 만들며 기존 `result_dir`을 가리킨다.
같은 정부보고서를 여러 사람이 올리는 상황에서 서버 자원을 가장 크게 아낀다.

`opts_hash`는 결과물에 영향을 주는 옵션만으로 만든다: `include_images`, `include_tables_csv`.

## 인증 / 멀티유저

- 로그인 없음. 첫 방문 시 `session_id`(uuid4)를 **httpOnly + SameSite=Lax 쿠키**로 발급.
- 목록·다운로드·미리보기는 본인 `session_id` 잡만 접근 가능.
- `X-Admin-Key` 헤더가 환경변수 `ADMIN_KEY`와 일치하면 모든 세션의 잡을 조회할 수 있다.
  `ADMIN_KEY`가 비어 있으면 관리자 기능은 비활성(빈 문자열 우회 방지).

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/` | 정적 UI |
| `POST` | `/api/jobs` | multipart 업로드(다중). 옵션 폼필드. → 생성된 잡 목록 반환 |
| `GET` | `/api/jobs` | `{jobs:[...], busy: bool}` — 내 잡 목록(admin 시 전체) + 전역 워커 busy |
| `GET` | `/api/events` | SSE. `{jobs, busy}` 변경분 push |
| `GET` | `/api/jobs/{id}/preview` | 렌더용 마크다운 원문 (text/plain). "마크다운 복사"도 이걸 사용 |
| `GET` | `/api/jobs/{id}/download` | `result.zip` |
| `GET` | `/api/download-all` | 내 완료 잡들을 파일명별 폴더로 묶은 단일 ZIP |

- `/api/jobs`·SSE의 각 대기 잡에는 전역 순번 `ahead`(int)를 포함한다.
- 최상위 `busy`는 "워커가 지금 무언가 처리 중"인지. 내 잡이 아닌 걸 처리 중일 때
  UI 하트비트를 켜는 신호. 응답 형태가 배열이 아니라 객체(`{jobs, busy}`)임에 유의.
- SSE는 `web`이 SQLite를 0.5초 폴링해 변경분만 내보낸다. 연결이 없으면 폴링도 안 한다.

## 가드레일 (축소하지 않음)

- 업로드 파일당 **100MB**, **500페이지** 상한. 초과 시 잡을 `failed`로 즉시 기록.
- 매직바이트 `%PDF` 검사. 확장자만 믿지 않는다.
- 업로드 파일명은 저장에 쓰지 않는다(해시 파일명). DB에만 표시용으로 보관.
- `download`/`preview`는 `result_dir`을 DB에서 읽어 사용한다. 경로를 요청에서 받지 않는다.
- compose `mem_limit: 3g` (worker), `2g` (web).
- 세션당 큐 대기 잡 상한 20개 (한 사람이 큐를 독점하지 못하게).

## 정리(retention)

생성 후 24시간이 지난 잡 행을 워커 루프가 지나가며 삭제한다.

캐시 히트로 생긴 잡은 남의 `result_dir`을 가리키므로, 파일 삭제는 **참조 카운트**로
판단한다: `uploads/<sha>.pdf`와 `results/<sha>-<opts>/`는 이를 참조하는 잡 행이 하나도
남지 않을 때만 지운다. (잡 행 삭제 → 고아 파일 스윕, 두 단계)

결과가 삭제되면 캐시도 자연히 무효화된다 — 캐시 조회는 `status='done'`인 잡 행을
찾는 것이므로.

## UI

정적 1페이지 (`static/index.html`, `app.js`, `style.css`, vendored `marked.min.js`).

**디자인 컨셉 (frontend-design으로 확정, 시안 승인됨): 노션풍 — 거의 무채색, 색은 상태로만.**

- **팔레트(라이트)**: `--paper #ffffff`, `--surface #f7f6f3`, `--ink #37352f`,
  `--ink-muted #787774`, `--line #eae9e5`. 상태색만 유채색:
  대기 `#9b9a97` · 변환 중 `#d9730d`(앰버) · 완료 `#448361`(초록) · 실패 `#e03e3e`.
  다크는 `#191919/#202020/#e9e8e5` 기반, 상태색 밝게 보정. `prefers-color-scheme` +
  `data-theme` 양방향 오버라이드. **밝은 테마 기본.**
- **타이포**: **Noto Sans 한 벌**(`Noto Sans KR` 포함, self-host — 폐쇄망·CDN 회피).
  굵기·자간·크기로만 위계: H1 600/-.02em, 파일명 500, 본문 400, 라벨 대문자 12px/자간 .09em.
  숫자는 `tabular-nums`. **이탤릭 전면 금지** — 미리보기의 마크다운 `em`도
  `font-style:normal; font-weight:500`로 강조를 굵기로 표현.
- **레이아웃**: 좌측 정렬 단일 컬럼(max-width 700px), 넉넉한 상단 여백. 대시보드가 아니라
  Notion 페이지처럼 읽힘.
- **시그니처**: 대기열 왼쪽 **세로 스파인 + 상태 노드**. 워커 1개 FIFO라 "순서"가 실제 정보다.
  변환 중 노드는 은은한 링 펄스, 완료 노드는 한 번 톡 튐(pop).
- **모션(부드러운 전환)**: 새 잡 행 8px 슬라이드+페이드 40ms 스태거, 진행바 부드럽게 차되
  추정치라 옅은 sheen 흘림, 모달 0.98→1 스케일+페이드. `prefers-reduced-motion` 존중.

기능:
- 드래그앤드롭 존 + 파일 선택. 옵션 토글 2개: **이미지 포함**(기본 켬), **표를 CSV로**(기본 켬).
  이미지 미포함 시 마크다운에 이미지 자리표시 주석만 남긴다.
- 잡 행: 파일명 · 상태 · 진행바 · 전역 대기 순번("앞에 N개 대기"). busy 하트비트는 상단에.
- 완료 시: **요약 칩 "표 N · 이미지 M"** · `[미리보기]` · `[마크다운 복사]` · `[ZIP 내려받기]`.
  내 완료가 2개 이상이면 상단에 **[완료분 전체 내려받기]** 노출.
- 미리보기는 모달에서 마크다운 렌더. `marked.js`는 CDN이 아닌 vendored(폐쇄망 대비).
  "마크다운 복사"는 `/preview` 원문을 `navigator.clipboard`로 복사.

## 포트

`PORTS.md` 규칙: `대역 + NN`.

비어 있는 최소 NN은 `00`이나 그러면 web이 `8000`이 되고, 이는 `nst-wiki`가 현재
점유 중이며 문서에도 "⚠ 위험 기본값"으로 표기돼 있다. 따라서 **NN=01**.

| 구성요소 | 포트 |
|---|---|
| web (API + UI 단일 진입점) | `8001` |

`.env`로 오버라이드 가능하게 두되 기본값은 `${PDF2MD_PORT:-8001}`.
`PORTS.md` 레지스트리에 한 줄 추가한다.

## 테스트

`test_pdf2md.py` 하나. Docling 실제 호출은 monkeypatch(실 변환은 느려 테스트에 부적합).

1. 해시 캐시 히트 — 같은 파일·같은 옵션 두 번 업로드 → 두 번째는 변환 함수 미호출, `done`.
2. 비-PDF 거부 — 매직바이트가 아닌 파일 업로드 → 거부.
3. 잡 상태 전이 — `queued → running → done`, 예외 시 `failed` + `error` 기록.
4. 세션 격리 — 다른 `session_id`로 남의 잡 조회·다운로드 시 404.
5. 전역 대기 순번 — 다른 세션의 이른 잡이 있으면 내 대기 잡의 `ahead`가 그만큼 커진다.

## 명시적으로 만들지 않는 것

페이지 범위 지정, 회원가입/계정, Redis·Celery, 프론트엔드 빌드 파이프라인,
다중 워커, OCR(스캔본 지원), 수식 LaTeX 변환, 변환 이력 영구 보관,
**대기열 드래그 재정렬**(제안됐으나 보류).

필요해지면: 다중 워커는 워커 컨테이너를 `deploy.replicas`로 늘리고 잡 선점에
`UPDATE ... WHERE status='queued'` 원자적 갱신을 쓰면 된다(설계상 이미 가능).
