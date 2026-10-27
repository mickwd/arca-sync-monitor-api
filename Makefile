# Atajos de desarrollo (Linux/macOS o Git Bash en Windows).
PYTHON ?= python3
VENV   ?= .venv
ifeq ($(OS),Windows_NT)
  BIN := $(VENV)/Scripts
else
  BIN := $(VENV)/bin
endif

.PHONY: help venv install lint format test cov run mock up down logs clean

help:  ## Muestra esta ayuda
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

venv:  ## Crea el entorno virtual
	$(PYTHON) -m venv $(VENV)

install: venv  ## Instala dependencias de desarrollo
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -r requirements-dev.txt

lint:  ## Ruff (lint + formato)
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:  ## Formatea y aplica fixes automáticos
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

test:  ## Corre los tests
	$(BIN)/python -m pytest

cov:  ## Tests con reporte de cobertura
	$(BIN)/python -m pytest --cov --cov-report=term-missing

run:  ## Levanta la API en modo desarrollo
	$(BIN)/uvicorn app.main:app --reload --port 8000

mock:  ## Levanta el mock de ARCA + receptor en :5001
	$(BIN)/python -m mock_arca.app

up:  ## docker compose up (api + mock-arca + postgres)
	docker compose up -d --build

down:  ## docker compose down
	docker compose down

logs:  ## Logs de la API
	docker compose logs -f api

clean:  ## Borra caches y bases locales
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage *.db
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
