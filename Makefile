.PHONY: build rebuild bench

# up -d --build 하나가 빌드+기동을 다 한다. mem_limit이 바뀌면 컨테이너도 재생성된다.
# Dockerfile이 app/·static/을 COPY하므로 코드가 바뀌면 이미지 해시가 바뀌어 컨테이너도
# 자동으로 재생성된다 — --force-recreate 불필요.
build:
	git pull
	docker compose up -d --build

rebuild: build

# 변환 성능 실측: make bench PDF=~/문서.pdf [N=회차] [SWEEP=1]
# worker와 같은 메모리 한도로 띄운다 — 한도가 다르면 peak 비교가 무의미하다.
# 이미지에 app/이 COPY돼 있으므로 코드를 고쳤으면 make build 먼저.
bench:
	docker run --rm --memory=5g --memory-swap=5g \
	  -v $(CURDIR)/bench:/bench:ro -v $(dir $(abspath $(PDF))):/pdfs:ro \
	  pdf2md:latest python /bench/bench.py /pdfs/$(notdir $(PDF)) \
	  $(or $(N),3) $(if $(SWEEP),--sweep)
