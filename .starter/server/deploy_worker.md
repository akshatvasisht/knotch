# Deploy the Coordinator participant worker to Pipecat Cloud (optional Step-5 flex)

> **This is OPTIONAL and NEVER load-bearing for the demo.** The real path is the
> local distributed stack (`run_stack.sh` / `worker_daily.py`). This deploys the
> *participant* voice worker as a session-oriented Pipecat Cloud agent that
> connects OUT to the SAME Upstash Redis bus, so a cloud participant and the
> off-cloud coordinator (`engine.proc_coordinator`) meet on the same fabric.

All commands/keys below were **verified against the live `pipecat-ai-cli`
v1.3.0** (`pipecat cloud deploy --help`, `pipecat cloud secrets set --help`) and
[docs.pipecat.ai → Pipecat Cloud](https://docs.pipecat.ai/deployment/pipecat-cloud/introduction)
on 2026-05-30. The CLI accepts either `pipecat` or `pcc` as the prefix.

## What gets deployed (and what does NOT)

- **Deployed:** the participant worker (`bot.py`, an adapter over
  `worker_daily.py`). Pipecat Cloud is session-oriented: **one started agent ==
  one Daily session == one transport-bound bot.** Pipecat Cloud provisions the
  Daily room + token and hands them to `async def bot(runner_args)`; the worker
  does **not** self-create a room in the cloud (unlike `worker_daily.py`).
- **NOT deployed:** the coordinator and the dashboard. They are transport-less
  Redis subscribers — run them off-cloud (e.g. on your laptop / a small VM)
  pointed at the same `REDIS_URL`. The deployed worker reaches them over the
  shared Upstash bus.

## Files (all under `.starter/server/`)

| File | Purpose |
|------|---------|
| `bot.py` | Pipecat Cloud entrypoint, `async def bot(runner_args)`; reuses `worker_daily.BusBridge`/`build_tts` + `nvidia_stt`. |
| `Dockerfile` | ARM64 image on `dailyco/pipecat-base:latest`; **build context = repo root** so `engine/adapters/domains/prompts` are present for `import engine.*`. |
| `requirements.txt` | Worker deps (pipecat-ai extras, redis, websockets, soxr, numpy, …). |
| `pcc-deploy.toml` | `agent_name`, `image`, `secret_set`, `[scaling] min_agents/max_agents`. |
| `.dockerignore` | Keeps `.env`/secrets out of the image. |

## Prerequisites

```bash
# Pipecat CLI (modern; installs `pipecat` and `pcc`).
uv tool install pipecat-ai-cli      # -> pipecat 1.3.0

# Docker with buildx + ARM64 emulation (Pipecat Cloud REQUIRES linux/arm64).
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker buildx create --name pcc-arm --driver docker-container --bootstrap
```

## Step 1 — AUTH (INTERACTIVE — you must run this; opens a browser)

```bash
pipecat cloud auth login          # opens a browser; cannot be done headlessly
pipecat cloud organizations list  # confirm you are logged in to an org
```
Non-interactive (CI) alternative: create an org API key once with
`pipecat cloud organizations keys create`, then export it as
`PIPECAT_CLOUD_API_KEY` (a.k.a. `PCC_API_KEY`) so subsequent commands skip the
browser. See `pipecat cloud organizations keys --help`.

## Step 2 — Secrets (NOT baked into the image)

Create a `pcc.env` (do **not** commit it) with the cloud worker's runtime env:

```dotenv
# The shared bus — point at your Upstash instance (rediss:// with TLS).
REDIS_URL=rediss://default:<PASSWORD>@<your-db>.upstash.io:6379
KNOTCH_BUS=redis
KNOTCH_DASHBOARD=off          # cloud worker must NOT host a dashboard

# Which role/domain this agent plays (one deploy per role, or pass in body).
KNOTCH_ROLE=role_grill
KNOTCH_DOMAIN=kitchen

# STT — NVIDIA ASR WebSocket endpoint.
NVIDIA_ASR_URL=<your-asr-websocket-url>

# Coordinator LLM endpoint (used by the off-cloud coordinator; harmless here).
KNOTCH_LLM_URL=<your-openai-compatible-llm-url>
KNOTCH_LLM_MODEL=<your-model-name>
KNOTCH_LLM_THINKING=false

# TTS — Gradium (omit GRADIUM_API_KEY to fall back to the no-audio StubTTS).
GRADIUM_API_KEY=<your-gradium-key>
GRADIUM_VOICE_ID=KWJiFWu2O9nMPYcR
KNOTCH_VOICE_TTS=gradium

# DAILY_API_KEY is NOT required in cloud (Pipecat Cloud owns the room).
```

Upload it as the secret set referenced by `pcc-deploy.toml`:

```bash
pipecat cloud secrets set coordinator-worker-secrets --file pcc.env
```

## Step 3 — Build + push the ARM64 image

Edit `pcc-deploy.toml` and set `image = "<your-dockerhub-username>/coordinator-worker:0.1"`.

**Option A — manual (proven locally; build context is the repo root):**
```bash
# Run from the repo root:
docker buildx build --builder pcc-arm --platform=linux/arm64 \
  -f .starter/server/Dockerfile \
  -t <your-dockerhub-username>/coordinator-worker:0.1 \
  --push .
```

**Option B — CLI helper** (reads `pcc-deploy.toml`; run from `.starter/server/`,
but note it builds with the Dockerfile's context — keep the `-f`/context model
above if the helper assumes a local context):
```bash
pipecat cloud docker build-push coordinator-worker \
  --registry dockerhub --username <your-dockerhub-username> --version 0.1
```

## Step 4 — Deploy

```bash
# From .starter/server/ (uses pcc-deploy.toml by default).
pipecat cloud deploy            # reads agent_name/image/secret_set/scaling

# …or fully explicit (overrides the toml):
pipecat cloud deploy coordinator-worker <your-dockerhub-username>/coordinator-worker:0.1 \
  --secrets coordinator-worker-secrets --min-agents 1 --max-agents 3
```

Check status / logs:
```bash
pipecat cloud agent list
pipecat cloud agent logs coordinator-worker
```

## Step 5 — Run the off-cloud coordinator against the SAME bus

On your laptop / VM (NOT on Pipecat Cloud), with the SAME `REDIS_URL`:
```bash
KNOTCH_BUS=redis REDIS_URL=rediss://… uv run python -m engine.proc_coordinator --domain kitchen
# optional: KNOTCH_BUS=redis REDIS_URL=… python -m engine.dashboard --domain kitchen --live
```

## Step 6 — Start a session (binds the agent to a Daily room)

Pipecat Cloud creates the room and invokes `bot(runner_args)`:
```bash
curl -X POST https://api.pipecat.daily.co/v1/public/coordinator-worker/start \
  -H "Authorization: Bearer $PIPECAT_CLOUD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"createDailyRoom": true, "body": {"role": "role_grill", "domain": "kitchen"}}'
```
(`body.role` / `body.domain` override the `KNOTCH_ROLE` / `KNOTCH_DOMAIN`
secrets; the response includes the room URL a human opens on a phone.) For N
participants, deploy one agent per role (e.g. `coordinator-worker-grill`,
`coordinator-worker-fry`) or start N sessions each with a distinct `body.role`.

## Notes / caveats

- **ARM64 is mandatory.** The image was built and smoke-tested locally under
  qemu emulation (`linux/arm64`, `aarch64`); the entrypoint and `import
  engine.*` resolve inside the container.
- **Build context = repo root.** The Dockerfile copies `engine/ adapters/
  domains/ prompts/` plus `.starter/server/{bot,worker_daily,nvidia_stt}.py`,
  laid out under `/app/.starter/server/` so `parents[1] == /app` keeps the
  worker's `import engine.*` working. `/app/bot.py` is a tiny shim that loads
  the real entrypoint by path under a distinct module name.
- **Secrets stay out of the image** (`.dockerignore` excludes `.env`; runtime
  env comes only from the Pipecat Cloud secret set).
- The exact REST `start` host/path may vary by org/region — confirm with
  `pipecat cloud agent --help` and the dashboard after deploy.
```
