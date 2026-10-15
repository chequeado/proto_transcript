# En la VM: make up DOCKER="sudo docker"
DOCKER ?= docker
COMPOSE = $(DOCKER) compose
RADIO ?= https://shoutcast.ccma.cat/ccma/catalunyaradioHD.mp3
PORT := $(or $(shell sed -n 's/^APP_PORT=//p' .env 2>/dev/null),8000)

.PHONY: help up stop logs ps restart rebuild local radio-test

help:
	@echo "make up          levanta la web en Docker (http://localhost:$(PORT))"
	@echo "make stop        apaga (cierra las sesiones de AssemblyAI antes)"
	@echo "make logs        eventos en vivo (Ctrl-C para salir, no apaga)"
	@echo "make ps          estado del contenedor"
	@echo "make restart     apaga y vuelve a levantar con el código actual"
	@echo "make rebuild     rearma la imagen desde cero (trae el último yt-dlp)"
	@echo "make local       la web sin Docker, con el .venv"
	@echo "make radio-test  2 min de la radio sin transcribir (gratis)"

up:
	$(COMPOSE) up -d --build
	@echo "listo: http://localhost:$(PORT)  ·  /radio  ·  /dev"

stop:
	$(COMPOSE) stop

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

restart: stop up

rebuild:
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

local:
	.venv/bin/uvicorn proto.web.app:app --port 8000

radio-test:
	$(COMPOSE) run --rm proto python -m proto.cli "$(RADIO)" --radio --max-minutes 2 --stt null
