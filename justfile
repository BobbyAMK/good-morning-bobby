set shell := ["bash", "-uceo", "pipefail"]

repo := justfile_directory()
home := env_var('HOME')
uid  := `id -u`
venv := repo / ".venv"

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
    uv run pytest -m live              # hits real Ollama / Google / Telegram

# ---------------------------------------------------------------- one-time setup
# Download the Desktop OAuth client JSON from Google Cloud Console first, and set the
# app's publishing status to "In production" -- in "Testing" the refresh token dies
# every 7 days.
google-client file:
    {{venv}}/bin/pa-core-admin google-client --file "{{file}}"

google-login:
    {{venv}}/bin/pa-core-admin google-login

telegram-token:
    keyring set pet-agents telegram_token

telegram-bind:
    {{venv}}/bin/pa-core-admin telegram-bind

telegram-test:
    {{venv}}/bin/pa-core-admin telegram-test

# bge-m3 is only needed for project 6 (second brain); skip it for now.
pull-models:
    ollama pull qwen2.5:7b-instruct-q4_K_M
    ollama pull qwen2.5:3b-instruct

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
install agent:
    mkdir -p "{{home}}/Library/LaunchAgents" "{{home}}/Library/Logs/pet-agents"
    sed -e 's|@@REPO@@|{{repo}}|g' -e 's|@@HOME@@|{{home}}|g' \
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

install-p1: (install "morning-brief") (install "watchdog")
