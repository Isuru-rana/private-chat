// Private Chat frontend. Deliberately keeps nothing in browser storage:
// the vault token lives only in this variable and dies with the page.
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  token: null,          // vault session token, memory only
  vaultExists: false,
  current: null,        // { kind: "normal" | "secret", id: string | null }
  chat: null,           // the open chat as last loaded (messages on the visible path)
  contextInfo: {},      // chatId -> what Gemma could see on the last reply (memory only)
  busy: false,
  pending: [],          // attachments waiting to be sent: { name, mime, data (base64) }
  newWebMode: null,     // web mode chosen for a chat that is not saved yet
};

const WEB_MODES = [
  ["off", "Off"],
  ["ask_each", "Ask every time"],
  ["ask_once", "Ask once per answer"],
  ["auto", "Auto"],
];
const defaultWebMode = (kind) => (kind === "secret" ? "off" : "ask_once");

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const MAX_FILES = 5;
const MAX_IMAGE = 10 * 1024 * 1024;
const MAX_FILE = 5 * 1024 * 1024;

// ---------- API ----------

async function api(path, { method = "GET", body, secret = false } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (secret) headers["X-Vault-Token"] = state.token || "";
  const resp = await fetch(path, { method, headers, body: body && JSON.stringify(body), cache: "no-store" });
  if (secret && resp.status === 401) {
    lockUI();
    throw new Error("locked");
  }
  return resp;
}

const base = (kind) => (kind === "secret" ? "/api/secret/chats" : "/api/chats");

// ---------- rendering ----------

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderMarkdown(src) {
  const parts = escapeHtml(src).split(/```/);
  return parts
    .map((part, i) => {
      if (i % 2 === 1) {
        const body = part.replace(/^[\w+-]*\n/, "");
        return `<pre><code>${body}</code></pre>`;
      }
      return part
        .replace(/`([^`\n]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/^#{1,6} (.+)$/gm, "<strong>$1</strong>")
        .replace(/\n/g, "<br>");
    })
    .join("");
}

function groupOf(ts) {
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (ts >= startToday) return "Today";
  if (ts >= startToday - 7 * 86400) return "Previous 7 days";
  return "Older";
}

function chatItem(chat, kind) {
  const li = document.createElement("li");
  li.className = "chat-item";
  if (state.current && state.current.kind === kind && state.current.id === chat.id) li.classList.add("active");
  const title = document.createElement("span");
  title.className = "title";
  title.textContent = chat.title;
  const del = document.createElement("button");
  del.className = "del";
  del.title = "Delete chat";
  del.textContent = "×";
  del.onclick = async (e) => {
    e.stopPropagation();
    if (!confirm(`Delete "${chat.title}"?`)) return;
    await api(`${base(kind)}/${chat.id}`, { method: "DELETE", secret: kind === "secret" });
    if (state.current && state.current.id === chat.id) newChat(kind);
    refreshLists();
  };
  li.append(title, del);
  li.onclick = () => openChat(kind, chat.id);
  return li;
}

async function refreshLists() {
  const normal = await (await api("/api/chats")).json();
  const nav = $("normal-list");
  nav.replaceChildren();
  let group = null;
  let ul = null;
  for (const chat of normal) {
    const g = groupOf(chat.updated);
    if (g !== group) {
      group = g;
      const label = document.createElement("div");
      label.className = "group-label";
      label.textContent = g;
      ul = document.createElement("ul");
      ul.className = "chat-list";
      nav.append(label, ul);
    }
    ul.append(chatItem(chat, "normal"));
  }

  const list = $("secret-list");
  list.replaceChildren();
  if (state.token) {
    try {
      const secret = await (await api("/api/secret/chats", { secret: true })).json();
      for (const chat of secret) list.append(chatItem(chat, "secret"));
    } catch {
      /* locked meanwhile */
    }
  }
}

function setUnlockedUI(on) {
  $("secret-entry").hidden = on;
  $("secret-section").hidden = !on;
  $("lock-btn").hidden = !on;
  $("pw-change-btn").hidden = !on;
  $("wipe-btn").hidden = !on;
}

function updateModeLabel() {
  $("mode-label").textContent = state.current && state.current.kind === "secret" ? "secret chat" : "";
}

function clearMessages() {
  $("messages").replaceChildren($("empty") || emptyEl());
  $("empty").hidden = false;
}

function emptyEl() {
  const d = document.createElement("div");
  d.id = "empty";
  d.className = "empty";
  d.textContent = "What can I help with?";
  return d;
}

function attachmentChip(att, onRemove) {
  const chip = document.createElement("div");
  chip.className = "chip";
  if (IMAGE_TYPES.includes(att.mime) && att.data) {
    const img = document.createElement("img");
    img.alt = att.name;
    img.src = `data:${att.mime};base64,${att.data}`;
    chip.append(img);
  } else {
    const icon = document.createElement("span");
    icon.className = "icon";
    icon.textContent = "📄";
    chip.append(icon);
  }
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = att.name + (att.truncated ? " (cut short)" : "");
  name.title = att.name;
  chip.append(name);
  if (onRemove) {
    const x = document.createElement("button");
    x.type = "button";
    x.className = "x";
    x.textContent = "×";
    x.title = "Remove";
    x.onclick = onRemove;
    chip.append(x);
  }
  return chip;
}

function addMessage(role, content, attachments = []) {
  $("empty").hidden = true;
  if (attachments.length) {
    const box = document.createElement("div");
    box.className = "msg-atts";
    for (const att of attachments) {
      if (att.kind === "image") {
        const img = document.createElement("img");
        img.alt = att.name;
        img.src = `data:${att.mime};base64,${att.data}`;
        box.append(img);
      } else {
        box.append(attachmentChip(att));
      }
    }
    $("messages").append(box);
  }
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  if (role === "user") div.textContent = content;
  else div.innerHTML = renderMarkdown(content);
  if (role === "user" && !content) div.hidden = true;
  $("messages").append(div);
  div.scrollIntoView({ block: "end" });
  return div;
}

// ---------- attachments ----------

function showAttachError(msg) {
  $("attach-error").textContent = msg;
}

function renderChips() {
  const box = $("chips");
  box.replaceChildren(
    ...state.pending.map((att, i) =>
      attachmentChip(att, () => {
        state.pending.splice(i, 1);
        renderChips();
      }),
    ),
  );
  box.hidden = state.pending.length === 0;
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function addFiles(files) {
  showAttachError("");
  for (const file of files) {
    if (state.pending.length >= MAX_FILES) {
      showAttachError(`You can attach at most ${MAX_FILES} files.`);
      break;
    }
    const isImage = IMAGE_TYPES.includes(file.type);
    if (file.size > (isImage ? MAX_IMAGE : MAX_FILE)) {
      showAttachError(`${file.name} is too big (${isImage ? "images: 10 MB" : "files: 5 MB"} max).`);
      continue;
    }
    const name = file.name || (isImage ? "pasted-image.png" : "file");
    state.pending.push({ name, mime: file.type, data: await readAsBase64(file) });
  }
  renderChips();
}

// ---------- chats ----------

// Approval cards that are on screen and unanswered: id -> chat kind. Leaving the chat removes the
// cards, so they are answered as Deny first (otherwise the turn waits 5 minutes and Send stays off).
const pendingApprovals = new Map();

async function denyPendingApprovals() {
  const open = [...pendingApprovals];
  pendingApprovals.clear();
  await Promise.all(
    open.map(([id, kind]) =>
      api("/api/web/approve", { method: "POST", body: { id, allow: false }, secret: kind === "secret" }).catch(() => {}),
    ),
  );
}

function newChat(kind = "normal", push = true) {
  denyPendingApprovals();
  state.current = { kind, id: null };
  state.chat = null;
  state.newWebMode = defaultWebMode(kind);
  clearMessages();
  updateModeLabel();
  updateWebButton();
  refreshLists();
  if (push && kind === "normal") history.pushState({ kind: "normal", id: null }, "", "#");
  $("input").focus();
}

async function fetchChat(kind, id) {
  const resp = await api(`${base(kind)}/${id}`, { secret: kind === "secret" });
  return resp.ok ? resp.json() : null;
}

async function openChat(kind, id, push = true) {
  if (state.current?.kind !== kind || state.current?.id !== id) denyPendingApprovals();  // re-clicking this chat keeps its card
  const chat = await fetchChat(kind, id);
  if (!chat) {
    if (kind === "normal" && !push) newChat("normal", false);
    return refreshLists();
  }
  state.current = { kind, id };
  renderChat(chat);
  updateModeLabel();
  updateWebButton();
  refreshLists();
  // Only normal chats go into the browser history: secret chat ids stay out of it.
  if (push && kind === "normal") history.pushState({ kind: "normal", id }, "", `#c/${id}`);
}

function toolButton(label, title, onclick) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.title = title;
  b.onclick = onclick;
  return b;
}

function copyButton(text) {
  const b = toolButton("📋", "Copy", async () => {
    try {
      await navigator.clipboard.writeText(text);
      b.textContent = "✓";
    } catch {
      b.textContent = "✗";
    }
    setTimeout(() => (b.textContent = "📋"), 1200);
  });
  return b;
}

function versionNav(m) {
  const nav = document.createElement("span");
  nav.className = "versions";
  const go = (delta) => async () => {
    const target = m.sibling_ids[m.version - 1 + delta];
    if (!target || state.busy) return;
    const { kind, id } = state.current;
    const resp = await api(`${base(kind)}/${id}/switch`, {
      method: "POST",
      body: { message_id: target },
      secret: kind === "secret",
    });
    if (resp.ok) renderChat(await resp.json());
  };
  const prev = toolButton("‹", "Previous version", go(-1));
  const next = toolButton("›", "Next version", go(1));
  prev.disabled = m.version <= 1;
  next.disabled = m.version >= m.versions;
  const label = document.createElement("span");
  label.textContent = `${m.version}/${m.versions}`;
  nav.append(prev, label, next);
  return nav;
}

function messageTools(m, index) {
  const tools = document.createElement("div");
  tools.className = `msg-tools ${m.role}`;
  if (m.versions > 1) tools.append(versionNav(m));
  if (m.role === "user") {
    tools.append(toolButton("✏️", "Edit", () => startEdit(index)));
  } else {
    tools.append(toolButton("🔄", "Regenerate", () => runTurn("regenerate", index)));
  }
  tools.append(copyButton(m.content));
  return tools;
}

function contextDivider(info) {
  const div = document.createElement("div");
  div.className = "context-divider";
  const parts = [];
  if (info.dropped) {
    parts.push(
      info.summarized
        ? "Gemma no longer sees the messages above. It works from a short summary of them."
        : "Gemma no longer sees the messages above.",
    );
  }
  if (info.images_hidden) parts.push("Older images aren't shown to Gemma anymore.");
  div.textContent = parts.join(" ");
  return div;
}

function renderMessages(messages) {
  clearMessages();
  const info = state.current && state.contextInfo[state.current.id];
  const cutId = info && (info.first_kept_id || (info.images_hidden ? messages[0]?.id : null));
  messages.forEach((m, i) => {
    if (cutId && m.id === cutId) $("messages").append(contextDivider(info));
    const el = addMessage(m.role, m.content, m.attachments || []);
    if (m.role === "assistant" && m.meta?.web) {
      if (m.meta.web.steps?.length) el.append(webPanel(m.meta.web.steps));
      if (m.meta.web.sources?.length) el.append(sourcesList(m.meta.web.sources));
    }
    if (m.id) $("messages").append(messageTools(m, i));
  });
}

function renderChat(chat, errorText = null) {
  state.chat = chat;
  renderMessages(chat.messages);
  const last = chat.messages[chat.messages.length - 1];
  if (last && last.role === "user") {
    // the reply to this message failed or never arrived
    const row = document.createElement("div");
    row.className = "msg error";
    row.textContent = errorText ? `Error: ${errorText}` : "No reply yet.";
    row.append(" ", toolButton("🔄 Retry", "Try the reply again", () => runTurn("retry", chat.messages.length - 1)));
    $("messages").append(row);
  }
}

function startEdit(index) {
  if (state.busy) return;
  const m = state.chat.messages[index];
  renderMessages(state.chat.messages.slice(0, index));
  if (m.attachments?.length) addMessage("user", "", m.attachments);
  const box = document.createElement("div");
  box.className = "edit-box";
  const area = document.createElement("textarea");
  area.value = m.content;
  area.spellcheck = false;
  area.setAttribute("autocomplete", "off");
  const row = document.createElement("div");
  row.className = "row";
  const cancel = toolButton("Cancel", "Keep the message as it was", () => renderChat(state.chat));
  cancel.className = "ghost";
  const save = toolButton("Send", "Send the edited message", () => runTurn("edit", index, area.value));
  row.append(cancel, save);
  box.append(area, row);
  $("messages").append(box);
  area.focus();
  area.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      save.click();
    } else if (e.key === "Escape") cancel.click();
  });
}

// ---------- web access ----------

const hostOf = (u) => {
  try {
    return new URL(u).host;
  } catch {
    return u;
  }
};

function stepText(s) {
  const what = s.kind === "search" ? s.query : s.url;
  if (s.kind === "search") return `Searching: ${what}`;
  if (s.kind === "page") return `Reading: ${hostOf(what)} (${what})`;
  return `Looking at picture: ${what}`;
}

const STEP_STATUS = { running: "…", done: "", refused: " (refused)", error: " (failed)", denied: " (denied)" };

function stepLine(s) {
  const line = document.createElement("div");
  line.className = `web-step ${s.status}`;
  line.textContent = stepText(s) + (STEP_STATUS[s.status] ?? "") + (s.detail && s.status !== "done" ? ` - ${s.detail}` : "");
  return line;
}

function webPanel(steps) {
  const count = (k) => steps.filter((s) => s.kind === k).length;
  const det = document.createElement("details");
  det.className = "web-panel";
  const sum = document.createElement("summary");
  sum.textContent = `🌐 Used the web: ${count("search")} searches, ${count("page")} pages, ${count("image")} pictures`;
  det.append(sum);
  for (const s of steps) det.append(stepLine(s));
  return det;
}

function sourcesList(sources) {
  const box = document.createElement("div");
  box.className = "web-sources";
  const label = document.createElement("span");
  label.textContent = "Sources:";
  box.append(label);
  for (const src of sources) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "source-btn";
    b.textContent = src.title || hostOf(src.url);
    b.title = "Read the saved text";
    b.onclick = () => openReader(src);
    box.append(b);
  }
  return box;
}

function clearReader() {
  $("reader-title").textContent = "";
  $("reader-url").textContent = "";
  $("reader-text").textContent = "";
  $("reader-copy").onclick = null;
}

function openReader(src) {
  $("reader-title").textContent = src.title || hostOf(src.url);
  $("reader-url").textContent = src.url;
  $("reader-text").textContent = src.text || "";
  const copy = $("reader-copy");
  copy.textContent = "Copy link";
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(src.url);
      copy.textContent = "Copied";
    } catch {
      copy.textContent = "Couldn't copy";
    }
  };
  $("reader-dialog").showModal();
}

function approvalCard(req, kind, chatId) {
  const card = document.createElement("div");
  card.className = "approval";
  card.setAttribute("role", "group");
  const label = document.createElement("div");
  label.className = "approval-label";
  label.textContent =
    req.kind === "search" ? "Gemma wants to search:"
      : req.kind === "page" ? "Gemma wants to open:"
      : req.kind === "image" ? "Gemma wants to look at picture:"
      : "Gemma wants to use the web to answer.";
  card.setAttribute("aria-label", label.textContent);
  card.append(label);
  let field = null;
  if (req.kind === "search") {
    field = document.createElement("input");
    field.type = "text";
    field.value = req.query || "";
    field.spellcheck = false;
    field.setAttribute("autocomplete", "off");
    field.setAttribute("aria-label", "Search text");
    card.append(field);
  } else if (req.url) {
    const u = document.createElement("code");
    u.className = "approval-url";
    u.textContent = req.url;
    card.append(u);
  }
  const row = document.createElement("div");
  row.className = "row";
  pendingApprovals.set(req.id, kind);
  const answer = async (allow) => {
    pendingApprovals.delete(req.id);
    for (const b of row.querySelectorAll("button")) b.disabled = true;
    const body = { id: req.id, allow };
    if (allow && field) body.query = field.value;
    try {
      await api("/api/web/approve", { method: "POST", body, secret: kind === "secret" });
    } catch {
      /* card goes away with approval_done or the end of the stream */
    }
  };
  const deny = toolButton("Deny", "Don't let Gemma do this", () => answer(false));
  deny.className = "ghost";
  const allow = toolButton("Allow", "Let Gemma do this", () => answer(true));
  row.append(deny, allow);
  card.append(row);
  // Enter only approves after the user has typed in this field themselves
  let edited = false;
  if (field) {
    field.addEventListener("input", () => (edited = true));
    field.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && edited) (e.preventDefault(), allow.click());
    });
  }
  return card;
}

function webMode() {
  const cur = state.current;
  if (!cur) return "ask_once";
  return (cur.id ? state.chat?.web_mode : state.newWebMode) || defaultWebMode(cur.kind);
}

function updateWebButton() {
  const mode = webMode();
  const name = WEB_MODES.find(([v]) => v === mode)?.[1] || mode;
  $("web-btn").setAttribute("aria-label", `Web access: ${name}`);
  $("web-btn").classList.toggle("on", mode !== "off");
}

function closeWebMenu() {
  $("web-menu").hidden = true;
  $("web-btn").setAttribute("aria-expanded", "false");
}

function openWebMenu() {
  const menu = $("web-menu");
  const mode = webMode();
  menu.replaceChildren(
    ...WEB_MODES.map(([value, text]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "menuitemradio");
      b.setAttribute("aria-checked", String(value === mode));
      b.textContent = `${value === mode ? "✓ " : ""}${text}`;
      b.onclick = () => {
        closeWebMenu();
        $("web-btn").focus();
        setWebMode(value);
      };
      return b;
    }),
  );
  menu.hidden = false;
  $("web-btn").setAttribute("aria-expanded", "true");
  menu.querySelector('[aria-checked="true"]').focus();
}

async function setWebMode(mode) {
  const cur = state.current;
  if (!cur) return;
  if (!cur.id) {
    state.newWebMode = mode;
  } else {
    try {
      const resp = await api(`${base(cur.kind)}/${cur.id}/web`, { method: "PUT", body: { mode }, secret: cur.kind === "secret" });
      if (resp.ok) {
        const chat = await resp.json();
        if (state.chat) state.chat.web_mode = chat.web_mode;
      } else showAttachError("Couldn't change web access.");
    } catch (err) {
      if (err.message !== "locked") showAttachError(err.message);
    }
  }
  updateWebButton();
}

// Stream a reply into the page, then reload the chat so ids and versions are current.
async function streamReply(resp, chatId, kind) {
  if (!resp.ok) {
    const detail = (await resp.json().catch(() => ({}))).detail;
    throw new Error(typeof detail === "string" ? detail : `request failed (${resp.status})`);
  }
  const holder = addMessage("assistant", "");
  const live = document.createElement("div");
  live.className = "web-live";
  live.hidden = true;
  holder.after(live);
  const cards = new Map();
  const steps = [];
  let reasoning = "";
  let content = "";
  let thinking = null;
  let error = null;
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const draw = () => {
    if (state.current.id !== chatId) return;
    holder.innerHTML = renderMarkdown(content);
    if (reasoning) {
      thinking = thinking || document.createElement("details");
      thinking.className = "thinking";
      thinking.innerHTML = `<summary>${content ? "Thought" : "Thinking…"}</summary><div></div>`;
      thinking.querySelector("div").textContent = reasoning;
      holder.prepend(thinking);
    }
    holder.scrollIntoView({ block: "end" });
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        const ev = /^event: (.*)$/m.exec(block)?.[1];
        const data = JSON.parse(/^data: (.*)$/m.exec(block)?.[1] ?? "null");
        if (ev === "status") {
          if (!content && !reasoning) {
            holder.innerHTML = "";
            const note = document.createElement("em");
            note.className = "status";
            note.textContent = data;
            holder.append(note);
          }
          continue;
        }
        if (ev === "web") {
          steps.push(data);
          // a step replaces its own "running" line instead of listing twice
          const same = (a, b) => a.kind === b.kind && (a.query ?? a.url) === (b.query ?? b.url);
          const prev = steps.findIndex((s, i) => i < steps.length - 1 && s.status === "running" && same(s, data));
          if (prev >= 0) steps.splice(prev, 1);
          live.replaceChildren(...steps.map(stepLine), ...cards.values());
          live.hidden = false;
          continue;
        }
        if (ev === "approval") {
          const card = approvalCard(data, kind, chatId);
          cards.set(data.id, card);
          live.append(card);
          live.hidden = false;
          card.scrollIntoView({ block: "end" });
            continue;
        }
        if (ev === "approval_done") {
          pendingApprovals.delete(data.id);
          cards.get(data.id)?.remove();
          cards.delete(data.id);
          continue;
        }
        if (ev === "context") {
          state.contextInfo[chatId] = data;
          continue;
        }
        if (ev === "reasoning") reasoning += data;
        else if (ev === "content") content += data;
        else if (ev === "error") error = data;
        draw();
      }
    }
  } finally {
    for (const id of cards.keys()) pendingApprovals.delete(id);
    for (const c of cards.values()) c.remove();
    live.remove();
  }
  if (state.current.id === chatId) {
    const chat = await fetchChat(kind, chatId);
    if (chat) renderChat(chat, error);
  }
  refreshLists();
}

// edit / regenerate / retry an earlier point of the open chat
async function runTurn(mode, index, newText = "") {
  if (state.busy || !state.chat) return;
  const { kind, id } = state.current;
  const msgs = state.chat.messages;
  const m = msgs[index];
  let path, body;
  if (mode === "edit") {
    if (!newText.trim() && !m.attachments?.length) return;
    renderMessages(msgs.slice(0, index));
    addMessage("user", newText, m.attachments || []);
    path = "messages";
    body = { content: newText, edit_of: m.id };
  } else {
    // regenerate an assistant reply (index = reply) or retry a failed one (index = user msg)
    const userIndex = mode === "regenerate" ? index - 1 : index;
    renderMessages(msgs.slice(0, userIndex + 1));
    path = "regenerate";
    body = { parent: msgs[userIndex].id };
  }
  setBusy(true);
  try {
    const resp = await api(`${base(kind)}/${id}/${path}`, { method: "POST", body, secret: kind === "secret" });
    await streamReply(resp, id, kind);
  } catch (err) {
    if (err.message !== "locked") {
      renderChat(state.chat);
      showAttachError(err.message);
    }
  } finally {
    setBusy(false);
  }
}

async function send(text, uploads) {
  if (!state.current) state.current = { kind: "normal", id: null };
  const { kind } = state.current;
  const secret = kind === "secret";
  if (!state.current.id) {
    const title =
      text.replace(/\s+/g, " ").trim().slice(0, 40) || (uploads.length ? `📎 ${uploads[0].name}`.slice(0, 40) : "New chat");
    const chat = await (await api(base(kind), { method: "POST", body: { title, web_mode: state.newWebMode || defaultWebMode(kind) }, secret })).json();
    state.current.id = chat.id;
    state.chat = { ...chat, messages: [] };
    if (kind === "normal") history.replaceState({ kind: "normal", id: chat.id }, "", `#c/${chat.id}`);
  }
  const chatId = state.current.id;
  const resp = await api(`${base(kind)}/${chatId}/messages`, {
    method: "POST",
    body: { content: text, attachments: uploads },
    secret,
  });
  if (resp.ok) {
    const shown = uploads.map((u) =>
      IMAGE_TYPES.includes(u.mime) ? { kind: "image", name: u.name, mime: u.mime, data: u.data } : { kind: "text", name: u.name },
    );
    addMessage("user", text, shown);
  }
  await streamReply(resp, chatId, kind);
}

function setBusy(on) {
  state.busy = on;
  $("send").disabled = on;
}

// ---------- vault ----------

function lockUI() {
  if (state.pwMode === "change") closePasswordDialog();
  pendingApprovals.clear(); // the server denies secret approvals itself on lock
  state.token = null;
  state.contextInfo = {};
  state.pending = [];
  $("reader-dialog").close();
  clearReader();
  renderChips();
  setUnlockedUI(false);
  $("secret-list").replaceChildren();
  if (state.current && state.current.kind === "secret") newChat("normal");
  else refreshLists();
}

async function lockVault() {
  await denyPendingApprovals();
  await fetch("/api/vault/lock", { method: "POST", cache: "no-store" });
  lockUI();
}

// mode: "create" | "unlock" | "change"
async function openPasswordDialog(mode) {
  if (mode !== "change") {
    const status = await (await api("/api/vault")).json();
    state.vaultExists = status.exists;
    mode = status.exists ? "unlock" : "create";
  }
  state.pwMode = mode;
  const text = {
    create: ["Create a vault password", "At least 12 characters. If you forget this password, your secret chats are gone for good.", "Create"],
    unlock: ["Secret chats", "Enter your password to open secret chats.", "Unlock"],
    change: ["Change vault password", "Your secret chats stay as they are and are re-encrypted with the new password. At least 12 characters.", "Change"],
  }[mode];
  $("pw-title").textContent = text[0];
  $("pw-help").textContent = text[1];
  $("pw-ok").textContent = text[2];
  $("pw0").hidden = mode !== "change";
  $("pw1").placeholder = mode === "change" ? "New password" : "Password";
  $("pw2").hidden = mode === "unlock";
  $("pw-forgot").hidden = mode !== "unlock";
  clearPasswordFields();
  $("pw-error").textContent = "";
  $("pw-dialog").showModal();
  $(mode === "change" ? "pw0" : "pw1").focus();
}

function clearPasswordFields() {
  for (const id of ["pw0", "pw1", "pw2"]) $(id).value = "";
}

function closePasswordDialog() {
  clearPasswordFields();
  $("pw-dialog").close();
}

async function submitPassword(e) {
  e.preventDefault();
  const mode = state.pwMode;
  const pw = $("pw1").value;
  const err = $("pw-error");
  if (mode !== "unlock") {
    if (pw.length < 12) return (err.textContent = "Use at least 12 characters.");
    if (pw !== $("pw2").value) return (err.textContent = "Passwords don't match.");
  }
  $("pw-ok").disabled = true;
  err.textContent = "";
  try {
    const resp =
      mode === "change"
        ? await api("/api/vault/password", { method: "POST", body: { old: $("pw0").value, new: pw }, secret: true })
        : await api(mode === "create" ? "/api/vault/create" : "/api/vault/unlock", {
            method: "POST",
            body: { password: pw },
          });
    const data = await resp.json();
    if (resp.ok) {
      closePasswordDialog();
      if (mode !== "change") {
        state.token = data.token;
        setUnlockedUI(true);
        refreshLists();
      }
    } else if (resp.status === 429) {
      err.textContent = `Too many attempts — try again in ${Math.ceil(data.retry_after)} s.`;
    } else if (resp.status === 401 || data.error === "wrong_password") {
      err.textContent = mode === "change" ? "Current password is wrong" : "Wrong password";
      $(mode === "change" ? "pw0" : "pw1").select();
    } else {
      err.textContent = data.detail || "Something went wrong.";
    }
  } catch (ex) {
    if (ex.message !== "locked") err.textContent = "Something went wrong.";
    else closePasswordDialog();
  } finally {
    $("pw-ok").disabled = false;
  }
}

function openWipeDialog() {
  closePasswordDialog();
  $("wipe-confirm").value = "";
  $("wipe-ok").disabled = true;
  $("wipe-error").textContent = "";
  $("wipe-dialog").showModal();
  $("wipe-confirm").focus();
}

async function submitWipe(e) {
  e.preventDefault();
  if ($("wipe-confirm").value !== "WIPE") return;
  $("wipe-ok").disabled = true;
  try {
    await denyPendingApprovals();
    const resp = await api("/api/vault/wipe", { method: "POST", body: { confirm: "WIPE" } });
    if (!resp.ok) throw new Error("wipe failed");
    $("wipe-dialog").close();
    state.vaultExists = false;
    lockUI();
  } catch {
    // the server locks before wiping, so a failure can leave the vault locked and partly wiped
    $("wipe-error").textContent = "Wipe may be incomplete — try again.";
    $("wipe-ok").disabled = false;
    lockUI();
  }
}

// ---------- wiring ----------

function autosize() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = `${t.scrollHeight}px`;
}

async function onSubmit(e) {
  e.preventDefault();
  const text = $("input").value.trim();
  if ((!text && !state.pending.length) || state.busy) return;
  const uploads = state.pending;
  setBusy(true);
  showAttachError("");
  try {
    await send(text, uploads);
    $("input").value = "";
    state.pending = [];
    renderChips();
    autosize();
  } catch (err) {
    // keep the typed text and files so nothing is lost; show why it failed
    if (err.message !== "locked") showAttachError(err.message);
  } finally {
    setBusy(false);
  }
}

$("composer").addEventListener("submit", onSubmit);
$("input").addEventListener("input", autosize);
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});
$("web-btn").onclick = () => ($("web-menu").hidden ? openWebMenu() : closeWebMenu());
$("web-menu").addEventListener("keydown", (e) => {
  const items = [...$("web-menu").querySelectorAll("button")];
  const i = items.indexOf(document.activeElement);
  if (e.key === "Escape") {
    e.preventDefault();
    closeWebMenu();
    $("web-btn").focus();
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
  } else if (e.key === "Tab") closeWebMenu();
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".web-wrap")) closeWebMenu();
});
$("reader-dialog").addEventListener("close", clearReader);
$("reader-close").onclick = () => $("reader-dialog").close();
$("attach").onclick = () => $("file-input").click();
$("file-input").addEventListener("change", (e) => {
  addFiles([...e.target.files]);
  e.target.value = "";
});
$("input").addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) {
    e.preventDefault();
    addFiles(files);
  }
});
// Drop files anywhere in the window. Without this, a drop outside the message box
// makes the browser open the file and leave the app.
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth++;
  $("drop-overlay").hidden = false;
});
window.addEventListener("dragover", (e) => {
  if (hasFiles(e)) e.preventDefault();
});
window.addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) $("drop-overlay").hidden = true;
});
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  $("drop-overlay").hidden = true;
  if (e.dataTransfer?.files.length) addFiles([...e.dataTransfer.files]);
});

// Browser back / forward moves between normal chats.
window.addEventListener("popstate", (e) => {
  const st = e.state;
  if (state.current?.kind === "secret") return; // stay in the secret chat; history only tracks normal chats
  if (st?.id) openChat("normal", st.id, false);
  else newChat("normal", false);
});
$("new-chat").onclick = () => newChat("normal");
$("new-secret").onclick = () => newChat("secret");
$("secret-entry").onclick = () => openPasswordDialog();
$("lock-btn").onclick = lockVault;
$("pw-change-btn").onclick = () => openPasswordDialog("change");
$("wipe-btn").onclick = openWipeDialog;
$("pw-forgot").onclick = openWipeDialog;
$("pw-form").addEventListener("submit", submitPassword);
$("pw-cancel").onclick = closePasswordDialog;
$("pw-dialog").addEventListener("close", clearPasswordFields);
$("wipe-form").addEventListener("submit", submitWipe);
$("wipe-cancel").onclick = () => $("wipe-dialog").close();
$("wipe-confirm").addEventListener("input", () => {
  $("wipe-ok").disabled = $("wipe-confirm").value !== "WIPE";
});
$("wipe-dialog").addEventListener("close", () => {
  $("wipe-confirm").value = "";
});
window.addEventListener("pagehide", () => {
  if (state.token) navigator.sendBeacon("/api/vault/lock");
});

// The backend auto-locks after idle time; mirror that so secret text doesn't stay on screen.
setInterval(async () => {
  if (!state.token) return;
  try {
    const status = await (await api("/api/vault")).json();
    if (!status.unlocked) lockUI();
  } catch {
    /* backend unreachable: leave the UI as is */
  }
}, 15000);

// A fresh page always starts locked, even if an old tab left the vault open.
lockVault().then(() => {
  const m = /^#c\/([0-9a-f]+)$/.exec(location.hash);
  if (m) {
    history.replaceState({ kind: "normal", id: m[1] }, "", location.hash);
    openChat("normal", m[1], false);
  } else {
    history.replaceState({ kind: "normal", id: null }, "", "#");
    newChat("normal", false);
  }
});
