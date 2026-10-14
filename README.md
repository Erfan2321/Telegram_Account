# 🤖 Telegram UserBot

A Telegram **userbot** that runs on a personal account (Pyrogram, not the Bot API), with
anti-delete logging, self-destructing media capture, auto read-receipts, an authorization
layer, group moderation, live data scrapers, and a multimodal AI assistant powered by the
Google Gemini API.

<p align="center">
  <img alt="Python 3.13" src="https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white">
  <img alt="kurigram 2.2.24" src="https://img.shields.io/badge/kurigram-2.2.24-2AABEE?logo=telegram&logoColor=white">
  <img alt="Docker ready" src="https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white">
  <img alt="License MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

> **Interface language:** every message the bot sends into Telegram is in **Persian (Farsi)**.
> Logs, error messages and all documentation are in English. If you need an English chat
> interface, the user-facing strings live in `plugins/` and `utils/helpers.py` and are
> straightforward to translate — see [Coding standards](#-development).

---

## ⚠️ Before you start

This project runs on your **real Telegram account**, not on a bot account.

- Running a userbot is generally interpreted as a violation of the
  [Telegram Terms of Service](https://core.telegram.org/api/terms). Unusual behaviour
  (spam, bulk actions) **can get your account limited or banned**. You accept that risk.
- `data/my_account.session` is equivalent to **full access to your account**. Never commit it,
  send it to anyone, or leave it anywhere public.
- `data/logger.db` contains **real private messages**.

Both are gitignored by default.

---

## ✨ Features

| Area | What it does |
|---|---|
| 🗑 **Anti-delete** | Private messages (both directions) are cached for 12 hours; a deletion is reported to Saved Messages — photos, voices, stickers, GIFs and video notes come back as the file itself, with the send and delete times |
| 🕵️ **TTL media guard** | Timed and view-once media — photo, video, video note, voice, GIF, audio, file — is saved before it can be viewed |
| 👁 **Auto-seen** | Automatic read receipts for selected chats |
| 🧹 **Bot-chat cleaner** | `.botclean on`: when a bot messages you in a chat you already deleted, the message goes away with the chat — no need to fill the block list |
| 🔗 **Forward by link** | `.fwd <link>` brings a message or album here from any channel or group you can see, private and protected ones included |
| 👮 **Member control** | Kick, ban and mute (with optional duration) in a group |
| 🌐 **Global actions** | `.gban` / `.gmute` across every managed group, enforced on later messages too |
| 🔐 **Authorization layer** | Owner-only commands separated from authorized users |
| 🧠 **AI assistant** | Streaming Q&A with support for images, voice, video, PDFs and replies |
| 🔊 **Voice** | Speech-to-text (`.stt`) and text to a real Telegram voice message (`.tts`) |
| 🌐 **AI tools** | Translation, OCR, summarization, image generation |
| 📈 **Live data** | Crypto prices in Toman, gold prices, weather, prayer times |
| 🛡 **Resilience** | Clock synchronization, Pyrogram peer-id range patch, log noise throttling |

---

## 📋 Requirements

- **Python 3.13**
- `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org)
- A Google Gemini API key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
  *(optional — needed only for the AI commands)*
- `ffmpeg` *(optional — without it, `.tts` returns a WAV file instead of a voice message)*

---

## 🚀 Getting started

### Option 1: run it directly

```bash
git clone <repo-url> && cd Telegram_Account
python3.13 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill in the values
./venv/bin/python main.py
```

On the first run you are asked for your phone number and a login code. The session is stored
in `data/` and you are not asked again.

### Option 2: Docker

```bash
cp .env.example .env      # then fill in the values
docker compose up --build
```

> The first login has to be interactive. If `data/my_account.session` does not exist yet,
> either log in once with Option 1, or run the container with
> `docker compose run --rm userbot` so you can type the code.

> Only `./data` is mounted into the container; the code ships inside the image, so code
> changes require `docker compose up --build`.

---

## ⚙️ Configuration

All settings come from a `.env` file. A fully commented sample lives in
[`.env.example`](.env.example).

| Variable | Required | Default | Description |
|---|:---:|---|---|
| `API_ID` | ✅ | — | From my.telegram.org |
| `API_HASH` | ✅ | — | From my.telegram.org |
| `LOG_LEVEL` | ➖ | `INFO` | Log level (`DEBUG` also enables Pyrogram's own logs) |
| `AI_API_KEYS` | ➖ | — | Gemini keys, comma-separated (rotated automatically on quota errors) |
| `AI_MODEL` | ➖ | `gemini-3.6-flash` | Chat model |
| `AI_IMAGE_MODEL` | ➖ | `gemini-3.1-flash-image` | Image generation model (requires a paid plan) |
| `AI_TTS_MODEL` | ➖ | `gemini-3.1-flash-tts-preview` | Text-to-speech model |
| `AI_TTS_VOICE` | ➖ | `Kore` | Default voice (`.tts voices` lists them) |
| `AI_TIMEOUT` | ➖ | `120` | Max wait per response chunk (seconds) |
| `AI_MAX_TURNS` | ➖ | `12` | Conversation memory kept per chat, in messages |
| `AI_MAX_CONCURRENCY` | ➖ | `5` | Max concurrent AI requests |
| `AI_MEDIA_MAX_MB` | ➖ | `25` | Max size of a file forwarded to the model |
| `TTL_GUARD` | ➖ | `true` | Save self-destructing private media before it is viewed |
| `TTL_GUARD_LOCAL_COPY` | ➖ | `false` | Keep an extra copy on disk under `data/ttl_media/` |
| `TTL_GUARD_DEBUG` | ➖ | `false` | Attach the raw media structure to a failed rescue report |
| `TTL_MEDIA_RETENTION_DAYS` | ➖ | `2` | How long local copies stay on disk before being swept |
| `DISPLAY_TIMEZONE` | ➖ | `Asia/Tehran` | Timezone for every time shown in a Telegram message |
| `DELETE_CACHE_MEDIA` | ➖ | `true` | Pre-download small media so a deletion report survives an expired file reference |
| `DELETE_CACHE_MAX_MB` | ➖ | `5` | Largest media that gets pre-downloaded |
| `DELETE_CACHE_TOTAL_MB` | ➖ | `500` | Hard ceiling for `data/deleted_media/` |
| `GBAN_ENFORCE` | ➖ | `true` | Apply the global lists when a listed user posts in a managed group |
| `GLOBAL_ACTION_DELAY_MS` | ➖ | `350` | Delay between requests during global operations (ms) |
| `CLOCK_SYNC` | ➖ | `true` | Clock synchronization (a slow clock kills the session) |
| `CLOCK_SYNC_INTERVAL` | ➖ | `1800` | Re-measurement interval (seconds) |
| `CLOCK_SYNC_TIMEOUT` | ➖ | `5` | Time-server timeout — added directly to startup latency |

> If the host clock is known to be correct, `CLOCK_SYNC=false` cuts roughly 8 seconds off
> startup time.

---

## 💬 Commands

Prefixes: `.` or `/`

### Access model

There are exactly two levels:

- **Owner** — the account the bot runs on. Nothing to register; Pyrogram identifies it with
  the `filters.me` filter. Configuration commands (`.adduser`, `.addseen`, `.ai`) are
  owner-only.
- **Authorized users** — ids added with `.adduser`, stored in the `authorized_users` table.
  Every other command (`AUTH_FILTER = filters.me | authorized users`) works for them too.

Any message from anyone else is ignored completely — the bot does not even reply with an
error to someone outside these two groups.

> ⚠️ "Authorized user" is a high trust level, not a limited role. Such a user can run `.send`
> (reading messages from any of your private chats) and `.gban` (banning across every group
> you manage). Only grant it to someone you would trust with the account itself — or change
> the decorators on the sensitive commands in `plugins/` to `filters.me`.

### Administration (owner only)

| Command | Purpose |
|---|---|
| `.adduser` / `.deluser` | Add or remove an authorized user |
| `.addseen` / `.delseen` | Add or remove an auto-seen chat |
| `.ai model` \| `models` \| `persona` \| `reset` \| `status` | AI configuration |
| `.fwd <link> [link …]` / `.forward` | Forward the linked message here — public or private (`t.me/c/…`) channels and groups, topics, `?single`, `?comment=`. Sent as a reply with no link, it reads the link from the replied message. On a protected chat the file is downloaded and re-uploaded as a copy. The command message is removed on success |
| `.botclean on` \| `off` | Bot-chat cleaner. With it on, a bot that writes into a chat you deleted (nothing of yours in it) has its message deleted together with the chat entry. Chats with any message of yours are never touched. `.botclean` alone shows the state and the last clean-ups |

### Utilities (authorized users)

| Command | Purpose |
|---|---|
| `.help` / `.cmds` | Full command list |
| `.ping` | Liveness check |
| `.id` | Show chat and user ids |
| `.weather <city>` | Weather |
| `.gold` | Gold and coin prices |
| `.price` / `.market` | Live USDT/BTC/ETH prices in Toman (60 s cache) |
| `.azan <city>` | Prayer times |
| `.del <n>` | Delete the last `n` messages without triggering the anti-delete alert (max 200, sent in batches of 100) |
| `.send <count> <user_id>` | Relay the last `count` messages of a private chat into the current chat (max 50) |
| `.ttl` / `.ttlguard` | TTL media guard status and per-run counters |

### Member control (authorized users)

The target is chosen by **replying** to the user's message, or by passing a numeric id or
username. Durations are written without a space: `30m` `2h` `1d` `1w`.

| Command | Purpose |
|---|---|
| `.kick [reason]` | Remove from this group without banning — the user can rejoin |
| `.ban [duration] [reason]` | Ban in this group; e.g. `.ban 2h spam` |
| `.unban` | Lift a ban *(supergroups only)* |
| `.mute [duration] [reason]` | Silence a member *(supergroups only)*; e.g. `.mute 30m` |
| `.unmute` | Lift user-level restrictions *(supergroups only)* |

> Telegram reinterprets any duration under 30 seconds or over 366 days as "forever", so both
> are rejected instead of silently doing something else. For a permanent ban, pass no duration.

### Global actions (authorized users)

Applied to **every group** where this account has the "ban users" right, and recorded in the
database as well.

| Command | Purpose |
|---|---|
| `.gban [reason]` / `.ungban` (`.gunban`) | Global ban and its removal |
| `.gmute [reason]` / `.ungmute` (`.gunmute`) | Global mute and its removal *(supergroups only)* |
| `.gbanlist` / `.gmutelist` | Show the contents of the lists |
| `.gstatus` / `.gadmin` | Managed-group count, list sizes and enforcement stats |
| `.fakegban` / `.fgban` | **Prank** — imitates `.gban` and does nothing at all |

> ⚠️ These commands fire dozens of consecutive requests at Telegram. The delay between them
> (`GLOBAL_ACTION_DELAY_MS`) is deliberate; lowering it raises the risk of the account being
> limited.
>
> ⚠️ Like the other utilities, these are gated by `AUTH_FILTER`, meaning **authorized users can
> run them too**. If you do not want that reach, change the decorators in
> `plugins/group_admin.py` to `filters.me`.

### AI (authorized users)

| Command | Purpose |
|---|---|
| `.ask <question>` | Question with a streamed answer (attachments and replies supported) |
| `.r` / `.re` / `.about` | Ask **about the replied-to message** and its media |
| `.rp` / `.suggest` | Draft three replies to the replied-to message |
| `.sum [n]` / `.tldr` | Summarize the replied-to message, or the last `n` messages (max 300) |
| `.tr [lang] <text>` / `.translate` | Translate text, a reply, or text inside an image |
| `.ocr` / `.readimg` | Extract text from an image or PDF |
| `.stt` / `.v2t` / `.voice2text` | Transcribe voice, audio or video |
| `.tts [voice] <text>` / `.say` | Turn text into a voice message |
| `.img <prompt>` / `.image` / `.draw` | Generate or edit an image *(requires a paid key)* |
| `.aistop` | Stop an in-flight generation |

> AI commands accept a model alias and `extended` as leading words:
> `.ask pro extended ...` or `.sum lite 100`

---

## 📁 Project layout

```text
.
├── main.py                 bootstrap: patches, clock sync, init_db, Client, idle, shutdown
├── config.py               env loading with fail-fast validation
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example            configuration sample
├── ARCHITECTURE.md         architecture notes and design decisions
│
├── data/                   ⚠️ runtime state — fully gitignored
│   ├── my_account.session      Telegram session (= full account access)
│   ├── logger.db               SQLite database (real private messages)
│   ├── ttl_media/              local copies of TTL media (only when enabled)
│   └── forward_tmp/            .fwd scratch space for protected files (emptied after each run)
│
├── plugins/                handlers — auto-discovered by Pyrogram
│   ├── general.py              authorization commands + general utilities
│   ├── logger.py               anti-delete cache and deletion reports
│   ├── auto_seen.py            automatic read receipts
│   ├── ai_assistant.py         .ask / .r / .rp / .sum — streaming assistant
│   ├── ai_tools.py             .tr / .ocr / .img
│   ├── voice_tools.py          .stt / .tts
│   ├── ttl_guard.py            self-destructing media capture (raw handler, group -1)
│   ├── group_admin.py          member control + global actions (enforcement, group -2)
│   ├── bot_cleaner.py          .botclean — deletes bot messages in chats you deleted (group 3)
│   └── link_forward.py         .fwd — forward by message link, protected chats included
│
└── utils/                  helper modules (no dependency on the handlers)
    ├── paths.py                project paths — single source of truth
    ├── db_manager.py           aiosqlite access + RAM caches
    ├── helpers.py              respond(), respond_long(), @safe_handler, flood_seconds()
    ├── net.py                  shared HTTP layer for the information modules
    ├── ai_engines.py           Gemini transport: SSE, files, images, TTS, key rotation
    ├── ai_context.py           Telegram message → model input
    ├── media.py                media inspect/download + PCM→WAV/OGG conversion
    ├── pyrogram_patch.py       runtime patches for the pinned library
    ├── clock.py                clock offset probe (SNTP + HTTP-Date)
    ├── logging_setup.py        log configuration and noise throttling
    ├── weather_api.py          Open-Meteo
    ├── gold_api.py             TGJU
    ├── azan_api.py             AlAdhan
    └── currency_api.py         Wallex → Ramzinex with automatic failover
```

The architecture, the reasoning behind each decision and the implementation details are
documented in [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 🔒 Security

- **Never commit `data/`.** The session file grants full access to the account.
- **Keys belong in `.env` only.** `.env` is gitignored; keep `.env.example` as the template.
- If a key is ever committed by accident, **revoke it** — removing it from the code is not
  enough, since it stays in the git history.
- Model and persona commands are deliberately owner-only: changing the persona is a
  configuration-injection path.
- Note that `.send` lets authorized users republish messages from **any** of the owner's
  private chats into a chat they can see. Restrict that decorator to `filters.me` if you do
  not want that level of access.
- The **TTL media guard** deliberately retains media the sender expected to disappear. The
  sender gets no notification (unlike a screenshot in a secret chat) and the media stays
  "unviewed" on their side. This means other people's private content accumulates in `data/`
  and in your Saved Messages — set `TTL_GUARD=false` to turn it off.
- On a chat with **protected content** ("Restrict saving content"), `.fwd` downloads the file
  and uploads a copy, which sidesteps the owner's restriction. It is owner-only for that
  reason; mind the rights of whoever published the content.

---

## 🩺 Troubleshooting

Errors and logs are deliberately **English** (easier to paste into issues and terminals
without right-to-left reordering), while the messages sent inside Telegram are Persian.

| Message | Meaning and fix |
|---|---|
| `configuration error: API_ID is not set` | `.env` is missing or empty. Run `cp .env.example .env` and fill in the values. |
| `API_HASH does not look like a 32-character hex string` | The value copied from my.telegram.org is incomplete. |
| `could not start the Telegram client` | Wrong `API_ID`/`API_HASH`, no network, or a corrupted session file. If the session is corrupt, delete `data/my_account.session` and log in again. |
| `database initialisation failed` | No write permission on `data/`, or `logger.db` is corrupted. |
| `AI_API_KEYS is not set` | Only the AI commands are disabled; the rest of the bot runs fine. |
| `found the legacy AI_API_KEY (singular)` | Old `.env`: the previous service's key is **not** read. Put the Gemini key in `AI_API_KEYS`. |
| `... is not a Google model name` | `AI_MODEL` is left over from the previous service (e.g. `gpt-5-mini`); it is ignored and the default is used. |
| `system clock is N s behind` | The host clock is slow. `msg_id` is corrected automatically, but the real fix is NTP on the host. |
| `FloodWait Ns ...` | Telegram is rate-limiting. Under 60 seconds it waits and retries; above that the operation is abandoned. |
| `.tts` returns a WAV file instead of a voice message | `ffmpeg` is not installed. The feature still works, only the presentation differs. |
| `.img` returns a quota error | Google's image models have a zero quota on the free plan. |
| The container keeps restarting | `restart: unless-stopped` ignores the exit code, so a configuration error (code 2) creates a restart loop. Check `docker compose logs`; the cause is almost always an incomplete `.env`. |
| The bot does not respond to a command | The sender is neither the owner nor on the authorized list — the silence is deliberate. Add them with `.adduser` from your own account. |

**Exit codes** (for systemd and Docker):
`2` configuration · `3` creating `data/` · `4` database · `5` connecting to Telegram ·
`1` anything unhandled

---

## 🛠 Development

Issues and pull requests are welcome. If you send a change, please follow the rules below so
review goes quickly — the reasoning behind each one is documented in [`ARCHITECTURE.md`](ARCHITECTURE.md).

**The repository has no automated test suite.** Verification is done headlessly, and none of
it requires connecting to Telegram:

- Handlers are plain async functions — call them directly with a fake `message` exposing
  `chat`, `command`, `from_user`, `outgoing`, `edit_text` and `reply_text`.
- `@Client.on_message` on the class returns the function unchanged and only attaches
  `.handlers` to it; assert that attribute to confirm registration.
- Test the `utils/*_api.py` modules against fixtures rather than live requests — the Iranian
  services they scrape are not reachable from everywhere.
- Point `db_manager.DB_PATH` at a temporary file. **Never test against the real
  `data/logger.db`.**

Core rules (in full in [`ARCHITECTURE.md`](ARCHITECTURE.md) §10):

1. **Async only** — no blocking calls (`requests`, `time.sleep`, synchronous `sqlite3`) in
   handlers.
2. The information modules in `utils/` return display-ready Persian strings and **never raise
   into a handler**.
3. Use `respond()`, not `message.edit_text` — another user's message cannot be edited.
4. Wrap every command handler in `@safe_handler`.
5. Escape user content (prefer HTML + `html.escape()`).
6. **Language:** everything that goes to Telegram is Persian, everything that goes to `log` or
   into an exception message is English. (The one exception is `MediaError`, whose text is
   shown to the user directly.)
7. Comments in Persian, identifiers in English — explain *why*, not *what*.
8. New dependency ⇒ pin it in `requirements.txt`.
9. Schema change ⇒ idempotent migration in `init_db()`.

---

## 📄 License

Released under the [MIT License](LICENSE) — you are free to use, modify, redistribute and
even commercialize it; the only condition is keeping the copyright and license text in your
own copies.

The software is provided "as is" and comes with **no warranty**. As noted above, running a
userbot is generally interpreted as a violation of Telegram's Terms of Service; whatever
happens to your account is your responsibility, not the author's.
