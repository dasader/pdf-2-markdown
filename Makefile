.PHONY: build

# up -d --build 하나가 빌드+기동을 다 한다. mem_limit이 바뀌면 컨테이너도 재생성된다.
build:
	git pull
	docker compose up -d --build
