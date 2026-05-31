# syntax=docker/dockerfile:1
#
# Convener PARTICIPANT worker — Pipecat Cloud image.
#
# IMPORTANT: build context is the REPO ROOT, not .starter/server, because the
# worker imports the engine from two levels up (engine/, adapters/, domains/,
# prompts/) and resolves them via Path(__file__).parents[1]. To keep that
# resolution working inside the container, the repo subtree is laid out under
# /app exactly as on disk: /app/.starter/server/bot.py with /app/engine, etc.
#
# Build (ARM64 is REQUIRED by Pipecat Cloud) FROM THE REPO ROOT:
#
#   docker buildx build --platform=linux/arm64 \
#     -f .starter/server/Dockerfile \
#     -t <user>/convener-worker:0.1 --load .
#
# The dailyco/pipecat-base image owns the runner: its /app/app.py discovers
# /app/bot.py and calls `await bot(runner_args)` once per session. Python 3.12.
#
FROM dailyco/pipecat-base:latest

WORKDIR /app

# 1) Python deps. Copy only the requirements first for layer caching.
COPY .starter/server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /app/requirements.txt

# 2) The pipecat-free engine subtree the worker imports (parents[1] == /app).
#    Keep this minimal: only what `import engine.* / adapters.*` and the pack
#    loader need at runtime.
COPY engine/   /app/engine/
COPY adapters/ /app/adapters/
COPY domains/  /app/domains/
COPY prompts/  /app/prompts/

# 3) The server package: bot.py (entrypoint), the worker it reuses, the STT
#    service, and any sibling modules bot.py imports. Placed at the SAME
#    relative path as on disk so parents[1] resolves to /app.
COPY .starter/server/bot.py          /app/.starter/server/bot.py
COPY .starter/server/worker_daily.py /app/.starter/server/worker_daily.py
COPY .starter/server/nvidia_stt.py   /app/.starter/server/nvidia_stt.py

# 4) The base image launches `python app.py` from /app, which discovers a
#    `bot` callable in /app/bot.py. We load the REAL entrypoint from
#    .starter/server/bot.py BY PATH under a distinct module name so its
#    parents[1] math (== /app) is unchanged and there is no name clash with
#    this shim. Importing the file by an explicit name avoids the circular
#    self-import that a bare `from bot import bot` would cause.
RUN { \
      echo 'import importlib.util as _u, sys as _s'; \
      echo 'from pathlib import Path as _P'; \
      echo '_src = _P(__file__).parent / ".starter" / "server" / "bot.py"'; \
      echo '_s.path.insert(0, str(_src.parent))'; \
      echo '_spec = _u.spec_from_file_location("convener_bot_impl", _src)'; \
      echo '_mod = _u.module_from_spec(_spec)'; \
      echo '_s.modules["convener_bot_impl"] = _mod'; \
      echo '_spec.loader.exec_module(_mod)'; \
      echo 'bot = _mod.bot'; \
    } > /app/bot.py
