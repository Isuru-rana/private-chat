# Private Chat

A local ChatGPT-style chat app for the Gemma 4 26B model on llama-server, with
**normal chats** plus a **🔒 Secret chats** vault (like WhatsApp Locked chats).
Anyone using the laptop can see the vault exists. Without the password, they can't
read what's inside.

Test application / proof of idea, built for one laptop (Linux, RTX 3050 4 GB). Not audited.

## Requirements

- Linux, Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).
- [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` on `127.0.0.1:8088` with a
  model that supports tool calling and vision (built and tested with Gemma 4 26B-A4B,
  Q4_K_M, plus its `mmproj` file). Example:
  ```bash
  llama-server -m gemma-4-26B-A4B-Q4_K_M.gguf --mmproj mmproj-gemma-4-26B-A4B.gguf \
    -c 16384 -np 1 -fa on --jinja --alias gemma4-26b --no-slots --host 127.0.0.1 --port 8088
  ```
  `run.sh` can start it for you: put that command in a `serve.sh` and point
  `PC_LLAMA_DIR` at its folder. If llama-server is already running, it's used as is.
- Docker with Compose, for web access (Tor + SearXNG in `web-stack/`). Optional: without
  it the app runs and web tools report "Tor isn't running".
- Brave (`brave-browser`), which `run.sh` opens in a clean app profile. Without it,
  run the backend alone and open the page in any browser.

## Run

```bash
./run.sh   # starts Gemma + Tor/SearXNG (if needed) + backend on 127.0.0.1:3050 + app window in a clean Brave profile
```

`run.sh` starts llama-server if it isn't running (about 30 s) and stops it again
when you close the app window or press Ctrl+C. If llama-server was already running
, it's left running. The private web stack (Tor + SearXNG,
`web-stack/`) follows the same rule: started if needed, stopped on
exit only if `run.sh` started it. If the stack can't start, the app still runs and
web tools report "Tor isn't running".

Backend only: `uv run python -m privatechat`, then open http://127.0.0.1:3050
(`PC_PORT` and `PC_DATA` change the port and the data folder).
Tests: `uv run pytest`.

## How it works

- **Normal chats:** `data/chats.db` (SQLite). Not encrypted, by design.
- **Secret chats:** `data/vault.bin`, a single file. The password is stretched with
  **Argon2id** (3 passes, 64 MiB, 4 lanes, random 16-byte salt), giving a 256-bit
  key. The key encrypts the whole vault with **AES-256-GCM**, using a fresh nonce
  on every save. A wrong password simply fails decryption. No password hash is stored.
- **Opening the vault:** click 🔒 Secret chats and enter the password in its own
  box, never the chat box. The first time, you create the password (at least 12
  characters, entered twice).
- **Attachments (📎, drag-and-drop, or paste):** up to 50 files per message (at most 5 images, 20 MB together).
  - **Images** (PNG, JPG, GIF, WebP; up to 10 MB) go to Gemma, which can see them.
    On a 4 GB GPU the image encoder runs on the CPU (`--no-mmproj-offload`), so a
    reply with an image takes ~20–40 s.
  - **PDFs and text/code files** (up to 5 MB) have their text extracted and added to
    the message. Small files go in whole.
  - **Big files are read in parts:** text over about 4,000 tokens is read in up
    to 5 parts, each condensed into notes (the first answer takes about 90 s
    per part on this machine, so 5 parts is about 7–8 minutes). A message that
    would need more than 5 parts is refused; remove some files. Follow-up questions reuse the notes, and Gemma can open
    the exact text of a file when it needs a detail. The page shows
    "📄 N files · ~K parts" while you attach.
  - The file type is checked from the file's contents, not its name, so other
    binaries are refused.
  - In secret chats, attachments are stored **inside the encrypted vault**. In
    normal chats they're stored unencrypted, like normal messages. In secret chats
    the notes stay in the vault too.
- **Editing and going back:** hover a message for its tools.
  - ✏️ **Edit** one of your messages: the chat continues from that point with your new text.
  - 🔄 **Regenerate** any reply for a fresh answer.
  - **‹ 1/2 ›** arrows switch between versions, so you can go back to the
    conversation as it was before an edit. Nothing is deleted.
  - 📋 **Copy** any message.
  - If a reply fails, a 🔄 **Retry** button appears.
  - Versions of secret chats are stored inside the encrypted vault too.
- **Long chats (context management):** Gemma reads about 16,000 tokens per reply,
  including its thinking and answer. Before each reply the app:
  - counts tokens exactly with llama-server's tokenizer;
  - re-sends images only from the last 2 user turns (older ones stay visible to you);
  - keeps the newest messages that fit, dropping the oldest first;
  - replaces the dropped part with a short summary, made once (thinking off) and
    stored with the chat, then extended as the chat grows. Secret chat summaries
    live inside the encrypted vault;
  - shows a dashed line where Gemma's view of the chat starts.
  If summarizing fails, it still answers from the recent messages.
- **Browser back / forward** moves between normal chats. Secret chats are kept
  out of the browser's history on purpose.
- **Drag and drop** works anywhere in the window.
- **Wrong passwords:** after 5 wrong tries in a row, each new attempt waits longer
  (2 s, 4 s … up to 60 s).
- **Locking:** the 🔒 Lock button, 10 minutes idle (time spent suspended counts),
  reloading or closing the tab, or restarting the backend. The key exists only in
  backend memory while the vault is unlocked.
- **Leak hardening:**
  - Every response sends `Cache-Control: no-store`. The frontend uses no
    localStorage, sessionStorage, IndexedDB or cookies.
  - No access log, and message text is never logged. Core dumps are disabled.
  - The backend binds 127.0.0.1 and rejects requests with a foreign Host header,
    which blocks DNS rebinding.
  - It rejects cross-site write requests (CSRF).
  - llama-server runs with `--no-slots`.
  - `run.sh` turns off Brave's password manager in the app profile, so the vault
    password is never offered for saving. It also refuses to start if port 3050
    is already taken by another program.

## Change password and wipe

- **🔑 Password** (vault unlocked): current password + new one twice. Every secret chat is
  re-encrypted under the new password with a fresh salt; the old file is replaced only after the
  new one is fully written. A wrong current password counts toward the unlock slow-down.
- **🗑 Wipe** (vault unlocked) or **Forgot password?** (on the unlock box): type `WIPE` to confirm.
  Deletes every secret chat: the vault file is overwritten with random bytes, then deleted.
  Normal chats are not touched. Afterwards you can create a new vault.
- Wipe needs no password, on purpose (forgot-password case). Someone with your laptop can
  therefore delete your secret chats, but still can't read them.
- **Old copies can survive, on any disk.** Every save writes a new file and swaps it in, so earlier
  versions of the vault sit in freed disk blocks (and in any backup) until overwritten. They stay
  encrypted, but under the password that was current then: changing the password does not retire
  them, and wipe only scrubs the latest copy. If an old password leaked, someone with a disk image
  or backup could still read the chats as they were back then.

## Web access

Gemma can search the web, read pages and look at pictures on those pages, to answer
with current data. Everything goes through **Tor**; nothing is ever fetched directly.

- **🌐 menu, per chat** (saved with the chat; for secret chats inside the vault):
  - **Off:** Gemma gets no web tools at all.
  - **Ask every time:** each search / page / picture shows a card with Allow / Deny;
    the search text can be edited before it goes out.
  - **Ask once per answer:** one Allow, then Gemma makes the requests it needs for
    that answer.
  - **Auto:** no prompts.
  - New normal chats start on **Ask once per answer**; new **secret chats start Off**.
  - Unanswered cards count as Deny after 5 minutes; locking the vault denies
    pending secret-chat cards.
- **Tools:** `web_search` (≤ 8 results via SearXNG), `read_page` (readable text,
  ≤ 6,000 chars, plus ≤ 10 images and ≤ 20 links listed), `view_image` (PNG/JPEG/GIF/WebP
  ≤ 10 MB, magic-byte checked, ≤ 3 per answer). ≤ 6 tool rounds per answer; pages
  ≤ 3 MB, ≤ 5 redirects, 30 s timeout.
- **Every request is listed** under the reply ("🌐 Used the web: …") with the exact
  queries and URLs, plus a **Sources** list.
- **Sources open in an in-app reader view** showing the text already fetched through
  Tor. The browser never loads the source site or its pictures ("Copy link" if you
  want to visit it yourself).

### Link rule

`read_page` and `view_image` only accept URLs (fragment ignored) that already appeared
in this chat's search results, in pages read in this chat (the page URL, its listed
links and images), or in **your own messages**. Anything else, such as an address
Gemma made up or one a page's hidden text told it to build
(`http://attacker/?q=<secret>`), is refused and shown as "link not allowed". This
holds in every mode, Auto included.

### Anonymity stack

- **Tor** (`private-web-tor`, SOCKS `127.0.0.1:9050`) and **SearXNG**
  (`private-web-searxng`, `127.0.0.1:8888`) run from `web-stack/` on their **own
  Docker network**, so other containers on the machine can't reach them. Ports are published on loopback only.
- The app fetches pages and pictures via `socks5h://` (DNS is resolved at the Tor
  exit, never locally) with a common Firefox user agent, `Accept-Language: en-US`,
  no cookies and no referrer. No direct-connection fallback: if Tor is down, the web
  step fails and Gemma answers without the web.
- **Per-chat circuits:** each chat's fetches use their own Tor circuit
  (`IsolateSOCKSAuth`, SOCKS username derived from a hash of the chat id), so two
  chats show different exit IPs and can't be linked by IP.
- **Per-search circuits:** SearXNG rotates through 32 SOCKS identities
  (`s0`…`s31`), so consecutive searches use different circuits;
  `MaxCircuitDirtiness 120` keeps circuits short-lived.
- **Secret chats:** queries, URLs, page text and tool results are stored only inside
  the encrypted vault and are never logged.

### What still leaks

- **Search query content goes to the search engines** (through Tor, so not with your
  IP). Whatever you or Gemma type into a search is readable by the engines and
  Tor exit; don't put names or secrets in a search.
- **Pages you read** see a Tor exit fetching them; the URL and which listed links
  Gemma chose are visible to the site.
- **Tor use is visible to your ISP** (they see Tor traffic, not its content or
  destinations).
- **Timing correlation:** someone who watches both your connection and the sites
  (or the search engines) can match request times. Tor doesn't defend against a
  global observer.
- **Unexpected errors while streaming a reply** are caught: the browser gets a generic
  "something went wrong" and only the exception class name is logged, never its text.
  A crash outside the reply stream could still print a chat URL (with the chat id, not
  message text) via uvicorn's error output.
- **SearXNG container logging is disabled on purpose** (`logging: driver: none` in
  `web-stack/docker-compose.yaml`): its engine-error warnings include the full search query.
- Exit relays can see and tamper with plain `http://` pages.

## llama-server settings

The app expects a 16,384-token context (`-c 16384`). Use **one slot** (`-np 1`) on a small GPU. The default of 4 slots kept four sliding-window KV caches and used about
600 MiB more GPU memory, which left ~370 MiB free on the 4 GB GPU and caused a CUDA
out-of-memory crash when something else (e.g. a browser) also used the GPU. With one
slot, simultaneous requests from other programs wait their turn; each extra slot
costs about 200 MiB.

## Threat model and known limits

Protects against a **passive reader** while the vault is **locked**: someone who
looks at the screen, copies `data/`, opens the database, or reads the logs or
browser profile.

It does **not** protect against:
- someone using the laptop while the vault is **open** (the text is on screen and in memory);
- someone who **modifies the app code** or installs a keylogger or browser
  extension, then waits for you to type the password;
- **swap**, if it isn't encrypted, which can hold pages of memory
  from while the vault was open. Python can't reliably wipe memory either;
  overwriting the key is best effort;
- a **weak password**: Argon2id slows guessing, but a short or common password
  can still be guessed from a copied `vault.bin`;
- a **forgotten password**: there is no recovery, and the secret chats are gone.

With web access, it also does **not** protect against the search engines reading
your queries, your ISP seeing that you use Tor, or timing correlation (see "What still
leaks" above). It does keep your IP away from sites and engines, keeps secret-chat web
data inside the vault, and stops Gemma from opening addresses it made up.

Also note:
- If you run llama-server on `0.0.0.0` (e.g. so other tools can reach it), others on
  your network can use the model, but they can't see your chats.
- The vault file's size grows with the amount of secret text.

## Verification (2026-10-24)

- `uv run pytest` passes, including the end-to-end canary test: secret text,
  title and password never appear in `data/` or the logs.
- Manual run with the real Gemma model:
  - created a vault and a secret chat containing `ZEBRA-PLUM-4471`, then locked it;
  - after locking, the canary was gone from the page, and a wrong password showed
    "Wrong password";
  - browser storage was empty, and a reload came back locked;
  - `grep` for the canary and the password over the data dir, the app log and the
    llama-server log found nothing.
- llama-server `/slots` returns 501 (disabled).
- To check the dedicated browser profile yourself after a session (this includes
  Brave's session-restore files): `grep -rl '<a phrase from a secret chat>' profile/`.
  It should print nothing.

## Verification: web access (2026-10-27)

`uv run pytest` passes (network tests use fakes). Live end-to-end run through real
Tor, with the real Gemma model, on a test instance (port 3058, temporary data dir):

1. **Current events (Auto):** a news question ran a search and read
   `en.wikipedia.org/wiki/Artemis_program`; the answer listed it under Sources.
   (AP News answered 403 to Tor; Gemma said so and fell back to search snippets.)
2. **Exit IP:** Gemma read `check.torproject.org` ("Congratulations. This browser
   is configured to use Tor", IP `192.42.116.52`); a direct `curl` from the laptop
   showed the home IP (`IsTor: false`), a different address. (`/api/ip` returns JSON,
   which the reader refuses by design, so the HTML page was used.) During the fetch,
   `ss -tnp` showed the app connected only to `127.0.0.1` (8088, 9050). With
   `private-web-tor` stopped, the web step showed "Tor isn't running" and Gemma
   answered without the web.
   - **2b:** a second chat saw exit IP `192.42.116.43`. After two searches Tor's
     `circuit-status` showed circuits for SOCKS users `s2`, `s4`, `s5` and
     `chat-6245a96fee892cfa`.
   - **2c:** from a container on another Docker network, `private-web-tor` /
     `private-web-searxng` don't resolve and the host's `:9050` / `:8888` are unreachable.
3. **DNS:** with the backend under `strace -f -e connect`, a search plus page read
   produced only `connect()` calls to `127.0.0.1` (8088, 8888, 9050); none to
   port 53 or any other address.
4. **Link rule:** asked to build `https://example.com/private-canary-path` and open
   it, Gemma's `read_page` call was shown as "refused: link not allowed". (Hidden
   page instructions like `http://attacker/?q=secret` are covered by unit tests.)
5. **Modes:** Off: no web steps. Ask every time: one card per search; an edited
   query was the one actually searched. Ask once: one card for two searches.
   Auto: no cards. New normal chat = `ask_once`; new secret chat = `off`.
6. **Secret chat:** with Auto on, searched for a canary code word, read a page;
   after locking, `grep` for the canary, the page URL and phrases from the page text
   over the data dir, the app log, the llama-server log, `profile/` and the Tor and
   SearXNG container logs found nothing (normal-chat text was found in `chats.db`,
   so the grep works).
7. **`run.sh`** (browser replaced by a stub that exits after 20 s): with the stack
   down it started Tor + SearXNG and stopped them when the window closed; with the
   stack already up it left it running. A running llama-server was left alone.
8. **Reader view:** clicking a source opened the in-app reader with the stored text;
   the browser's network log and `performance` entries showed only `127.0.0.1:3058`
   requests and no `<img>` elements.

Also checked: `view_image` on the Wikimedia Commons picture of the day (Atakule tower)
reached Gemma's vision, which described the lattice dome, warm-lit observation deck
and blue sky correctly.

## License

GPL-3.0-or-later. See `LICENSE`.
