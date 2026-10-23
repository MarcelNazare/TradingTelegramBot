# ============================================================
# Forex Signal Bot — Makefile
# ============================================================

# Auto-detect Python and venv paths
ifeq ($(OS),Windows_NT)
    PYTHON  = python
    VENV_PY = .venv/Scripts/python.exe
    VENV_PIP = .venv/Scripts/pip.exe
else
    PYTHON  = python3
    VENV_PY = .venv/bin/python
    VENV_PIP = .venv/bin/pip
endif

.PHONY: help install env run test check clean

help: ## Show available commands
	@echo "make install    Create venv and install dependencies"
	@echo "make env        Create .env from .env.example"
	@echo "make run        Run the bot once"
	@echo "make test       Send a test Telegram message"
	@echo "make check      Verify syntax and .env"
	@echo "make clean      Remove __pycache__ and caches"

install: ## Create venv and install dependencies
	uv run install -r requirements.txt


run: ## Run the bot once
	uv run main.py

listen: ## Run Polling Service
	uv run run_commands.py

test: ## Send a test Telegram message
	uv run telegram_test.py

check: ## Verify syntax and .env
	$(PYTHON) -m py_compile forex_bot.py && echo "Syntax OK"
	@test -f .env && echo ".env found" || echo "MISSING .env — run 'make env'"

clean: ## Remove __pycache__ and caches
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name '*.pyc' -delete 2>/dev/null || true