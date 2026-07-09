FROM python:3.12-slim

# PyMuPDF·docling 런타임에 필요한 최소 라이브러리
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Docling 레이아웃/TableFormer 모델을 빌드 타임에 받아 상주(런타임 다운로드 회피)
RUN python -c "from docling.utils.model_downloader import download_models; download_models()" || true

COPY app/ app/
COPY static/ static/

ENV PDF2MD_DATA=/data
EXPOSE 8001
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8001"]
