"""HTTP API: normal chats (SQLite) + secret chats (encrypted vault), streamed replies."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from .attachments import AttachmentError, process
from .crypto import DEFAULT_KDF, KdfParams, WrongPassword
from . import agent, context
from .llm import MAX_TOKENS, LLMError, complete, count_tokens, slots_disabled, stream_chat
from .store import NormalStore
from .throttle import Throttle
from .vault import Vault, VaultLocked, boottime
from .web.net import isolation_for_chat, tor_client, tor_reachable
from .web.tools import WebSession, normalize_url, urls_in_text

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("privatechat")


class Password(BaseModel):
    password: str


class PasswordChange(BaseModel):
    old: str
    new: str


class WipeConfirm(BaseModel):
    confirm: str


class Title(BaseModel):
    title: str = "New chat"
    web_mode: str | None = None


class WebMode(BaseModel):
    mode: str


class Approval(BaseModel):
    id: str
    allow: bool
    query: str | None = None


class Upload(BaseModel):
    name: str = "file"
    mime: str = ""
    data: str


class Message(BaseModel):
    content: str = ""
    attachments: list[Upload] = []
    edit_of: str | None = None  # id of an earlier user message this replaces (new version)


class Regenerate(BaseModel):
    parent: str  # user message whose reply should be generated again


class Switch(BaseModel):
    message_id: str


def _alternating(messages: list[dict]) -> list[dict]:
    """Merge back-to-back turns of the same role (left behind by a failed reply);
    chat templates require user/assistant to alternate."""
    out: list[dict] = []
    for m in messages:
        if out and out[-1]["role"] == m["role"]:
            prev = out[-1]
            merged = {"id": m.get("id"), "role": m["role"], "content": (prev["content"] + "\n\n" + m["content"]).strip()}
            atts = (prev.get("attachments") or []) + (m.get("attachments") or [])
            if atts:
                merged["attachments"] = atts
            out[-1] = merged
        else:
            out.append(dict(m))
    return out


def _prepare(body: "Message", allow_empty: bool = False) -> list[dict]:
    """Validate a send request; returns processed attachments."""
    try:
        atts = process([u.model_dump() for u in body.attachments])
    except AttachmentError as e:
        raise HTTPException(400, str(e)) from None
    if not allow_empty and not body.content.strip() and not atts:
        raise HTTPException(400, "empty message")
    return atts


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


APPROVAL_TIMEOUT = 300  # seconds; no answer counts as Deny
NO_ANSWER = "I couldn't finish an answer — try again or rephrase."
TOR_DOWN = {"kind": "search", "status": "error", "detail": "Tor isn't running"}


def allowed_urls(history: list[dict]) -> set[str]:
    """Links the model may open this turn: URLs saved with earlier web answers on
    this branch plus URLs the user wrote. Never raises on malformed data."""
    raw: list = []
    for m in history:
        meta = m.get("meta")
        urls = meta.get("urls") if isinstance(meta, dict) else None
        if isinstance(urls, list):
            raw += urls
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            raw += urls_in_text(m["content"])
    return {n for n in (normalize_url(u) for u in raw if isinstance(u, str)) if n}


def _default_web_session(chat_id: str, allowed: set[str]) -> WebSession:
    return WebSession(allowed, client=tor_client(isolation_for_chat(chat_id)))


CONTEXT_SIZE = int(os.environ.get("PC_CTX", "16384"))  # must match llama-server's -c
SUMMARY_MAX_TOKENS = 600


def create_app(
    data_dir: Path,
    *,
    kdf: KdfParams = DEFAULT_KDF,
    llm: Callable[[list[dict]], AsyncIterator[tuple[str, str]]] = stream_chat,
    clock: Callable[[], float] = boottime,
    slots_check: Callable = slots_disabled,
    idle_poll: float = 5.0,
    allowed_hosts: list[str] = ("127.0.0.1", "localhost"),
    counter: Callable[[str], Awaitable[int]] = count_tokens,
    summarizer: Callable[..., Awaitable[str]] = complete,
    context_budget: int = CONTEXT_SIZE - MAX_TOKENS - 300,
    web_session_factory: Callable[[str, set[str]], WebSession] | None = None,
    tor_check: Callable[[], Awaitable[bool]] = tor_reachable,
) -> FastAPI:
    web_session_factory = web_session_factory or _default_web_session
    # httpx logs every request URL at INFO; web URLs of secret chats must never reach a log
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    data_dir = Path(data_dir)
    store = NormalStore(data_dir / "chats.db")
    vault = Vault(data_dir / "vault.bin", kdf=kdf, clock=clock)
    throttle = Throttle(clock=clock)
    session = {"token": None}
    unlock_lock = asyncio.Lock()  # one unlock attempt at a time, so the throttle can't be raced
    approvals: dict[str, tuple[asyncio.Future, str, bool]] = {}  # id -> (future, chat_id, secret)

    def deny_secret_approvals() -> None:
        """Lock: pending secret-chat approvals resolve as Deny (thread-safe)."""
        for aid, (fut, _, secret) in list(approvals.items()):
            if secret:
                approvals.pop(aid, None)
                fut.get_loop().call_soon_threadsafe(
                    lambda f=fut: f.done() or f.set_result({"allow": False, "query": None}))

    def end_session() -> None:
        vault.lock()
        session["token"] = None
        deny_secret_approvals()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def idle_watch():
            while True:
                await asyncio.sleep(idle_poll)
                if not vault.is_unlocked:  # also runs the idle check
                    session["token"] = None
                    deny_secret_approvals()

        task = asyncio.create_task(idle_watch())
        try:
            yield
        finally:
            task.cancel()
            end_session()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.vault = vault
    app.state.approvals = approvals

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        # Block cross-site requests from other web pages (CSRF). Browsers send
        # Sec-Fetch-Site / Origin; non-browser local clients send neither.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if (site and site not in ("same-origin", "none")) or (
                origin and origin.split("://", 1)[-1] != host
            ):
                return JSONResponse({"error": "cross_site"}, status_code=403)
        return await call_next(request)

    # Added last so it runs first: rejects DNS-rebinding requests with a foreign Host.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    @app.exception_handler(VaultLocked)
    async def vault_locked(request: Request, exc: VaultLocked):
        # the vault auto-locked between the token check and the read
        session["token"] = None
        return JSONResponse({"detail": "locked"}, status_code=401)

    def require_token(x_vault_token: str | None = Header(default=None)) -> None:
        if not vault.is_unlocked:
            session["token"] = None
        token = session["token"]
        if token is None or x_vault_token is None or not secrets.compare_digest(x_vault_token, token):
            raise HTTPException(401, "locked")

    def open_session() -> dict:
        session["token"] = secrets.token_urlsafe(32)
        return {"token": session["token"]}

    token_cache: dict[bytes, int] = {}  # sha256(text) -> tokens; hashes only, never text

    async def count_text(text: str) -> int:
        key = hashlib.sha256(text.encode()).digest()
        if key not in token_cache:
            if len(token_cache) > 5000:
                token_cache.clear()
            token_cache[key] = await counter(text)
        return token_cache[key]

    async def count_message(m: dict) -> int:
        images = sum(a["kind"] == "image" for a in m.get("attachments", []))
        return await count_text(context.message_text(m)) + images * context.IMAGE_TOKENS

    async def web_turn(messages: list[dict], chat_id: str, mode: str, secret: bool, allowed: set[str]):
        """Run the agent loop; yields SSE strings, then ("final", dict) as the last item.
        The loop runs in a task so approval requests can be streamed while it waits."""
        q: asyncio.Queue = asyncio.Queue()
        web = web_session_factory(chat_id, allowed)
        timed_out = False  # nobody answered: deny the rest of this answer without waiting again

        async def approve(req: dict) -> dict:
            nonlocal timed_out
            if timed_out:
                return {"allow": False, "query": None}
            aid = secrets.token_urlsafe(16)
            fut = asyncio.get_running_loop().create_future()
            approvals[aid] = (fut, chat_id, secret)
            await q.put(("sse", _sse("approval", {"id": aid, **req})))
            try:
                verdict = await asyncio.wait_for(fut, APPROVAL_TIMEOUT)
            except asyncio.TimeoutError:
                timed_out = True
                verdict = {"allow": False, "query": None}
            finally:
                approvals.pop(aid, None)
            await q.put(("sse", _sse("approval_done", {"id": aid})))
            if secret and not vault.keep_alive():
                raise VaultLocked()  # locked while waiting: stop the turn, touch nothing more
            return verdict

        async def pump():
            try:
                async for kind, val in agent.run_turn(
                    messages, llm=llm, session=web, mode=mode, approve=approve,
                    count=count_text, budget=context_budget,
                ):
                    if secret and not vault.keep_alive():
                        raise VaultLocked()  # locked mid-turn: no further rounds or fetches
                    await q.put(("final", val) if kind == "final" else ("sse", _sse(kind, val)))
            except Exception as e:  # handed to the stream, which re-raises it
                await q.put(("raise", e))

        task = asyncio.create_task(pump())
        try:
            while True:
                kind, val = await q.get()
                if kind == "raise":
                    raise val
                yield val if kind == "sse" else ("final", val)
                if kind == "final":
                    return
        finally:
            with anyio.CancelScope(shield=True):  # client disconnect must not skip the cleanup
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                client = getattr(web, "client", None)
                if client is not None:
                    await client.aclose()

    def reply_stream(
        st, chat_id: str, save: Callable[..., None], history: list[dict], mode: str = "off", secret: bool = False
    ) -> StreamingResponse:
        async def gen():
            parts = []
            try:
                plan = await context.plan(
                    _alternating(history), count=count_message, budget=context_budget, summaries=st.summaries(chat_id)
                )
                if plan.needs_summary:
                    yield _sse("status", "Summarizing earlier messages…")
                    prev, rest = plan.needs_summary
                    try:
                        text = await summarizer(context.summary_prompt(prev, rest), max_tokens=SUMMARY_MAX_TOKENS)
                    except LLMError:
                        log.warning("could not summarize older messages; sending recent ones only")
                        text = ""
                    if text and plan.dropped[-1].get("id"):
                        st.save_summary(chat_id, plan.dropped[-1]["id"], text)
                        plan.summary = text
                if plan.dropped or plan.images_hidden:
                    yield _sse("context", {
                        "first_kept_id": plan.first_kept_id if plan.dropped else None,
                        "dropped": len(plan.dropped),
                        "images_hidden": plan.images_hidden,
                        "summarized": bool(plan.dropped and plan.summary),
                    })
                turn_mode, meta = mode, None
                if turn_mode != "off" and not await tor_check():  # never fall back to a direct fetch
                    yield _sse("web", TOR_DOWN)
                    meta = {"web": {"steps": [dict(TOR_DOWN)], "sources": []}, "urls": []}
                    turn_mode = "off"
                if turn_mode == "off":
                    async for kind, text in llm(plan.final_messages()):
                        if secret and not vault.keep_alive():
                            raise VaultLocked()  # a streaming secret reply counts as use
                        if kind == "content":
                            parts.append(text)
                        yield _sse(kind, text)
                    save("".join(parts), meta)
                else:
                    final = None
                    async with contextlib.aclosing(web_turn(
                        plan.final_messages(), chat_id, turn_mode, secret, allowed_urls(history)
                    )) as turn:
                        async for item in turn:
                            if isinstance(item, tuple):
                                final = item[1]
                            else:
                                yield item
                    content = final["content"]
                    if not content.strip():
                        content = NO_ANSWER
                        yield _sse("content", NO_ANSWER)
                    if final["steps"]:
                        meta = {"web": {"steps": final["steps"], "sources": final["sources"]},
                                "urls": final["new_urls"]}
                    save(content, meta)
                yield _sse("done", True)
            except LLMError as e:
                yield _sse("error", str(e))
            except VaultLocked:
                yield _sse("error", "vault locked")
            except Exception as e:
                # exception text could hold secret-chat URLs or queries: class name only, never the message
                log.warning("reply failed (%s)", type(e).__name__)
                yield _sse("error", "something went wrong")

        return StreamingResponse(gen(), media_type="text/event-stream")

    # --- pages ---

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # --- vault ---

    @app.get("/api/vault")
    def vault_status():
        return {"exists": vault.exists(), "unlocked": vault.is_unlocked}

    @app.post("/api/vault/create")
    def vault_create(body: Password):
        try:
            vault.create(body.password)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return open_session()

    @app.post("/api/vault/unlock")
    async def vault_unlock(body: Password):
        async with unlock_lock:
            wait = throttle.retry_after()
            if wait > 0:
                return JSONResponse({"retry_after": round(wait, 1)}, status_code=429)
            if not vault.exists():
                raise HTTPException(404)
            try:
                await asyncio.to_thread(vault.unlock, body.password)
            except WrongPassword:
                throttle.fail()
                return JSONResponse({"error": "wrong_password"}, status_code=401)
            throttle.success()
        if not await slots_check():
            log.warning("llama-server /slots endpoint is enabled; it can expose prompt text")
        return open_session()

    @app.post("/api/vault/lock")
    def vault_lock():
        end_session()
        return {"ok": True}

    @app.post("/api/vault/password", dependencies=[Depends(require_token)])
    async def vault_password(body: PasswordChange):
        async with unlock_lock:  # a wrong current password counts like a wrong unlock
            wait = throttle.retry_after()
            if wait > 0:
                return JSONResponse({"retry_after": round(wait, 1)}, status_code=429)
            try:
                await asyncio.to_thread(vault.change_password, body.old, body.new)
            except WrongPassword:
                throttle.fail()
                return JSONResponse({"error": "wrong_password"}, status_code=403)  # 401 would mean "locked"
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
            throttle.success()
        return {"ok": True}

    @app.post("/api/vault/wipe")
    async def vault_wipe(body: WipeConfirm):
        # No password: this is also the "forgot password" way out. Deleting can't reveal anything.
        if body.confirm != "WIPE":
            raise HTTPException(400, "type WIPE to confirm")
        async with unlock_lock:
            end_session()
            await asyncio.to_thread(vault.destroy)
            throttle.success()
        return {"ok": True}

    # --- chats (same routes for normal and secret; secret ones need the vault token) ---

    def add_chat_routes(prefix: str, get_store: Callable, deps: list, secret: bool) -> None:
        def found(value):
            if value is None:
                raise HTTPException(404)
            return value

        @app.get(prefix, dependencies=deps)
        def list_chats():
            return get_store().list_chats()

        @app.post(prefix, dependencies=deps)
        def create_chat(body: Title):
            try:
                return get_store().create_chat(body.title, web_mode=body.web_mode)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None

        @app.put(prefix + "/{chat_id}/web", dependencies=deps)
        def set_web_mode(chat_id: str, body: WebMode):
            st = get_store()
            try:
                st.set_web_mode(chat_id, body.mode)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
            except KeyError:
                raise HTTPException(404) from None
            return found(st.get_chat(chat_id))

        @app.get(prefix + "/{chat_id}", dependencies=deps)
        def get_chat(chat_id: str):
            return found(get_store().get_chat(chat_id))

        @app.delete(prefix + "/{chat_id}", dependencies=deps)
        def delete_chat(chat_id: str):
            found(get_store().delete_chat(chat_id) or None)
            return {"ok": True}

        def stream_reply(st, chat_id: str, user_id: str) -> StreamingResponse:
            history = st.history(chat_id, user_id)
            mode = found(st.get_chat(chat_id))["web_mode"]
            return reply_stream(
                st, chat_id,
                lambda text, meta=None: st.append_message(chat_id, "assistant", text, parent=user_id, meta=meta),
                history, mode, secret,
            )

        @app.post(prefix + "/{chat_id}/messages", dependencies=deps)
        def send(chat_id: str, body: Message):
            st = get_store()
            atts = _prepare(body, allow_empty=body.edit_of is not None)
            try:
                if body.edit_of is None:
                    user_id = st.append_message(chat_id, "user", body.content, atts)
                else:
                    orig = found(st.get_message(chat_id, body.edit_of))
                    if orig["role"] != "user":
                        raise HTTPException(400, "only your own messages can be edited")
                    atts = (orig.get("attachments") or []) + atts
                    if not body.content.strip() and not atts:
                        raise HTTPException(400, "empty message")
                    user_id = st.append_message(chat_id, "user", body.content, atts, parent=orig["parent"])
            except KeyError:
                raise HTTPException(404) from None
            return stream_reply(st, chat_id, user_id)

        @app.post(prefix + "/{chat_id}/regenerate", dependencies=deps)
        def regenerate(chat_id: str, body: Regenerate):
            st = get_store()
            try:
                msg = found(st.get_message(chat_id, body.parent))
            except KeyError:
                raise HTTPException(404) from None
            if msg["role"] != "user":
                raise HTTPException(400, "regenerate needs the user message the reply answers")
            return stream_reply(st, chat_id, msg["id"])

        @app.post(prefix + "/{chat_id}/switch", dependencies=deps)
        def switch(chat_id: str, body: Switch):
            st = get_store()
            try:
                st.switch(chat_id, body.message_id)
            except KeyError:
                raise HTTPException(404) from None
            return st.get_chat(chat_id)

    add_chat_routes("/api/chats", lambda: store, [], secret=False)
    add_chat_routes("/api/secret/chats", lambda: vault, [Depends(require_token)], secret=True)

    @app.post("/api/web/approve")
    async def web_approve(body: Approval, x_vault_token: str | None = Header(default=None)):
        entry = approvals.get(body.id)
        if entry is None:
            raise HTTPException(404)
        fut, _, secret = entry
        if secret:
            require_token(x_vault_token)
        approvals.pop(body.id, None)
        if not fut.done():
            fut.set_result({"allow": body.allow, "query": body.query})
        return {"ok": True}

    return app
