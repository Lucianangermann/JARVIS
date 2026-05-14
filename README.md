# JARVIS

A small, security-first voice/text assistant built around the **Anthropic Claude
Haiku 4.5** API. One always-on MacBook server. Multiple clients (iPhone via the
web UI, Windows PC via the Python client, plus optional local mic).

```
                ┌──────────────────────┐
                │   MacBook (server)   │
                │  FastAPI + Whisper   │
   ┌────────────┤   Claude Haiku 4.5   ├────────────┐
   │            │   + command_guard    │            │
   │            └──────────────────────┘            │
   │                                                │
[iPhone Safari]                            [Windows PC]
  /web (HTML)                              clients/windows/client.py
```

## Project layout

```
jarvis/
├── server/
│   ├── main.py              # FastAPI app + WebSocket
│   ├── brain.py             # Claude API + agentic tool loop
│   ├── stt.py               # Whisper STT (lazy)
│   ├── tts.py               # pyttsx3 TTS in a worker thread
│   ├── auth.py              # Bearer auth (HTTP + WS) + rate limit
│   ├── command_guard.py     # Whitelist + rejected.log
│   ├── config.py            # .env loader, settings singleton
│   └── tools/
│       ├── search.py        # placeholder — web_search is server-side
│       └── system.py        # thin wrappers around command_guard
├── clients/
│   ├── windows/client.py    # Cross-platform REST + WS client
│   └── web/index.html       # Mobile-friendly UI (iPhone Safari)
├── .env.example
├── .gitignore               # Includes .env, logs/, *.key
└── requirements.txt
```

## Setup

```bash
cd /Users/lucianangeramann/Documents/JARVIS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # large — pulls torch for whisper

cp .env.example .env
# 1. Paste your Anthropic API key into ANTHROPIC_API_KEY
# 2. Generate a strong auth token:
python -c "import secrets; print(secrets.token_urlsafe(32))"
# 3. Paste it into JARVIS_AUTH_TOKEN
```

## Run the server

```bash
python -m server.main
```

You should see:

```
[JARVIS] starting up
[JARVIS] model=claude-haiku-4-5-20251001
[JARVIS] listening on http://127.0.0.1:8000
[JARVIS] web UI at  http://127.0.0.1:8000/web
```

### Text test (no microphone needed)

```bash
TOKEN=$(grep JARVIS_AUTH_TOKEN .env | cut -d= -f2)
curl -sS http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Say hello in one short sentence."}'
```

### iPhone (web client)

1. On the MacBook, set `HOST=0.0.0.0` in `.env` and restart (`HOST=127.0.0.1` is
   localhost-only).
2. Add the LAN URL to `ALLOWED_ORIGINS`, e.g.
   `ALLOWED_ORIGINS=http://192.168.1.50:8000`.
3. On iPhone Safari, open `http://<mac-lan-ip>:8000/web`.
4. Paste the same token and tap **Connect**. The page auto-reconnects on
   reload.
5. Optional: *Share → Add to Home Screen* for a full-screen PWA-style icon.

### Windows client

```cmd
set JARVIS_URL=http://192.168.1.50:8000
set JARVIS_AUTH_TOKEN=<paste token>
pip install requests websockets
python clients\windows\client.py --ws
```

## Security model

| Concern | What we do |
|---|---|
| API key leakage | `ANTHROPIC_API_KEY` only in `.env`, which is in `.gitignore`. `config.py` errors out if the placeholder is left in place. |
| Unauthenticated access | Every HTTP route (except `GET /`) and WebSocket requires `Authorization: Bearer <JARVIS_AUTH_TOKEN>`. WS also accepts `?token=` so browsers can connect. Token comparison uses `secrets.compare_digest`. |
| Arbitrary command execution | The brain has *one* system tool, `system_command`, dispatching into the named whitelist in `command_guard.py`. Anything else is rejected and logged to `logs/rejected.log`. No `os.system` of raw user input anywhere. |
| Microphone privacy | Audio is only captured when a wake word fires (`stt.listen_once()`), and `[MIC ON]` / `[MIC OFF]` is printed every time. |
| Input flooding | 500-char cap (`MAX_INPUT_LENGTH`), 10 req/min per token (`RATE_LIMIT_PER_MINUTE`), control characters stripped. |
| CORS | Only origins listed in `ALLOWED_ORIGINS` get CORS headers. |
| Network exposure | `HOST=127.0.0.1` by default. Switch to `0.0.0.0` only on a trusted LAN. |

## Swap-ability

- **STT backend** — edit `server/stt.py::transcribe`. Whisper is loaded lazily.
- **TTS backend** — replace `server/tts.py::speak`. Worker-thread queue stays.
- **Model** — change `MODEL` in `.env`. Default is `claude-haiku-4-5-20251001`.
- **Tool surface** — add a handler + schema to `ALLOWED_COMMANDS` in
  `server/command_guard.py`. The brain picks it up automatically because
  `system_command`'s `command` enum is generated from the registry.

## Notes

- Conversation memory is per-auth-token, kept in process memory only. Trimmed
  to the last `MAX_HISTORY_TURNS` user/assistant pairs.
- Prompt caching is enabled on the system prompt (`cache_control: ephemeral`),
  so repeat turns pay ~0.1× for the prompt — verify with
  `response.usage.cache_read_input_tokens` in the brain logs if you add
  diagnostics.
- The first call to `/audio` triggers Whisper to download its model file
  (hundreds of MB). Consider preloading by running
  `python -m server.stt --listen` once.
