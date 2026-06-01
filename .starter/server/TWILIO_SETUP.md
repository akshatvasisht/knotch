# Twilio Echo Smoke-Test — Setup Guide

This is a standalone smoke-test for the Twilio → pipecat media-stream channel.
It proves the webhook → TwiML → WebSocket → pipecat transport plumbing works
**before** wiring the real coordinator worker into it.

---

## Prerequisites

- Python venv already set up at `.starter/server/.venv`
- `.starter/server/.env` has `GRADIUM_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
- cloudflared installed at `~/.local/bin/cloudflared` (see installation below)

---

## Step 1 — Start the echo server

```bash
cd .starter/server
.venv/bin/python twilio_echo.py
# or: uv run twilio_echo.py
```

Default port is **7860**.  Override with `TWILIO_ECHO_PORT=<port>`.

---

## Step 2 — Open a cloudflared tunnel

In a **second terminal**:

```bash
~/.local/bin/cloudflared tunnel --url http://localhost:7860
```

cloudflared will print something like:

```
https://abc123.trycloudflare.com
```

Note that hostname — you need it in the next step.

---

## Step 3 — Set PUBLIC_WSS_URL and restart

Stop the echo server (Ctrl-C), then:

```bash
PUBLIC_WSS_URL=abc123.trycloudflare.com .venv/bin/python twilio_echo.py
```

Or add it to `.env`:

```
PUBLIC_WSS_URL=abc123.trycloudflare.com
```

---

## Step 4 — Verify TwiML (headless, no phone needed)

```bash
curl -s -X POST https://abc123.trycloudflare.com/twiml
```

Expected output:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://abc123.trycloudflare.com/ws" />
  </Connect>
</Response>
```

You can also hit `http://localhost:7860/health` to confirm the server is up.

---

## Step 5 — Configure your Twilio number (needs a Twilio voice number)

1. Go to **Twilio Console → Phone Numbers → Manage → Active numbers**.
2. Click your voice number.
3. Under **Voice & Fax → A Call Comes In**, set:
   - **Webhook**: `https://abc123.trycloudflare.com/twiml`
   - **HTTP method**: `POST`
4. Save.

Now call the number — Twilio will hit the webhook, receive the TwiML `<Connect><Stream>`,
open a WebSocket to `/ws`, and the bot will speak a greeting via Gradium TTS.

---

## cloudflared installation (if not present)

```bash
mkdir -p ~/.local/bin
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
     -o ~/.local/bin/cloudflared
chmod +x ~/.local/bin/cloudflared
~/.local/bin/cloudflared --version
```

---

## What is confirmed headless (no phone number required)

| Check | How |
|---|---|
| TwiML returns valid XML with `<Connect><Stream` | `curl -X POST .../twiml` |
| Health endpoint up | `curl .../health` |
| pipecat imports resolve cleanly | Start the server — no ImportError |
| cloudflared starts and yields a public URL | Run Step 2 |

## What is blocked on the phone number

- Full end-to-end call (Twilio media stream open + audio in/out)
- `auto_hang_up` (needs real `call_sid`)
- Confirming Gradium TTS audio reaches the caller

---

## Integration note — swapping in the real coordinator worker

When Step 4 is clear, replace the `Pipeline` block inside `ws_endpoint()` in
`twilio_echo.py` with the same `run_participant()` function used by `bot_coordinator.py`,
passing the `FastAPIWebsocketTransport` instead of the existing `SmallWebRTCTransport`.
The only diff is the transport constructor and the 8 kHz sample rates (`audio_in_sample_rate=8000`,
`audio_out_sample_rate=8000` in both `FastAPIWebsocketParams` and `PipelineParams`).
`BusBridge`, `NVidiaWebSocketSTTService`, and `GradiumTTSService` are transport-agnostic
and wire in unchanged.
