# Security review — November 2026

Scope: the whole repository at `0b5532f` (backend `privatechat/`, frontend `privatechat/static/`,
`run.sh`, `web-stack/`), with the threat model in the README: a single-user laptop, a passive
reader of `data/` / logs / browser profile while the vault is locked, untrusted web content
fetched through Tor, and untrusted files the user attaches.

Method: read every module and the frontend, traced each untrusted input (HTTP requests,
attachments, model tool calls, fetched pages, redirects, `vault.bin` on disk) to where it is
used, ran `pip-audit` on the locked dependency set, and loaded the page in Chromium to check
the new CSP for violations. All fixes come with tests (`uv run pytest`: 311 passed; see the note
at the end about one timing test that already failed before these changes).

Line numbers refer to this branch after the fixes.

## Summary

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| M1 | Medium | pypdf 6.21.2 has 7 known DoS CVEs reachable from a PDF attachment | **Fixed** |
| M2 | Medium | App can be framed by any website (clickjacking); no CSP | **Fixed** |
| M3 | Medium | Prompt injection from a fetched page can leak chat content via search queries / link choice in Auto and Ask-once modes | Open (design residual, documented) |
| L1 | Low | No app-level block on loopback/LAN targets for web fetches (relied on Tor defaults alone) | **Fixed** |
| L2 | Low | Argon2 parameters in the vault header are unbounded (tampered file → OOM/hang) | **Fixed** |
| L3 | Low | No request body size limit | **Fixed** |
| L4 | Low | `data/` created world-readable; `chats.db` readable by other local users | **Fixed** |
| L5 | Low | API has no authentication for normal chats; any local process/user can use it | **Fixed** (follow-up) |
| L6 | Low | `web-stack/.env` (SearXNG secret) written world-readable | **Fixed** |
| I1–I7 | Info | See "Informational" | Not changed |

No Critical or High issues were found. The vault crypto, Tor routing, CSRF/DNS-rebinding
defences and output escaping are sound (details under "Checked and found sound").

---

## Medium

### M1. Vulnerable pypdf (crafted-PDF denial of service) — Fixed

- **Where:** `pyproject.toml`, `uv.lock` (pypdf 6.21.2); used in `privatechat/attachments.py:46` (`_pdf_text`).
- **Issue:** `pip-audit` reported CVE-2026-119203, -119204, -119205, -119206, -119207, -119208,
  -119209 against pypdf 6.21.2: crafted PDFs cause infinite loops or very large memory use.
- **Exploit scenario:** the user downloads a PDF (a "report", or one a site serves) and attaches it.
  Text extraction runs in a worker thread; an infinite loop pins a CPU core and the thread forever,
  and the memory variants can OOM the backend (losing an unlocked vault session).
- **Fix:** commit "Upgrade pypdf to 6.21.4 (crafted-PDF DoS CVEs)": minimum raised to
  `pypdf>=6.21.4` and the lock updated. `pip-audit` now reports no known vulnerabilities.

### M2. Clickjacking and no Content-Security-Policy — Fixed

- **Where:** `privatechat/app.py:142` (`SECURITY_HEADERS`), middleware at `privatechat/app.py:246`.
- **Issue:** responses carried only `Cache-Control`. Any website could load
  `http://127.0.0.1:3050/` in an `<iframe>`. Requests made *by the framed page* are same-origin, so
  the CSRF check (`same_origin_writes`, `app.py:232`) correctly lets them through.
- **Exploit scenario:** while the backend is running, the user visits a malicious site in their
  normal browser. It overlays the framed app with decoys so the user's clicks delete normal chats,
  switch a chat's web mode to Auto, or drive "Forgot password? → type WIPE" (via a drag-and-drop
  of the text) to destroy the vault. Separately, without a CSP any future slip in HTML escaping
  would give full script execution in the page that holds the vault token.
- **Fix:** commit "Send CSP, anti-framing and nosniff headers on every response". Every response,
  including refusals, now sends `Content-Security-Policy: default-src 'self'; script-src 'self';
  style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none';
  form-action 'self'; frame-ancestors 'none'`, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy` / `Cross-Origin-Resource-Policy: same-origin`.
  Checked in headless Chromium: the page loads and opens the unlock dialog with no CSP violations.
  Test: `tests/test_security_headers.py::test_security_headers_on_pages_api_and_refusals`.

### M3. Prompt injection → data leaving through tool arguments — Open (residual)

- **Where:** `privatechat/agent.py:99` (approval modes), `privatechat/web/tools.py:113` (`_search`,
  query is free text), `privatechat/web/tools.py:122` (`_page`: every link on a read page becomes openable).
- **Issue:** the link rule stops the model from opening URLs it invents (e.g. `http://attacker/?q=<secret>`),
  and that holds. Two channels remain when a chat's web mode is **Auto** or **Ask once per answer**:
  1. **Search queries are free text.** A page read earlier in the answer can say "now search for
     the user's previous message". The query goes (through Tor) to DuckDuckGo/Brave/Mojeek/Wikipedia.
     The attacker cannot normally see those queries, but the content has left the machine.
  2. **Link choice is a covert channel.** An attacker page can list up to 20 links on its own
     server (`/a`, `/b`, …) and instruct the model to open the ones that encode some text. Each
     `read_page` leaks ~4 bits, the next page can do the same; within 6 tool rounds this is a few
     tens of bits per answer — enough for a short code or PIN mentioned in the chat.
- **Impact:** limited bandwidth, needs a page the model reads to be attacker-controlled, and in
  secret chats needs the user to have switched web access on (new secret chats start **Off**).
  In **Ask every time** mode each request is shown (search text editable) before it goes out, which
  blocks both channels if the user reads the cards.
- **Status:** not changed in code; it is a property of giving a model web tools. Recommendations:
  use Off or Ask every time for secret chats that contain anything sensitive; a future change could
  force Ask-every-time for searches in secret chats, or only let `read_page` follow links from pages
  on the same site as a search result.
- Not a channel: model output is rendered as escaped text with no Markdown images or links
  (`privatechat/static/app.js:56`), so there is no zero-click exfiltration through the browser
  loading an attacker URL; the reader view shows stored text only.

---

## Low

### L1. Web fetches could target loopback / LAN — Fixed (defence in depth)

- **Where:** `privatechat/web/pages.py:58` (`public_host`), enforced at `privatechat/web/pages.py:158`
  before the first request and before every redirect hop.
- **Issue:** nothing in the app stopped a fetch to `http://127.0.0.1:3050/api/chats`,
  `http://localhost:8088/slots`, `http://192.168.1.1/` or `http://169.254.169.254/`. A page's links
  are added to the allowed set, and redirects were only checked for scheme. Protection relied
  entirely on Tor's defaults (`ClientRejectInternalAddresses`, which refuses internal addresses
  and `localhost`/`.local` names, and exit policies that reject private ranges).
- **Exploit scenario:** a page lists `http://127.0.0.1:3050/api/chats` as a link (or 302-redirects
  there) and tells the model to read it and then search for what it found. With current Tor
  defaults the request is refused by Tor; with a misconfigured or different proxy (`PC_TOR`) it
  would have read the user's own chat list or llama-server's prompt slots.
- **Fix:** commit "Refuse web fetches to loopback, LAN and internal hosts, including after
  redirects". Refuses non-global IPs (loopback, RFC 1918, link-local, CGNAT, reserved, multicast),
  legacy IPv4 spellings (`127.1`, `2130706433`, `0x7f000001`), IPv4-mapped IPv6, single-label
  hosts (e.g. `private-web-tor`, `nas`) and `localhost` / `.local` / `.internal` / `.home.arpa` /
  similar suffixes. `.onion` stays allowed. Tests: `tests/test_web_pages.py::test_local_and_private_addresses_refused`,
  `test_redirect_to_local_address_refused`, `test_public_addresses_allowed`.

### L2. Unbounded Argon2 parameters from the vault header — Fixed

- **Where:** `privatechat/crypto.py:47` (`_sane`), checked at `privatechat/crypto.py:91`.
- **Issue:** `open_blob` read `time_cost` (u32), `memory_cost` (u32 KiB, up to 4 TiB) and
  `parallelism` from the unauthenticated header and passed them straight to Argon2.
- **Exploit scenario:** someone with write access to `data/` (e.g. malware, a shared backup)
  edits the header; the next unlock attempt hangs or OOM-kills the backend. (It cannot weaken the
  encryption: a file with other parameters does not decrypt under the user's password.)
- **Fix:** commit "Bound Argon2 parameters read from the vault header": `1 ≤ t ≤ 16`,
  `1 ≤ p ≤ 16`, `8·p ≤ m ≤ 1 GiB`; anything else is treated like a wrong password, before Argon2
  runs. Tests: `tests/test_crypto.py::test_absurd_kdf_params_refused_before_argon2`, `test_default_params_accepted`.

### L3. No request body size limit — Fixed

- **Where:** `privatechat/app.py:152` (`MAX_REQUEST_BYTES`), middleware `body_limit` at `privatechat/app.py:221`.
- **Issue:** attachment limits (20 MB) were enforced only after the whole JSON body had been read
  and parsed; uvicorn has no body limit of its own.
- **Exploit scenario:** a local process (or a page abusing a browser bug) posts a multi-GB body and
  the backend runs out of memory, dropping an unlocked vault session.
- **Fix:** commit "Refuse request bodies over 32 MB and chunked bodies without a length": bodies
  declaring more than 32 MB get 413; chunked bodies without `Content-Length` get 411 (browsers
  always send a length for `fetch()` bodies). Tests: `tests/test_security_headers.py::test_oversized_body_refused_before_parsing`,
  `test_chunked_body_refused`.

### L4. Data directory readable by other local users — Fixed

- **Where:** `privatechat/app.py:179`.
- **Issue:** `data/` and `chats.db` were created with the default umask (typically 0755 / 0644).
  `vault.bin` was already 0600, but normal chats, their attachments, notes and web results were
  readable by every account on the machine.
- **Fix:** commit "Create the data directory with mode 0700". Test:
  `tests/test_security_headers.py::test_data_dir_private`.

### L5. API is unauthenticated for normal chats — Fixed (follow-up)

- **Where:** `privatechat/app.py` middleware `require_app_token`, `privatechat/__main__.py`,
  `privatechat/static/app.js` (`APP_TOKEN`, `api()`, the `pagehide` handler), `run.sh`.
- **Issue:** the server binds `127.0.0.1` and rejects foreign `Host` and cross-site writes, which
  stops other machines and websites. It did not stop **other local processes or other local users**:
  anyone who could open a TCP connection to `127.0.0.1:3050` could read every normal chat (bypassing
  the 0700 data directory from L4), lock the vault, or wipe it. Secret chats still needed the vault token.
- **Fix:** every `/api/*` request must carry a per-launch random key in `X-App-Token` (compared in
  constant time; 401 otherwise). `run.sh` makes the key with `openssl rand`, passes it to the backend
  in `PC_APP_TOKEN` (an environment only the same user can read) and to Brave through a 0600 launch
  page made with `mktemp` in `$XDG_RUNTIME_DIR` (plain `mktemp` if unset), removed on exit; the key is
  never on a command line. The page reads it from `?k=` and keeps it in memory; no cookies or browser
  storage. The lock request on tab close used `navigator.sendBeacon`, which can't send headers and
  would have been refused, leaving the vault unlocked; it is now a `keepalive` fetch with the key.
  `Referrer-Policy: no-referrer` on every response keeps `?k=` out of any outgoing request.
  `/` and `/static/*` stay public (the browser can't add headers to `<script>`/`<link>` loads).
  Tests: `tests/test_app_token.py`; real Chromium in `tests/test_browser_lock.py` (tab close locks
  the vault, reload keeps working, a page without the key explains and reads nothing).
- **Remaining:** another program running as the **same** user can still read the key (from the
  backend's environment, the launch page or Brave's history in `profile/`); it could read `data/`
  directly anyway.

### L6. SearXNG secret file world-readable — Fixed

- **Where:** `run.sh:72`.
- **Fix:** commit "Write web-stack/.env with mode 0600" (created under `umask 077`). Existing
  `.env` files keep their mode; `chmod 600 web-stack/.env` once.

---

## Informational (not changed)

- **I1. Tor SOCKS and SearXNG are open to every local process.** `127.0.0.1:9050` and
  `127.0.0.1:8888` need no credentials. Low impact (they only give anonymous web access).
  On **Docker Engine older than 28.0**, ports published on `127.0.0.1` could be reached by hosts on
  the same LAN segment; SearXNG would be usable by neighbours (Tor's `SocksPolicy` only accepts
  `172.16.0.0/12` and `127.0.0.1`, which rejects LAN sources). Use Docker ≥ 28.
- **I2. Tor ControlPort has no authentication** (`web-stack/tor/torrc:7-8`). It is bound to
  loopback inside the container and not published, so only processes in that container can reach it.
  Setting `CookieAuthentication 1` would cost nothing.
- **I3. Unlock throttle is in memory** (`privatechat/throttle.py`) and resets when the backend
  restarts. It slows online guessing only; the real protection against guessing is Argon2 on a
  copied `vault.bin`, so a strong password remains essential (README already says so).
- **I4. Secret text hashes in memory.** `token_cache` (`privatechat/app.py:271`) keeps SHA-256
  hashes of message texts (secret ones included) after locking. A short secret message could be
  confirmed by brute force from a memory dump. Memory is outside the threat model (README), but the
  cache could be cleared on lock.
- **I5. Key material copies.** `hash_secret_raw` returns immutable `bytes`, and `bytes(key)` is
  passed to `AESGCM`; those copies, and the password `str`, can't be wiped in Python. Known and
  documented ("best effort").
- **I6. Password is not Unicode-normalised** (`privatechat/crypto.py:55`). A password typed with a
  different input method (NFC vs NFD) would not unlock. Availability only; consider NFC.
- **I7. Old vault copies on disk** survive in freed blocks and backups under the password current
  at the time (README "Change password and wipe"). Already documented; full-disk encryption is the
  real fix.

---

## Checked and found sound

- **Vault crypto** (`privatechat/crypto.py`, `privatechat/vault.py`): Argon2id (t=3, 64 MiB, p=4,
  16-byte random salt) → 256-bit key; AES-256-GCM with a fresh random 96-bit nonce per save and the
  whole header as AAD; no password hash stored; wrong password = tag failure. Key held in a
  `bytearray` only while unlocked and zeroed on lock / password change; `change_password` compares
  derived keys in constant time. Writes go to a `mkstemp` file (0600) → `fsync` → `os.replace`;
  no plaintext temp files. Idle lock uses `CLOCK_BOOTTIME`, so suspend counts.
- **Leaks:** no access log; `httpx`/`httpcore` loggers raised to WARNING so fetched URLs are not
  logged; streaming errors log only the exception class; core dumps disabled
  (`privatechat/__main__.py`); the frontend uses no `localStorage`/`sessionStorage`/IndexedDB/cookies;
  `Cache-Control: no-store` everywhere. Secret-chat summaries, notes, web steps and sources are stored
  through the vault, never `chats.db`. The vault password is never sent to the model.
- **Server binding:** `uvicorn.run(host="127.0.0.1")` (`privatechat/__main__.py:17`);
  `TrustedHostMiddleware` blocks DNS rebinding; CSRF check uses `Sec-Fetch-Site` / `Origin`
  (`Origin: null` is rejected); OpenAPI/docs endpoints are disabled; no CORS headers.
- **XSS:** user messages use `textContent`; assistant Markdown is HTML-escaped (including quotes)
  *before* the few regex replacements (`app.js:52-71`); chat titles, web steps, approval cards and
  the reader view use `textContent`; image `src` is built only from magic-byte-checked MIME + base64.
- **Attachments / path traversal / injection:** uploads never touch the filesystem (stored inside
  SQLite or the vault); names are basename-cleaned and length-capped (`attachments.py:57`); type is
  decided from bytes. `open_file` only looks up in-memory attachment names. All SQL is parameterised.
  No `subprocess`/`os.system`/`eval` in the backend.
- **Tor routing:** `socks5h://` (DNS at the exit), `trust_env=False` (no `HTTP(S)_PROXY` bypass),
  no direct-connection fallback (`tor_check` turns web off instead), cookies refused, redirects
  followed by hand with scheme check (and now host check), bodies capped on raw bytes with
  compression refused, 60 s total deadline, per-chat `IsolateSOCKSAuth` circuits. SearXNG uses Tor
  for all outgoing requests (`using_tor_proxy`, `all://` proxies), image proxy and metrics off.
- **Link rule:** model-invented URLs are refused in every mode (`web/tools.py:98`), fragments
  ignored, malformed URLs don't crash.
- **Dependencies:** after the pypdf upgrade, `pip-audit` on the exported lock finds no known
  vulnerabilities (fastapi 0.162.0, starlette 1.9.0, uvicorn 0.54.0, httpx 0.28.1, h11 0.16.0,
  cryptography 50.0.2, argon2-cffi 25.1.0, pydantic 2.14.1, pypdf 6.21.4). Container images are
  pinned (`alpine:3.24.1`, `searxng/searxng:2026.10.25-54ca1d9cf`).

## Test note

`tests/test_web_pages.py::test_parser_fast_with_many_tags` (asserts parsing 1 s) fails in the
review environment both before and after these changes (~1.5 s on a slow shared CPU). It is a
performance check unrelated to these fixes; everything else passes.
