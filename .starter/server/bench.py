"""Real end-to-end latency + integration bench (headless, no mic).

Exercises every live leg of the voice->route->voice path:
  1. Convener LLM (Nemotron-3-Super) routing-decision latency.
  2. Gradium TTS time-to-first-audio (+ full synth) -> real PCM.
  3. Nemotron ASR (STT): feed the synthesized PCM back, measure finalization
     latency + check the transcript (a real TTS->STT loop).
Then sums the legs against the <1.5s TRD target.
"""
import asyncio, base64, json, os, statistics, sys, time
from pathlib import Path

import numpy as np
import soxr
from dotenv import load_dotenv
from websockets.asyncio.client import connect

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
load_dotenv(override=True)

from engine.packloader import load_pack, load_scaffold, assemble_system_prompt  # noqa
from engine.interfaces import RoutingRequest, Utterance  # noqa
from adapters.nemotron_llm import NemotronConvenerLLM  # noqa

LINE = "fryer's down, I'm forty seconds behind on the fries"
GRADIUM_URL = "wss://api.gradium.ai/api/speech/tts"
ASR_URL = os.environ["NVIDIA_ASR_URL"]


def ms(x): return f"{x*1000:7.1f} ms"


async def bench_llm(n=3):
    dd, pd = str(REPO / "domains"), str(REPO / "prompts")
    pack = load_pack("kitchen", domains_dir=dd, prompts_dir=pd)
    sysp = assemble_system_prompt(load_scaffold(prompts_dir=pd), pack)
    llm = NemotronConvenerLLM()
    req = RoutingRequest(
        system_prompt=sysp,
        utterance=Utterance(participant="role_fryer", text=LINE, triage_class="alert"),
        state={}, recent=[], participants=pack.roles,
    )
    lat, last = [], None
    for _ in range(n):
        t = time.monotonic()
        last = await llm.decide(req)
        lat.append(time.monotonic() - t)
    return lat, last


async def bench_tts(n=3):
    """Return (ttfa_list, total_list, pcm48k_bytes) — real Gradium synthesis."""
    key = os.environ["GRADIUM_API_KEY"]
    voice = os.environ["GRADIUM_VOICE_ID"]
    headers = {"x-api-key": key, "x-api-source": "bench"}
    ttfa, total, pcm = [], [], b""
    for i in range(n):
        ws = await connect(GRADIUM_URL, additional_headers=headers)
        await ws.send(json.dumps({"type": "setup", "output_format": "pcm",
                                  "voice_id": voice, "model_name": "default",
                                  "client_req_id": f"b{i}"}))
        t0 = time.monotonic()
        await ws.send(json.dumps({"type": "text", "text": LINE,
                                  "client_req_id": f"b{i}"}))
        first = None
        buf = b""
        try:
            while True:
                m = await asyncio.wait_for(ws.recv(), timeout=2.0)
                if isinstance(m, str):
                    d = json.loads(m)
                    if d.get("type") == "audio" and d.get("audio"):
                        if first is None:
                            first = time.monotonic()
                        buf += base64.b64decode(d["audio"])
        except asyncio.TimeoutError:
            pass
        await ws.close()
        if first:
            ttfa.append(first - t0)
        total.append(time.monotonic() - t0)
        pcm = buf  # keep last
    return ttfa, total, pcm


async def bench_stt(pcm48k: bytes):
    """Feed 48k PCM (resampled to 16k) to the ASR ws; return (final_latency, text)."""
    x = np.frombuffer(pcm48k, dtype="<i2").astype(np.float32) / 32768.0
    y = soxr.resample(x, 48000, 16000)
    pcm16 = (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()

    ws = await connect(ASR_URL, ping_interval=20, ping_timeout=20)
    # wait for ready
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        json.loads(msg)
    except Exception:
        pass
    # stream ~100ms chunks
    chunk = 3200  # 100ms @ 16k mono 16-bit
    for i in range(0, len(pcm16), chunk):
        await ws.send(pcm16[i:i + chunk])
    t0 = time.monotonic()
    await ws.send(json.dumps({"type": "reset", "finalize": True}))
    final_text, lat = "", None
    try:
        while True:
            m = await asyncio.wait_for(ws.recv(), timeout=6)
            d = json.loads(m)
            if d.get("type") == "transcript" and d.get("is_final") and d.get("text"):
                final_text = d["text"]; lat = time.monotonic() - t0
                break
    except asyncio.TimeoutError:
        pass
    await ws.close()
    return lat, final_text


async def main():
    print("== Convener LLM (Nemotron-3-Super) ==")
    llm_lat, decision = await bench_llm()
    print(f"  decision: recipients={decision.get('recipients')} "
          f"signal={decision.get('signal_type')} urgency={decision.get('urgency')}")
    print(f"  latency p50={ms(statistics.median(llm_lat))} "
          f"min={ms(min(llm_lat))} max={ms(max(llm_lat))}  (n={len(llm_lat)})")

    print("== Gradium TTS (voice=John) ==")
    ttfa, ttot, pcm = await bench_tts()
    if not ttfa:
        print("  WARN: no TTS audio received — skipping remaining benchmarks")
        return
    print(f"  time-to-first-audio p50={ms(statistics.median(ttfa))}  "
          f"full-synth p50={ms(statistics.median(ttot))}  pcm={len(pcm)} bytes")

    print("== Nemotron ASR (STT, TTS->STT loop) ==")
    stt_lat, text = await bench_stt(pcm)
    print(f"  transcript: {text!r}")
    print(f"  finalization latency={ms(stt_lat) if stt_lat else 'N/A'}")

    print("== End-to-end estimate (voice->route->voice) ==")
    e2e = (stt_lat or 0) + statistics.median(llm_lat) + statistics.median(ttfa)
    print(f"  STT_final + LLM + TTS_first ≈ {ms(e2e)}  "
          f"(+bus/transport ~50-100ms)  target < 1500 ms")


asyncio.run(main())
