# ============================================================
# Forex Signal Bot — Makefile
# ============================================================

.PHONY: help install install-dev env run run-verbose clean test lint \
        check deploy-check telegram-test log tail-log \
        backup backup-list requirements freeze \
        pa-upload pa-schedule pa-logs

# ---------- Config ----------
PYTHON        ?= python3
VENV          ?= venv
VENV_PY       = $(VENV)/bin/python
VENV_PIP      = $(VENV)/bin/pip
SCRIPT        = forex_bot.py
ENV_FILE      = .env
LOG_DIR       = logs
BACKUP_DIR    = backups
PA_USER       ?= yourusername
PA_HOST       = $(PA_USER).pythonanywhere.com
PA_REMOTE_DIR = /home/$(PA_USER)

# ---------- Colors ----------
GREEN  = \033[0;32m
YELLOW = \033[0;33m
RED    = \033[0;31m
BLUE   = \033[0;34m
NC     = \033[0m

# ---------- Help ----------
help: ## Show this help
	@echo ""
	@echo "$(BLUE)Forex Signal Bot — available targets$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)Quick start:$(NC)"
	@echo "  make install     # create venv and install deps"
	@echo "  make env         # create .env from .env.example"
	@echo "  make run         # run the bot once"
	@echo ""

# ---------- Setup ----------
install: $(VENV)/bin/activate ## Create venv and install dependencies
	@echo "$(GREEN)[install] Installing dependencies...$(NC)"
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r requirements.txt
	@echo "$(GREEN)[install] Done.$(NC)"

install-dev: install ## Install dependencies + dev tools (pytest, ruff, black)
	@echo "$(GREEN)[install-dev] Installing dev tools...$(NC)"
	$(VENV_PIP) install pytest ruff black
	@echo "$(GREEN)[install-dev] Done.$(NC)"

$(VENV)/bin/activate: requirements.txt
	@echo "$(GREEN)[venv] Creating virtualenv in $(VENV)/...$(NC)"
	$(PYTHON) -m venv $(VENV)
	@touch $(VENV)/bin/activate

requirements.txt: ## Regenerate requirements.txt from installed packages
	@if [ ! -f requirements.txt ]; then \
		echo "yfinance>=0.2.40" > requirements.txt; \
		echo "pandas>=2.0.0" >> requirements.txt; \
		echo "numpy>=1.24.0" >> requirements.txt; \
		echo "python-telegram-bot>=20.0" >> requirements.txt; \
		echo "python-dotenv>=1.0.0" >> requirements.txt; \
		echo "$(YELLOW)[requirements] Created default requirements.txt$(NC)"; \
	fi

env: ## Create .env from .env.example (will not overwrite existing)
	@if [ -f $(ENV_FILE) ]; then \
		echo "$(YELLOW)[env] $(ENV_FILE) already exists — not overwriting.$(NC)"; \
	else \
		if [ -f .env.example ]; then \
			cp .env.example $(ENV_FILE); \
			echo "$(GREEN)[env] Created $(ENV_FILE) from .env.example$(NC)"; \
			echo "$(RED)[env] Edit $(ENV_FILE) and fill in your real credentials.$(NC)"; \
		else \
			echo "$(RED)[env] .env.example not found — create it first.$(NC)"; \
			exit 1; \
		fi; \
	fi

# ---------- Running ----------
run: install ## Run the bot once (normal mode)
	@echo "$(GREEN)[run] Starting bot...$(NC)"
	@mkdir -p $(LOG_DIR)
	@$(VENV_PY) $(SCRIPT)

run-verbose: install ## Run the bot with DEBUG logging
	@mkdir -p $(LOG_DIR)
	PYTHONUNBUFFERED=1 LOG_LEVEL=DEBUG $(VENV_PY) $(SCRIPT)

run-bg: install ## Run the bot in background, output to logs/
	@mkdir -p $(LOG_DIR)
	@nohup $(VENV_PY) $(SCRIPT) > $(LOG_DIR)/run-$(shell date +%Y%m%d-%H%M%S).log 2>&1 &
	@echo "$(GREEN)[run-bg] Started in background. Logs in $(LOG_DIR)/$(NC)"

# ---------- Testing & Quality ----------
test: install-dev ## Run test suite
	@echo "$(GREEN)[test] Running tests...$(NC)"
	@$(VENV)/bin/pytest -v tests/ 2>/dev/null || \
		echo "$(YELLOW)[test] No tests/ directory yet — create one to enable tests.$(NC)"

lint: install-dev ## Lint with ruff
	@echo "$(GREEN)[lint] Linting...$(NC)"
	@$(VENV)/bin/ruff check $(SCRIPT) || true

format: install-dev ## Auto-format with black
	@echo "$(GREEN)[format] Formatting...$(NC)"
	@$(VENV)/bin/black $(SCRIPT) || true

check: ## Quick sanity checks: syntax, .env present, imports resolve
	@echo "$(BLUE)[check] Python syntax...$(NC)"
	@$(PYTHON) -m py_compile $(SCRIPT) && echo "  $(GREEN)✓ syntax OK$(NC)"
	@echo "$(BLUE)[check] .env presence...$(NC)"
	@if [ -f $(ENV_FILE) ]; then \
		echo "  $(GREEN)✓ $(ENV_FILE) found$(NC)"; \
	else \
		echo "  $(RED)✗ $(ENV_FILE) missing — run 'make env'$(NC)"; \
	fi
	@echo "$(BLUE)[check] Required variables in .env...$(NC)"
	@if [ -f $(ENV_FILE) ]; then \
		for var in TELEGRAM_TOKEN TELEGRAM_CHAT_ID; do \
			if grep -q "^$$var=" $(ENV_FILE) 2>/dev/null; then \
				val=$$(grep "^$$var=" $(ENV_FILE) | head -1 | cut -d= -f2-); \
				if [ -z "$$val" ] || echo "$$val" | grep -q "your_"; then \
					echo "  $(RED)✗ $$var still has placeholder value$(NC)"; \
				else \
					echo "  $(GREEN)✓ $$var is set$(NC)"; \
				fi; \
			else \
				echo "  $(RED)✗ $$var not found in $(ENV_FILE)$(NC)"; \
			fi; \
		done; \
	fi

# ---------- Telegram ----------
telegram-test: install ## Send a test message to your Telegram chat
	@echo "$(GREEN)[telegram-test] Sending test message...$(NC)"
	@$(VENV_PY) -c "\
import os, asyncio; \
from pathlib import Path; \
from dotenv import load_dotenv; \
from telegram import Bot; \
load_dotenv(Path('$(ENV_FILE)')); \
async def _t(): \
    bot = Bot(token=os.getenv('TELEGRAM_TOKEN')); \
    await bot.send_message(chat_id=os.getenv('TELEGRAM_CHAT_ID'), text='✅ Test from forex_bot'); \
asyncio.run(_t()); \
print('  $(GREEN)✓ Message sent — check Telegram$(NC)')"

# ---------- Logs ----------
log: ## Show the most recent log file
	@if [ -d $(LOG_DIR) ] && [ "$$(ls -A $(LOG_DIR) 2>/dev/null)" ]; then \
		ls -t $(LOG_DIR)/*.log 2>/dev/null | head -1 | xargs cat; \
	else \
		echo "$(YELLOW)[log] No logs yet. Run 'make run-bg' to generate one.$(NC)"; \
	fi

tail-log: ## Tail the most recent log file
	@if [ -d $(LOG_DIR) ] && [ "$$(ls -A $(LOG_DIR) 2>/dev/null)" ]; then \
		ls -t $(LOG_DIR)/*.log 2>/dev/null | head -1 | xargs tail -f; \
	else \
		echo "$(YELLOW)[tail-log] No logs yet.$(NC)"; \
	fi

# ---------- Backup ----------
backup: ## Backup .env and script to backups/
	@mkdir -p $(BACKUP_DIR)
	@STAMP=$$(date +%Y%m%d-%H%M%S); \
	tar -czf $(BACKUP_DIR)/forex-bot-$$STAMP.tar.gz $(SCRIPT) .env requirements.txt Makefile 2>/dev/null; \
	echo "$(GREEN)[backup] Created $(BACKUP_DIR)/forex-bot-$$STAMP.tar.gz$(NC)"

backup-list: ## List available backups
	@if [ -d $(BACKUP_DIR) ]; then \
		ls -lh $(BACKUP_DIR)/*.tar.gz 2>/dev/null || \
			echo "$(YELLOW)[backup-list] No backups found.$(NC)"; \
	else \
		echo "$(YELLOW)[backup-list] No backups directory.$(NC)"; \
	fi

# ---------- PythonAnywhere ----------
pa-upload: ## Upload script + requirements to PythonAnywhere
	@if [ -z "$(PA_USER)" ] || [ "$(PA_USER)" = "yourusername" ]; then \
		echo "$(RED)[pa-upload] Set PA_USER first: make pa-upload PA_USER=myuser$(NC)"; \
		exit 1; \
	fi
	@echo "$(GREEN)[pa-upload] Uploading to $(PA_HOST)...$(NC)"
	scp $(SCRIPT) requirements.txt .env.example $(PA_USER)@ssh.pythonanywhere.com:$(PA_REMOTE_DIR)/
	@echo "$(GREEN)[pa-upload] Done.$(NC)"
	@echo "$(YELLOW)Remember to create .env on the server if it's missing.$(NC)"

pa-schedule: ## Print instructions for scheduling on PythonAnywhere
	@echo ""
	@echo "$(BLUE)Schedule the bot on PythonAnywhere:$(NC)"
	@echo ""
	@echo "  1. Log in at https://www.pythonanywhere.com/user/$(PA_USER)/tasks_tab/"
	@echo "  2. Create a task running at 22:00 UTC daily with:"
	@echo "       $(GREEN)python3 $(PA_REMOTE_DIR)/$(SCRIPT)$(NC)"
	@echo "  3. Add these environment variables in the task editor:"
	@echo "       TELEGRAM_TOKEN=<your token>"
	@echo "       TELEGRAM_CHAT_ID=<your chat id>"
	@echo ""
	@echo "$(YELLOW)If your free account was created after Jan 15, 2026,$(NC)"
	@echo "$(YELLOW)scheduled tasks are unavailable — use a Flask web app + cron-job.org.$(NC)"
	@echo ""

pa-logs: ## Stream PythonAnywhere task logs
	@if [ -z "$(PA_USER)" ] || [ "$(PA_USER)" = "yourusername" ]; then \
		echo "$(RED)[pa-logs] Set PA_USER first: make pa-logs PA_USER=myuser$(NC)"; \
		exit 1; \
	fi
	ssh $(PA_USER)@ssh.pythonanywhere.com 'tail -f /var/log/$(PA_USER).pythonanywhere.com.task.log'

# ---------- Cleanup ----------
clean: ## Remove __pycache__, .pyc, and temp files
	@echo "$(GREEN)[clean] Cleaning...$(NC)"
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name '*.pyc' -delete 2>/dev/null || true
	@find . -type f -name '*.pyo' -delete 2>/dev/null || true
	@find . -type d -name '.pytest_cache' -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name '.ruff_cache' -exec rm -rf {} + 2>/dev/null || true
	@echo "$(GREEN)[clean] Done.$(NC)"

clean-all: clean ## Also remove venv and logs (destructive)
	@read -p "$(RED)Remove venv/ and logs/? [y/N] $(NC)" ans; \
	if [ "$$ans" = "y" ] || [ "$$ans" = "Y" ]; then \
		rm -rf $(VENV) $(LOG_DIR); \
		echo "$(GREEN)[clean-all] Done.$(NC)"; \
	else \
		echo "$(YELLOW)[clean-all] Aborted.$(NC)"; \
	fi

# ---------- Deploy ----------
deploy-check: check ## Pre-flight checks before deploying
	@echo ""
	@echo "$(BLUE)[deploy-check] Checking for common issues...$(NC)"
	@if grep -q "YOUR_BOT_TOKEN_HERE\|your_bot_token_here" $(SCRIPT) 2>/dev/null; then \
		echo "  $(RED)✗ Hardcoded placeholder token in $(SCRIPT)$(NC)"; \
	else \
		echo "  $(GREEN)✓ No hardcoded token in $(SCRIPT)$(NC)"; \
	fi
	@if [ -f .gitignore ] && grep -q "^\.env$$" .gitignore; then \
		echo "  $(GREEN)✓ .env is in .gitignore$(NC)"; \
	else \
		echo "  $(RED)✗ .env is NOT in .gitignore — add it!$(NC)"; \
	fi
	@if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then \
		if git ls-files --error-unmatch .env >/dev/null 2>&1; then \
			echo "  $(RED)✗ .env is tracked by git — remove it!$(NC)"; \
		else \
			echo "  $(GREEN)✓ .env not tracked by git$(NC)"; \
		fi; \
	fi
	@echo ""
	@echo "$(GREEN)[deploy-check] Pre-flight complete.$(NC)"