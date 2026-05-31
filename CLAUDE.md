# CLAUDE.md

Agent context for working in the **Knotch** repo. Read `README.md` for the full
system design; this file is the working map and the invariants you must not break.

## What this is

A domain-agnostic **one-to-many-to-one** voice coordination engine. One
transport-less *convener* process subscribes to N voice-participant processes over
a shared pub/sub bus, holds cross-channel state, and emits one addressed routing
decision per utterance. Python 3.12+, asyncio, stdlib + PyYAML at the core.

## Architecture map

The live path is three hops, each in its own module:

```
voice_worker (input)  → bus:utterances → convener_worker → bus:routed → voice_worker (output)
  STT→TurnGate→triage                     LLM.decide                      self_select→TTS
                                          →validate_decision
                                          →bus:state
```

- `engine/interfaces.py` — **the single source of truth.** Bus channels, envelope
  types, payload dataclasses (`Utterance`, `RoutingDecision`, `StateUpdate`,
  `EvalScore`, `TurnEvent`), domain-pack value objects, and the service
  `Protocol`s (`Bus`, `Transport`, `STTService`, `TTSService`, `ConvenerLLM`).
  Change wire format or contracts here, nowhere else.
- `engine/convener_worker.py` — the brain loop; holds the rolling state digest.
- `engine/routing.py` — `validate_decision` (fail-safe coercion) + `self_select`.
- `engine/voice_worker.py` — one participant's input + output loops.
- `engine/bus.py` / `engine/redis_bus.py` — the two `Bus` backends.
- `engine/eval_runner.py` + `engine/cekura_score.py` — scenario scoring.
- `engine/improve.py` — eval → reflect → patch self-improvement loop.
- `engine/packloader.py` — loads `domains/<name>/` and assembles the system prompt.
- `adapters/factory.py` — env-driven fake-vs-real service selection.
- `adapters/nemotron_llm.py` — the real convener LLM (any OpenAI-compatible endpoint).
- `engine/proc_*.py` — process entry points for distributed mode.
- `engine/dashboard/` — read-only FastAPI + WebSocket bus subscriber (port 7861).

## Invariants — do not break these

1. **Generality gate.** No domain-specific literal (role names, station names,
   signal vocabulary) may appear anywhere under `engine/`. Domain knowledge lives
   only in `domains/<name>/`. Enforced by `tests/test_no_domain_strings.py`.
2. **Fail-safe routing.** `validate_decision` must never raise and must never
   trust the LLM: unknown recipients dropped, source never a recipient, malformed
   input → held decision. Covered by `tests/test_routing_contract.py`.
3. **One Bus protocol.** `InMemoryBus` and `RedisBus` are interchangeable; new bus
   features go through the `Bus` Protocol so both stay swappable by env var.
4. **Backends via env, not code.** Build services through `adapters/factory.py`
   (`CONVENER_BUS|LLM|STT|TTS|TRANSPORT|TURN`); defaults in
   `interfaces.DEFAULT_BACKEND`. Don't hardcode a backend in an entry point.

## Commands

```bash
pip install -e ".[dashboard]"                          # core + dashboard
bash scripts/demo_local.sh                             # offline demo (no keys, no external services)
python3 -m engine.proc_improve --domain _template --rounds 3  # self-improvement (needs CONVENER_LLM_URL)
bash scripts/run_stack.sh                              # distributed (Redis): convener + dashboard + Daily + Twilio
python3 -m pytest                                      # tests
```

`bash scripts/demo_local.sh` is the only fully offline path (stdlib + PyYAML + the dashboard
extra). The default `nemotron` backend needs `CONVENER_LLM_URL` + `CONVENER_LLM_MODEL` + the `.[llm]` extra (`pip install -e ".[llm]"`); the
self-improvement loop always calls that endpoint to rewrite the fragment. Only
`domains/_template/` ships in the repo; create a domain by copying it to
`domains/<name>/` — packs other than `_template` are gitignored (local-only).

## Gotchas

- **`adapters/factory.py` only wires real LLM and bus.** `make_stt`/`make_tts`/
  `make_transport` only support `backend=fake`; any other value raises
  `NotImplementedError`. Real STT/TTS/WebRTC/telephony lives in the
  `.starter/server/` workers. Don't assume the factory gives you live audio.
- **`domains/<name>/convener.md` is injected verbatim** into the system prompt by
  `packloader.assemble_system_prompt`. Keep it clean prose — anything in the file
  reaches the model.
- **Self-improvement.** `improve.improve_once` / `improve_rounds` return the best
  fragment and always write a `domains/<name>/convener.revised.md` sibling;
  `proc_improve --apply` overwrites the live `convener.md`, otherwise the original
  is untouched. It does not hot-swap an already-running convener process.
- **Naming.** The project is `knotch`; the core component and env vars are
  "convener" / `CONVENER_*`. Both are intentional product names — keep them.

## Conventions

- Comments and docstrings are professional and minimal: standard terminology, no
  references to the development process (no "TODO later", "Phase N", "WIP",
  snapshot/commit notes), no verbose restatement of the code.
- Engine code is domain-free; put anything domain-specific in a pack.
- All I/O is async; services are duck-typed against the `interfaces.py` Protocols.
