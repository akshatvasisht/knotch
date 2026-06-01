# CLAUDE.md

Agent context for the **Knotch** repo. Read `README.md` for the full system
design; this file covers the architecture map, invariants, gotchas, and working
conventions you need to make changes safely.

## What this is

A domain-agnostic, one-to-many-to-one voice coordination engine. One
transport-less coordinator process subscribes to N voice-participant processes over
a shared pub/sub bus, holds cross-channel state, and emits one addressed routing
decision per utterance. Most utterances route to nobody; silence is the common,
correct outcome. Python 3.12+, asyncio, stdlib plus PyYAML at the core. Swapping
`--domain` loads a different pack (roles, routing policy, eval rubric,
scenarios) with no engine change.

## Architecture map

The hot path is three hops, each in its own module:

```
voice_worker (input)  → bus:utterances → coordinator → bus:routed → voice_worker (output)
  STT→TurnGate→triage                     LLM.decide                      self_select→TTS
                                          →validate_decision
                                          →bus:state
```

README's Project layout has the full file map. The anchors that matter when
changing code:

- `engine/interfaces.py` is the single source of truth for the wire format and
  contracts: bus channels, envelope and payload dataclasses, domain-pack value
  objects, and the service `Protocol`s. Change them here, nowhere else.
- `engine/coordinator.py` holds the routing loop, the rolling state digest, and the recent-utterance window fed to the LLM each turn.
- `engine/routing.py` holds `validate_decision` (fail-safe coercion) and `self_select`.

### Processing path

Two async loops per participant wrap the hot path, both in `engine/voice_worker.py`:

- **Input** — `TurnGate` accumulates interim STT and emits only on turn-stop
  (swap via `KNOTCH_TURN`); `triage` drops backchannels before the LLM and tags
  a coarse class on routable turns.
- **Output** — each participant filters `bus:routed` by its own `role_id`;
  addressing is deterministic, with no second LLM call.

All bus messages are JSON `Envelope`s; the type-to-channel mapping lives in
`engine/interfaces.py`.

## Invariants, do not break these

1. **Generality gate.** No domain-specific literal (role names, station names,
   signal vocabulary) may appear anywhere under `engine/`. Domain knowledge lives
   only in `domains/<name>/`. Enforced by `tests/test_no_domain_strings.py`.
2. **Fail-safe routing.** `validate_decision` must never raise and must never
   trust the LLM: unknown recipients are dropped, source is never a recipient,
   and malformed input becomes a held decision. Covered by
   `tests/test_routing_contract.py`.
3. **One Bus protocol.** `InMemoryBus` and `RedisBus` are interchangeable; new
   bus features go through the `Bus` Protocol so both stay swappable by env var.
4. **Backends via env, not code.** Build services through `adapters/factory.py`
   (`KNOTCH_BUS|LLM|STT|TTS|TRANSPORT|TURN`); defaults are in
   `interfaces.DEFAULT_BACKEND`. Do not hardcode a backend in an entry point.

## Commands

README covers install, the single-process run, self-improvement, and the
distributed stack. The two an agent reaches for most:

```bash
KNOTCH_LLM=fake python3 -m engine --domain _template --all-scenarios   # offline smoke test, no endpoint
python3 -m pytest                                                      # tests
```

Only `domains/_template/` ships in the repo; other packs are gitignored and local-only.

## Gotchas

- **`adapters/factory.py` only wires the real LLM and bus.** `make_stt`,
  `make_tts`, and `make_transport` support `backend=fake` only; any other value
  raises `NotImplementedError`. Real STT, TTS, WebRTC, and telephony live in the
  `.starter/server/` workers, not the in-process engine path. Do not assume the
  factory gives you live audio.
- **`domains/<name>/routing_policy.md` is injected verbatim** into the system prompt by
  `domain_loader.assemble_system_prompt`. Keep it clean prose; anything in the file
  reaches the model.
- **Self-improvement does not persist by default.** `optimizer.improve_once` and
  `improve_rounds` return the best fragment and always write a
  `domains/<name>/routing_policy.revised.md` sibling. `proc_improve --apply` overwrites
  the live `routing_policy.md`; otherwise the original is untouched. It does not
  hot-swap an already-running coordinator process.
- **Optimizer extension seams.** `reflect` in `engine/optimizer.py` is a pure
  `(fragment, feedback) -> fragment` function — replace it to swap the rewrite
  strategy. Any object satisfying the `ScoreSource` protocol can replace the local
  rubric scorer; pass it via `improve_once(..., score_source=...)`.

## Conventions

- Comments and docstrings are professional and minimal: standard terminology, no
  references to the development process (no "TODO later", "Phase N", "WIP", or
  snapshot/commit notes), and no verbose restatement of the code.
- Engine code is domain-free; put anything domain-specific in a pack.
- All I/O is async; services are duck-typed against the `interfaces.py` Protocols.
- To add a backend, implement the relevant Protocol from `engine/interfaces.py`
  (`CoordinatorLLM`, `STTService`, `TTSService`, `Transport`, or `Bus`) and register
  it in `adapters/factory.py`.
