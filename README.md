# Knotch

**A coordination engine for N simultaneous voice channels — one AI convener decides, per utterance, which participants need to hear a derived message.**

Most voice AI is one human, one bot. Knotch targets the harder case: many humans who cannot all monitor each other — a kitchen line, a pit crew, an incident bridge, an OR. A single convener process subscribes to every channel, holds shared state across all of them, and routes selectively. Most utterances go nowhere; silence is the common and correct outcome.

The engine is domain-agnostic. Swapping `--domain` loads a different pack (roles, routing policy, eval rubric, scenarios) with no engine code changes.

```bash
pip install -e ".[dashboard]"
CONVENER_LLM=fake python3 -m engine --domain _template --all-scenarios --dashboard
# dashboard → http://localhost:7861
```

For a live LLM backend, copy `.env.example` to `.env`, set your endpoint vars, and drop `CONVENER_LLM=fake`. Demo packs (`kitchen`, `race_ops`) are gitignored; the repo ships the engine and a `_template` pack.

## Architecture

Each participant runs in its own OS process with its own transport. The convener is a separate, transport-less process. All processes communicate only over a shared pub/sub bus. The same code runs single-process (in-memory bus) for development and multi-host (Redis bus) for production.

```
 Participant processes (one per voice channel)
 ┌──────────────────────────────────────────────────────────────────┐
 │  participant_worker  (role_a, WebRTC)                             │
 │    INPUT : transport → STT → turn gate → triage ─┐               │
 │    OUTPUT: TTS (dispatcher voice) ◄───────────┐  │               │
 │  participant_worker  (role_b, telephony)       │  │               │
 │    INPUT : transport → STT → turn gate → triage─┤               │
 │    OUTPUT: TTS ◄──────────────────────────────┘  │               │
 └──────────────────────────────────┬────────────────┼──────────────┘
                         bus:routed │                │ bus:utterances
                                    ▼                ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │  BUS  —  one Bus protocol, two backends                           │
 │    InMemoryBus  (CONVENER_BUS=memory, single process)             │
 │    RedisBus     (CONVENER_BUS=redis, multi-host)                  │
 │  channels: utterances · routed · state · eval · curves · system  │
 └────────────────────┬────────────────────┬────────────────────────┘
                      ▼                    ▼
      ┌───────────────────────┐  ┌────────────────────────────┐
      │  proc_convener        │  │  dashboard (port 7861)      │
      │  LLM routing loop     │  │  FastAPI + WebSocket        │
      │  utterances → routed  │  │  read-only bus subscriber   │
      └───────────────────────┘  └────────────────────────────┘
```

### Processing path

Each participant runs two async loops (`engine/voice_worker.py`):

**Input** — `transport → STT → TurnGate → triage → bus:utterances`
- `TurnGate` accumulates interim STT chunks and publishes only on turn-stop. A semantic VAD model can replace the simple gate behind the same interface via `CONVENER_TURN`.
- `triage` drops backchannels ("yeah", "ok") without hitting the LLM, and tags a coarse class on routable turns.

**Convene** — `bus:utterances → LLM.decide → validate_decision → bus:routed + bus:state`
- The convener holds a rolling state digest (last class per participant, turn count) and a recent-utterance window fed to the LLM on every turn.
- `validate_decision` coerces the raw LLM output to the contract: `recipients ⊆ participants`, source removed from recipients, empty recipients forces empty message. Returns a held decision on any malformed input; never raises.

**Output** — `bus:routed → self_select → TTS`
- Every participant subscribes to `bus:routed` and filters by its own `role_id`. Addressing is deterministic — no second LLM call.

All messages are JSON `Envelope`s; the type-to-channel mapping lives in `engine/interfaces.py`.

## Domain packs

No domain-specific string appears anywhere under `engine/` — a CI test (`tests/test_no_domain_strings.py`) enforces this. All domain knowledge is in a pack directory, `domains/<name>/`:

| File | Purpose |
|---|---|
| `pack.yaml` | Roles (`role_id`, `display_name`, `voice_id`), dispatcher voice |
| `convener.md` | Routing policy fragment appended verbatim to the convener system prompt |
| `rubric.yaml` | Eval metrics, layered on the base rubric (`acted_on`, `missed`, `ignored`, `time_to_action`) |
| `scenarios.yaml` | Scripted utterances with `expect` labels for eval and replay |

New domain — four files, no code:

```bash
cp -r domains/_template domains/myapp
# edit the four files
CONVENER_LLM=fake python3 -m engine --domain myapp --all-scenarios --dashboard
```

## Evaluation and self-improvement

**Eval** (`engine/eval_runner.py`) replays scenarios through an isolated `ConvenerWorker` and scores each routing decision against `expect` labels: `acted` (correct route or correct hold), `ignored` (routed to unexpected recipients), `missed` (expected route not delivered). Aggregates misroute rate, missed rate, and average time-to-action.

**Self-improvement** (`engine/improve.py`) runs an eval → reflect → rewrite loop: score the current policy, ask the LLM to revise the `convener.md` fragment based on failures, re-score, keep the best over N rounds. The best fragment is written to `convener.revised.md`; `--apply` overwrites the original.

```bash
python3 -m engine.proc_improve --domain _template --rounds 3           # local scoring
python3 -m engine.proc_improve --domain _template --rounds 3 --cekura  # Cekura judges
```

Eval uses the factory-selected LLM (`CONVENER_LLM=fake` runs offline). The rewrite step calls the LLM endpoint directly, so it requires `CONVENER_LLM_URL` to be set.

## Running

```bash
# Offline demo (no external services)
pip install -e ".[dashboard]"
CONVENER_LLM=fake python3 -m engine --domain _template --all-scenarios --dashboard

# Real LLM backend
cp .env.example .env  # fill in endpoint vars
pip install -e ".[dashboard,llm]"
python3 -m engine --domain _template --all-scenarios --dashboard

# Distributed stack (Redis, real transports)
bash scripts/run_stack.sh
# stop with: bash scripts/stop_stack.sh
```

Flags: `--scenario <id>`, `--list-scenarios`, `--record run.jsonl`, `--dashboard-port`, `--step-delay`.

## Configuration

Copy `.env.example` to `.env`. The offline demo needs none of these.

| Variable | Purpose |
|---|---|
| `CONVENER_BUS` | `memory` (default, single-process) or `redis` (multi-host) |
| `REDIS_URL` | Required when `CONVENER_BUS=redis` |
| `CONVENER_LLM` | `fake` (offline) or `nemotron` (any OpenAI-compatible endpoint) |
| `CONVENER_LLM_URL` | LLM routing endpoint (any OpenAI-compatible URL — Ollama, Together, NIM, etc.). Required when `CONVENER_LLM=nemotron`. |
| `CONVENER_LLM_MODEL` | Model name to pass to the endpoint. Required when `CONVENER_LLM=nemotron`. |
| `CONVENER_STT` | `fake` or `nvidia` (live ASR) |
| `CONVENER_TTS` | `fake` or `gradium` (live dispatcher voice) |
| `CONVENER_TRANSPORT` | `fake` or `daily` (WebRTC) |

Live STT, TTS, and transport run in the `.starter/server/` participant workers. The engine's in-process path uses fakes for all three; only the LLM is real by default.

## Layout

```
engine/
  interfaces.py       bus channels, envelope types, service Protocols (single source of truth)
  bus.py              InMemoryBus
  redis_bus.py        RedisBus (same protocol)
  turn_gate.py        chunk accumulation → turn-stop
  triage.py           backchannel drop + coarse class tagging
  convener_worker.py  routing loop: utterances → decision + state
  routing.py          validate_decision + self_select
  voice_worker.py     per-participant input + output loops
  eval_runner.py      scenario replay and scoring
  improve.py          eval → reflect → rewrite loop
  cekura_score.py     Cekura-backed scoring source
  packloader.py       pack loader + system prompt assembly
  __main__.py         single-process runner
  proc_convener.py    convener as a standalone process
  proc_inject.py      scenario injector (distributed)
  proc_improve.py     improvement loop as a process
  proc_curves.py      replay saved improvement curves to the dashboard
  dashboard/          FastAPI + WebSocket live dashboard
adapters/
  factory.py          env-driven service selection
  fake_*.py           offline STT / TTS / transport / LLM
  nemotron_llm.py     LLM adapter (OpenAI-compatible endpoint)
domains/              pack directories (_template ships; others are local)
prompts/              generic scaffold + base rubric
.starter/server/      Pipecat participant workers (Daily + Twilio + NVIDIA STT). This is the
                      hackathon starter adapted to publish utterances to the engine's Redis bus;
                      not required for the offline demo.
tests/                generality gate + routing-contract tests
scripts/              stack management and integration tests
```

## Tests

```bash
python3 -m pytest
```

- `tests/test_no_domain_strings.py` — no domain literals under `engine/`
- `tests/test_routing_contract.py` — `validate_decision` contract and never-raise guarantee

---

---

## Hackathon stack: Nemotron, Cekura, Pipecat

### Nemotron

Nemotron does two distinct jobs in Knotch:

**Routing.** Every routable utterance produces one LLM call (`adapters/nemotron_llm.py`). The prompt gives the model the active participant roster, shared state, recent utterance window, and the current turn; it must return a single JSON object specifying recipients, a derived message, urgency, and rationale. The model never sees raw audio — only the turn-complete, pre-triaged text.

**Policy rewriting.** The self-improvement loop (`engine/improve.py`) uses the same endpoint as an optimizer: given the current routing-policy fragment and a list of rubric-cited failures, it rewrites the `convener.md` fragment to reduce those failures. Open-weights optimizing open-weights — the same model routes and improves its own routing policy.

### Cekura

Cekura is wired into the self-improvement loop as an optional scoring source (`engine/cekura_score.py`, `proc_improve --cekura`).

The local eval runner replays scenarios through the convener and produces ground-truth routing decisions. Knotch then renders those decisions as Cekura-format transcripts (Testing Agent = utterances, Main Agent = routing decisions), ingests one call log per scenario, and triggers evaluation against `llm_judge` metrics auto-created from the domain's own rubric. When results land, Cekura's per-metric explanations are attached to each failing record and fed verbatim into the reflection prompt — so the rewrite step cites the judge's reasoning, not just the failure flag.

The net effect: Cekura's LLM judges replace the local rubric scorer for the "what went wrong and why" step, producing richer, more targeted feedback for the rewrite.

### Pipecat

The participant workers (`.starter/server/`) are built on Pipecat (`pipecat-ai==1.3.0`, `nvidia-pipecat`). Pipecat handles the real-time audio pipeline — Daily WebRTC transport for browser participants, Twilio media streams for phone participants, NVIDIA Parakeet for STT. The Pipecat workers connect to the engine's Redis bus as standard participants: they publish utterances and subscribe to routing decisions, identical to any other transport.

---

## What's new

Everything in this repo was built during the hackathon, with one exception: the dashboard frontend (`engine/dashboard/static/index.html`) reuses a prior UI. Specifically:

- The convener architecture (pub/sub bus, transport-less routing process, domain pack model)
- `engine/` — all modules: routing, eval, self-improvement, packloader, bus backends
- `adapters/` — Nemotron LLM adapter, factory pattern
- Cekura integration (`engine/cekura_score.py`) — self-improvement loop with LLM judge scoring
- `.starter/server/` participant workers — Daily, Twilio, NVIDIA STT wiring
- The two-stage input gate (TurnGate + triage) and the fail-safe routing contract

---

## Tool feedback

### Nemotron

**What worked well.** JSON output reliability was high — with a strict schema in the system prompt the model consistently returned parseable objects, even across many consecutive calls. Latency was acceptable for a routing use case where decisions are sequential per participant; sub-second responses kept the coordination loop feeling real-time.

**What could be better.** The model occasionally returns recipient IDs that are plausible-sounding but not in the provided participant list (e.g. inventing a `role_manager` when only `role_a` and `role_b` exist). The engine's `validate_decision` catches and drops these, but it means the model isn't reliably grounding to the exact roster it was given. Stronger few-shot examples or a constrained decoding step for the `recipients` field would improve this.

The dual use of the same endpoint for routing (latency-sensitive, many calls) and policy rewriting (quality-sensitive, rare calls) works, but a dedicated fine-tuned variant for structured routing would meaningfully raise routing precision.

### Cekura

**What worked well.** Programmatic metric creation from a rubric was clean — `llm_judge` metrics created via API, reused across improvement rounds, never duplicated. The transcript ingestion format was straightforward once understood.

**What could improve.** Two friction points:

1. **Polling lag.** After triggering `evaluate_metrics`, results could take 60–90 seconds to land on the call log. The self-improvement loop has to poll with a timeout, and a round blocks until results arrive. A webhook or SSE completion signal would make this significantly less brittle.

2. **Self-improvement loop pattern is undocumented.** Using Cekura as an in-loop judge (ingest → evaluate → read explanations → feed to reflect step → repeat) required figuring out the API flow from scratch. The individual endpoints are documented, but the pattern of "use Cekura scores as feedback for prompt optimization" has no worked example. A reference implementation or guide for this use case would reduce the integration time considerably.