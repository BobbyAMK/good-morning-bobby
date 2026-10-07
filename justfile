set shell := ["bash", "-uceo", "pipefail"]

repo := justfile_directory()
home := env_var('HOME')
uid  := `id -u`
venv := repo / ".venv"
# What launchd actually runs. Outside ~/Documents on purpose -- see `deploy`.
runtime := home / "Library/Application Support/pet-agents/runtime"

default:
    @just --list

# ---------------------------------------------------------------- dev
sync:
    uv sync --all-packages

lint:
    uv run ruff check . && uv run ruff format --check .

fix:
    uv run ruff check --fix . && uv run ruff format .

test:
    uv run pytest                      # 'live' tests excluded via pyproject addopts

test-live:
    uv run pytest -m live              # hits real Ollama / public sources / Telegram

# ---------------------------------------------------------------- one-time setup
telegram-token:
    {{venv}}/bin/pa-core-admin telegram-token

telegram-bind:
    {{venv}}/bin/pa-core-admin telegram-bind

telegram-test:
    {{venv}}/bin/pa-core-admin telegram-test

pull-models:
    ollama pull qwen2.5:7b-instruct-q4_K_M

# BELT 1 for the 6am problem: without this, a closed-lid MacBook may not run the job
# until you open it, and a brief at 2pm is not a morning brief.
wake:
    sudo pmset repeat wakeorpoweron MTWRFSU 05:58:00
    pmset -g sched

unwake:
    sudo pmset repeat cancel
    pmset -g sched

# ---------------------------------------------------------------- run by hand
brief-dry:
    {{venv}}/bin/pa-morning-brief run --dry-run

brief:
    {{venv}}/bin/pa-morning-brief run --force

show:
    {{venv}}/bin/pa-morning-brief show

doctor:
    {{venv}}/bin/pa-core-admin doctor

bench:
    {{venv}}/bin/pa-core-admin bench

cost:
    {{venv}}/bin/pa-core-admin cost

runs:
    {{venv}}/bin/pa-core-admin runs

# ---------------------------------------------------------------- launchd
# Use bootstrap/bootout/kickstart -- the modern verbs. `load -w`/`unload` are
# deprecated and quietly different. plutil -lint first, because an XML typo otherwise
# manifests as "my job just doesn't run".
# launchd cannot read ~/Documents (macOS TCC), and the repo's .venv is editable -- its
# .pth files point back into the repo. So scheduled jobs run a NON-editable copy here.
# Code changes reach the 6am job only after a redeploy; `install` does it for you.
deploy:
    mkdir -p "{{runtime}}"
    # --reinstall-package: workspace packages keep version 0.1.0, so without it uv reuses the
    # wheel it built on the FIRST deploy and code changes never reach launchd (measured).
    UV_PROJECT_ENVIRONMENT="{{runtime}}/venv" uv sync --all-packages --no-editable --no-dev --frozen --quiet \
        --reinstall-package pa-core --reinstall-package pa-morning-brief
    install -m 755 "{{repo}}/ops/run.sh" "{{runtime}}/run.sh"
    cp "{{repo}}/.env" "{{runtime}}/.env"
    git -C "{{repo}}" describe --always --dirty > "{{runtime}}/VERSION"
    # Prove it: every source file must be byte-identical in the deployed venv.
    cd "{{repo}}/packages" && for f in */src/*/*.py */src/*/*/*.py */src/*/*/*/*.sql; do \
        [ -f "$f" ] || continue; rel="${f#*/src/}"; \
        cmp -s "$f" "{{runtime}}/venv/lib/python3.14/site-packages/$rel" || { echo "STALE after deploy: $rel"; exit 1; }; \
    done
    @echo "deployed $(cat '{{runtime}}/VERSION') -> {{runtime}}"

install agent: deploy
    mkdir -p "{{home}}/Library/LaunchAgents" "{{home}}/Library/Logs/pet-agents"
    sed -e 's|@@RUNTIME@@|{{runtime}}|g' -e 's|@@HOME@@|{{home}}|g' \
        "{{repo}}/ops/launchd/com.pa.{{agent}}.plist.tmpl" \
        > "{{home}}/Library/LaunchAgents/com.pa.{{agent}}.plist"
    plutil -lint "{{home}}/Library/LaunchAgents/com.pa.{{agent}}.plist"
    launchctl bootout gui/{{uid}}/com.pa.{{agent}} 2>/dev/null || true
    launchctl bootstrap gui/{{uid}} "{{home}}/Library/LaunchAgents/com.pa.{{agent}}.plist"
    @echo "installed com.pa.{{agent}}"

uninstall agent:
    launchctl bootout gui/{{uid}}/com.pa.{{agent}} 2>/dev/null || true
    rm -f "{{home}}/Library/LaunchAgents/com.pa.{{agent}}.plist"
    @echo "removed com.pa.{{agent}}"

# Test a 6am job at 3pm.
kick agent:
    launchctl kickstart -k gui/{{uid}}/com.pa.{{agent}}

status agent:
    launchctl print gui/{{uid}}/com.pa.{{agent}} | sed -n '1,25p'

logs agent:
    tail -n 100 -f "{{home}}/Library/Logs/pet-agents/{{agent}}.err.log"

install-brief: (install "morning-brief") (install "watchdog") (install "bot")

# The always-on Telegram bot (reply-to-save, /search).
bot-logs:
    tail -n 100 -f "{{home}}/Library/Logs/pet-agents/bot.err.log"

# Run the bot in the foreground from the repo, for development. Stop the launchd one
# first or the two will fight over getUpdates (409 Conflict).
bot-dev:
    launchctl bootout gui/{{uid}}/com.pa.bot 2>/dev/null || true
    {{venv}}/bin/pa-bot
