"""Panel SPA behaviour (app/web/static/app.js) checked by running the real code under node.

The shipped file is loaded unchanged except for one line injected before the closing IIFE call
that exposes its internals (`__pcb(code)` = a direct eval inside the panel's scope). The DOM is
faked: elements are inert objects that remember what was written into them; `fetch` is a stub
that records every request. Nothing touches a server or Telegram.

Covers review2 findings 11 (compact button), 22 (idempotent create), 26 (preview of a scheduled
post), 27 (button field focus during uploads), 28 (503 health text) and the SPEC_V23 premium
emoji UI. Skipped when node is not installed (the Docker image does not run tests).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# The path, not `app.web.STATIC_DIR`: these tests need only the shipped files, not the backend.
STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
NODE = shutil.which("node")
APP_JS_PATH = STATIC_DIR / "app.js"
APP_JS = APP_JS_PATH.read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = r"""
import fs from 'node:fs';

class FakeEl {
  constructor(sel = '') {
    this.sel = sel; this._html = ''; this.writes = 0; this.textContent = ''; this.hidden = false;
    this.className = ''; this.title = ''; this.value = ''; this.disabled = false; this.checked = false;
    this.dataset = {}; this.style = {}; this.attrs = {}; this.children = []; this._q = new Map();
    this.isConnected = true;
    const cls = new Set();
    this.classList = {
      add: (...c) => c.forEach((x) => cls.add(x)), remove: (...c) => c.forEach((x) => cls.delete(x)),
      contains: (c) => cls.has(c),
      toggle: (c, on) => { const v = on === undefined ? !cls.has(c) : Boolean(on); if (v) cls.add(c); else cls.delete(c); return v; },
    };
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.writes += 1; this._q.clear(); }
  querySelector(sel) { if (!this._q.has(sel)) this._q.set(sel, new FakeEl(sel)); return this._q.get(sel); }
  querySelectorAll() { return []; }
  closest() { return null; }
  contains() { return false; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  addEventListener() {}
  appendChild(el) { this.children.push(el); return el; }
  replaceChildren() { this.children = []; this._html = ''; }
  replaceWith() {}
  remove() {}
  focus() { document.activeElement = this; }
  scrollIntoView() {}
  cloneNode() { return new FakeEl(this.sel); }
}
globalThis.FakeEl = FakeEl; // for stubs evaluated inside the panel's scope

const docEls = new Map();
globalThis.document = {
  readyState: 'loading', title: '', activeElement: null, body: new FakeEl('body'),
  addEventListener() {},
  querySelector(sel) { if (!docEls.has(sel)) docEls.set(sel, new FakeEl(sel)); return docEls.get(sel); },
  querySelectorAll() { return []; },
  getElementById(id) { return this.querySelector(`#${id}`); },
  createElement(tag) { return new FakeEl(tag); },
};
globalThis.window = globalThis;
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
globalThis.addEventListener = () => {};
globalThis.location = { hash: '', search: '', pathname: '/', reload() {} };
globalThis.history = { replaceState(_s, _t, url) { location.hash = url; } };
globalThis.requestAnimationFrame = (fn) => fn();
globalThis.scrollTo = () => {};

// fetch stub: routes = [{method, path: string | RegExp, respond(call) → {status, body, html}}];
// a route may throw to simulate a dropped connection; an unknown request is a network error.
const calls = [];
const routes = [];
globalThis.fetch = async (path, init = {}) => {
  const call = {
    method: init.method || 'GET', path: String(path), headers: { ...(init.headers || {}) },
    body: typeof init.body === 'string' ? JSON.parse(init.body) : undefined,
  };
  calls.push(call);
  const route = routes.find((r) => r.method === call.method
    && (r.path instanceof RegExp ? r.path.test(call.path) : r.path === call.path));
  if (!route) throw new TypeError(`network: ${call.method} ${call.path}`);
  const out = route.respond(call);
  const status = out.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (n) => (n.toLowerCase() === 'content-type' ? (out.html ? 'text/html' : 'application/json') : null) },
    json: async () => { if (out.html) throw new SyntaxError('not json'); return out.body; },
  };
};

const src = fs.readFileSync(process.argv[2], 'utf8');
const END = /\}\)\(\);\s*$/;
if (!END.test(src)) throw new Error('app.js must end with the IIFE call');
(0, eval)(src.replace(END, 'globalThis.__pcb = (code) => eval(code);\n})();'));
const pcb = globalThis.__pcb;

// toasts and modals are recorded instead of being drawn
const toasts = [];
pcb("toast = (message, type = 'info') => { globalThis.__toasts.push({ message: String(message), type }); }");
globalThis.__toasts = toasts;
pcb('confirmModal = async () => true');
pcb('showReport = () => {}');

const ACTIVE = (id, title) => ({ id, title, status: 'active', members_count: 10 });
function composer(channels = []) {
  const cmp = pcb('newComposer')(new FakeEl('root'), { alive: () => true }, { channels, groups: [{ folder: null, channels }] });
  pcb('state').composer = cmp;
  return cmp;
}
const json = (body, status = 200) => () => ({ status, body });
"""

EPILOGUE = r"""
try {
  const result = await scenario();
  console.log(JSON.stringify(result));
  process.exit(0);
} catch (err) {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
}
"""


def run_js(tmp_path, body: str) -> dict:
    script = tmp_path / "scenario.mjs"
    script.write_text(f"{PRELUDE}\nasync function scenario() {{\n{body}\n}}\n{EPILOGUE}", encoding="utf-8")
    proc = subprocess.run([NODE, str(script), str(APP_JS_PATH)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------ finding 26: preview of a planned post

@needs_node
def test_preview_of_scheduled_post_keeps_the_plan(tmp_path):
    """«Отправить мне превью» after ticking one more channel (mode «В очередь»): only the content is
    saved, with the channels the post already has; the new channel stays an unsaved change."""
    out = run_js(tmp_path, r"""
      const cmp = composer([ACTIVE(1, 'A'), ACTIVE(2, 'B'), ACTIVE(3, 'C')]);
      const post = { id: 7, status: 'scheduled', text_md: 'Привет', buttons: [], media: [], scheduled_at: 2000000000,
        targets: [{ channel_id: 1, status: 'pending', scheduled_at: 2000000000, queued: true },
                  { channel_id: 2, status: 'pending', scheduled_at: 2000003600, queued: true }] };
      pcb('loadPostIntoComposer')(cmp, post);
      cmp.channelIds.add(3);
      pcb('markDirty')(cmp);
      cmp.pubMode = 'queue';
      routes.push({ method: 'PUT', path: 'api/posts/7', respond: (c) => ({ body: { post: { ...post, text_md: c.body.text_md } } }) });
      routes.push({ method: 'POST', path: 'api/posts/7/preview', respond: json({ sent: 1, errors: [] }) });
      await pcb('actPreview')(cmp);
      const put = calls.find((c) => c.method === 'PUT');
      return { body: put.body, dirty: cmp.dirty, selected: [...cmp.channelIds].sort(), previews: calls.filter((c) => c.path === 'api/posts/7/preview').length };
    """)
    assert out["body"]["channel_ids"] == [1, 2]  # not the new channel 3
    assert "action" not in out["body"]  # «keep»: every channel keeps its time
    assert out["dirty"] is True  # the new selection is still unsaved: the leave guard stays on
    assert out["selected"] == [1, 2, 3]
    assert out["previews"] == 1


@needs_node
def test_preview_of_scheduled_post_unchanged_channels_saves_and_cleans(tmp_path):
    out = run_js(tmp_path, r"""
      const cmp = composer([ACTIVE(1, 'A'), ACTIVE(2, 'B')]);
      const post = { id: 8, status: 'scheduled', text_md: 'x', buttons: [], media: [], scheduled_at: 2000000000,
        targets: [{ channel_id: 1, status: 'pending', scheduled_at: 2000000000 }, { channel_id: 2, status: 'failed', scheduled_at: 2000000000 }] };
      pcb('loadPostIntoComposer')(cmp, post);
      cmp.text = 'новый текст';
      pcb('markDirty')(cmp);
      routes.push({ method: 'PUT', path: 'api/posts/8', respond: () => ({ body: { post } }) });
      routes.push({ method: 'POST', path: 'api/posts/8/preview', respond: json({ sent: 1 }) });
      await pcb('actPreview')(cmp);
      const put = calls.find((c) => c.method === 'PUT');
      return { ids: put.body.channel_ids, text: put.body.text_md, action: put.body.action ?? null, dirty: cmp.dirty };
    """)
    assert out == {"ids": [1, 2], "text": "новый текст", "action": None, "dirty": False}


# ------------------------------------------------------------------ finding 27: focus in a button field

@needs_node
def test_upload_start_and_finish_do_not_rebuild_the_buttons_editor(tmp_path):
    out = run_js(tmp_path, r"""
      const cmp = composer([]);
      cmp.buttons = [[{ text: 'Куп', url: '', style: '' }]];
      pcb('renderButtonsEditor')(cmp);
      const box = cmp.root.querySelector('#cmp-buttons');
      const start = box.writes;
      const mediaChanged = pcb('mediaChanged');
      cmp.media.push({ key: 'm1', type: 'video', state: 'uploading', localUrl: 'blob:x', url: '' });
      mediaChanged(cmp);                       // upload started
      cmp.media[0].state = 'ready';
      mediaChanged(cmp);                       // upload finished
      cmp.media[0].type = 'animation';
      mediaChanged(cmp);                       // «как GIF»
      const single = box.writes - start;
      cmp.media.push({ key: 'm2', type: 'photo', state: 'ready', url: 'api/x' });
      mediaChanged(cmp);                       // an album: the editor must lock
      const album = box.writes - start;
      const locked = box.innerHTML.includes('не поддерживает кнопки у альбомов');
      cmp.media.pop();
      mediaChanged(cmp);                       // one file again: unlocked
      return { single, album, back: box.writes - start, locked, unlocked: !box.innerHTML.includes('не поддерживает кнопки') };
    """)
    assert out["single"] == 0  # the focused input survives uploads
    assert out["album"] == 1 and out["locked"]
    assert out["back"] == 2 and out["unlocked"]


# ------------------------------------------------------------------ finding 28: status pill on 503

@needs_node
def test_health_503_shows_the_servers_problem(tmp_path):
    out = run_js(tmp_path, r"""
      const pill = document.querySelector('#status-pill');
      routes.push({ method: 'GET', path: 'api/health', respond: json({ status: 'error', version: '2.3.0', bot: null,
        problems: ['База данных недоступна — подробности в логе бота'] }, 503) });
      await pcb('refreshHealth')();
      const db = { cls: pill.className, title: pill.title };
      routes.length = 0;                      // the bot does not answer at all
      await pcb('refreshHealth')();
      const down = { cls: pill.className, title: pill.title };
      routes.push({ method: 'GET', path: 'api/health', respond: () => ({ status: 502, html: true }) });
      await pcb('refreshHealth')();
      return { db, down, proxy: { cls: pill.className, title: pill.title } };
    """)
    assert "b-danger" in out["db"]["cls"]
    assert out["db"]["title"] == "База данных недоступна — подробности в логе бота"
    for case in ("down", "proxy"):
        assert "b-danger" in out[case]["cls"]
        assert out[case]["title"] == "Нет ответа от бота. Проверьте, что он запущен."


# ------------------------------------------------------------------ finding 11: «Уплотнить очередь»

@needs_node
def test_compact_button_enabled_when_the_queue_is_beyond_the_loaded_days(tmp_path):
    out = run_js(tmp_path, r"""
      const day = (date, ts) => ({ date, weekday: 'Пн', items: [{ ts, time: '10:00', slot: true, post: null }] });
      const plan = (slots) => ({ channel: { id: 5, title: 'K', status: 'active' }, slots, slots_summary: 'пн 10:00',
        days: [day('2030-01-07', 1894006800), day('2030-01-14', 1894611600)] });
      const html = async (slots) => {
        routes.length = 0;
        routes.push({ method: 'GET', path: /^api\/plan\/channel\/5\?days=14$/, respond: json(plan(slots)) });
        const main = new FakeEl('main');
        await pcb('channelPlanView')(main, { id: 5, title: 'K', status: 'active' }, { alive: () => true });
        return /data-action="plan-compact"[^>]*>/.exec(main.innerHTML)[0];
      };
      return { withSlots: await html({ 0: ['10:00'] }), noSlots: await html({}) };
    """)
    assert "disabled" not in out["withSlots"]  # only free slots in 14 days, posts further out
    assert "disabled" in out["noSlots"]


# ------------------------------------------------------------------ finding 22: X-Idempotency-Key

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@needs_node
def test_create_retries_with_the_same_idempotency_key(tmp_path):
    out = run_js(tmp_path, r"""
      pcb('IDEMPOTENT_RETRY_DELAYS').fill(1);
      const cmp = composer([ACTIVE(1, 'A')]);
      cmp.text = 'Пост'; cmp.channelIds.add(1);
      let n = 0;
      routes.push({ method: 'POST', path: 'api/posts', respond: () => {
        n += 1;
        if (n === 1) throw new TypeError('connection reset');   // dropped connection
        if (n === 2) return { status: 504, html: true };          // nginx gave up waiting
        return { body: { post: { id: 41, status: 'draft', targets: [] } } };
      } });
      const res = await pcb('savePost')(cmp, 'draft');
      const first = calls.map((c) => c.headers['X-Idempotency-Key']);
      calls.length = 0;
      await pcb('sendIdempotent')('POST', 'api/posts', { text_md: 'другое действие' });
      return { id: res.post.id, first, second: calls[0].headers['X-Idempotency-Key'], csrf: calls[0].headers['X-Requested-With'] };
    """)
    assert out["id"] == 41
    assert len(out["first"]) == 3 and len(set(out["first"])) == 1  # automatic retries keep the key
    assert UUID_RE.match(out["first"][0])
    assert out["second"] != out["first"][0]  # a new user action gets a new key
    assert out["csrf"] == "pcb"


@needs_node
def test_identical_repeat_after_a_lost_answer_keeps_the_key(tmp_path):
    out = run_js(tmp_path, r"""
      pcb('IDEMPOTENT_RETRY_DELAYS').fill(1);
      routes.push({ method: 'POST', path: 'api/posts', respond: () => ({ status: 504, html: true }) });
      const keys = async (body) => {
        calls.length = 0;
        try { await pcb('sendIdempotent')('POST', 'api/posts', body); } catch (err) { /* lost */ }
        return [...new Set(calls.map((c) => c.headers['X-Idempotency-Key']))];
      };
      const a = await keys({ text_md: 'x', action: 'queue' });
      const b = await keys({ text_md: 'x', action: 'queue' });  // the owner presses the button again
      const c = await keys({ text_md: 'y', action: 'queue' });  // ...after editing: another action
      routes.length = 0;
      routes.push({ method: 'POST', path: 'api/posts', respond: json({ error: 'Неверные данные' }, 400) });
      calls.length = 0;
      try { await pcb('sendIdempotent')('POST', 'api/posts', { text_md: 'z' }); } catch (err) { /* refused */ }
      return { a, b, c, refusedAttempts: calls.length };
    """)
    assert len(out["a"]) == 1 and out["b"] == out["a"]
    assert out["c"] != out["a"]
    assert out["refusedAttempts"] == 1  # a definite answer is never retried


@needs_node
def test_new_post_is_published_by_id_after_a_draft_save(tmp_path):
    out = run_js(tmp_path, r"""
      const cmp = composer([ACTIVE(1, 'A')]);
      cmp.text = 'Новость'; cmp.channelIds.add(1);
      routes.push({ method: 'POST', path: 'api/posts', respond: (c) => ({ body: { post: { id: 50, status: 'draft', text_md: c.body.text_md,
        targets: [{ channel_id: 1, status: 'pending' }] } } }) });
      routes.push({ method: 'PUT', path: 'api/posts/50', respond: () => ({ body: { post: { id: 50, status: 'sent', text_md: 'Новость',
        targets: [{ channel_id: 1, status: 'sent' }] }, report: { ok_count: 1, fail_count: 0, results: [] } } }) });
      await pcb('actPublish')(cmp, null);
      return calls.map((c) => [c.method, c.path, c.body.action, Boolean(c.headers['X-Idempotency-Key'])]);
    """)
    assert out == [["POST", "api/posts", "draft", True], ["PUT", "api/posts/50", "publish", True]]


@needs_node
def test_every_publishing_request_sends_a_key(tmp_path):
    """«Допубликовать» in the editor, «Опубликовать» in the posts list and «Опубликовать сейчас» in
    the content plan go through the same idempotent helper (the server accepts the key there)."""
    out = run_js(tmp_path, r"""
      pcb('IDEMPOTENT_RETRY_DELAYS').fill(1);
      let n = 0;
      routes.push({ method: 'POST', path: /^api\/posts\/\d+\/publish$/, respond: () => {
        n += 1;
        return n === 1 ? { status: 502, html: true } : { body: { post: { id: 70, status: 'sent', targets: [] }, report: null } };
      } });
      await pcb('publishPlannedNow')({ id: 70, excerpt: 'x' }, { id: 1, title: 'A' });
      return calls.map((c) => [c.path, c.body, c.headers['X-Idempotency-Key']]);
    """)
    assert [c[0] for c in out] == ["api/posts/70/publish", "api/posts/70/publish"]
    assert out[0][1] == {"channel_ids": [1]}
    assert out[0][2] and out[0][2] == out[1][2]
    for needle in ("sendIdempotent('POST', `api/posts/${cmp.postId}/publish`)",
                   "sendIdempotent('POST', `api/posts/${num(p.id)}/publish`)"):
        assert needle in APP_JS
    # no publishing request bypasses the helper
    assert not re.search(r"api\.(post|put)\(\s*[`'](api/posts(/\$\{[^}]+\})?(/publish)?)[`']", APP_JS)


@needs_node
def test_lost_answer_on_publish_resyncs_the_post(tmp_path):
    out = run_js(tmp_path, r"""
      pcb('IDEMPOTENT_RETRY_DELAYS').fill(1);
      const cmp = composer([ACTIVE(1, 'A')]);
      const draft = { id: 60, status: 'draft', text_md: 'x', buttons: [], media: [], targets: [{ channel_id: 1, status: 'pending' }] };
      pcb('loadPostIntoComposer')(cmp, draft);
      routes.push({ method: 'PUT', path: 'api/posts/60', respond: () => ({ status: 504, html: true }) });
      routes.push({ method: 'GET', path: 'api/posts/60', respond: json({ post: { ...draft, status: 'sent', targets: [{ channel_id: 1, status: 'sent' }] } }) });
      let message = '';
      try { await pcb('savePost')(cmp, 'publish'); } catch (err) { message = err.message; }
      const puts = calls.filter((c) => c.method === 'PUT').map((c) => c.headers['X-Idempotency-Key']);
      return { message, status: cmp.post.status, mode: cmp.mode, puts };
    """)
    assert len(out["puts"]) == 3 and len(set(out["puts"])) == 1  # retried with the same key, then resynced
    assert out["status"] == "sent" and out["mode"] == "sent"
    assert out["message"].startswith("Ответ сервера не дошёл")


# ------------------------------------------------------------------ SPEC_V23: premium emoji

RENDER = r"""
  pcb('sanitizeTelegramHtml = () => new FakeEl("fragment")'); // no real DOM to parse the preview HTML into
  const render = (extra) => {
    routes.length = 0;
    routes.push({ method: 'POST', path: 'api/render', respond: json({ html: '😀 текст', length: 7, limit: 4096, error: null, ...extra }) });
  };
  const warn = (cmp) => cmp.root.querySelector('#cmp-premium-warn').innerHTML;
"""


@needs_node
def test_composer_warns_about_premium_emoji_without_fragment_username(tmp_path):
    out = run_js(tmp_path, RENDER + r"""
      const cmp = composer([]);
      cmp.text = '![😀](tg://emoji?id=5368324170671202286) текст';
      render({ has_custom_emoji: true, premium_emoji: false });
      await pcb('doRender')(cmp);
      const off = warn(cmp);
      render({ has_custom_emoji: true, premium_emoji: true });
      await pcb('doRender')(cmp);
      const on = warn(cmp);
      render({ has_custom_emoji: false, premium_emoji: false });
      await pcb('doRender')(cmp);
      const none = warn(cmp);
      // a server without the V2.3 fields: no warning unless the setting is known to be off
      pcb('state').premiumEmoji = null;
      render({});
      await pcb('doRender')(cmp);
      const unknown = warn(cmp);
      return { off, on, none, unknown, state: pcb('state').premiumEmoji };
    """)
    assert "В посте есть премиум-эмодзи — в канале они станут обычными: у бота нет юзернейма с Fragment" in out["off"]
    assert 'href="#/settings?card=premium-emoji"' in out["off"] and "Настройки → Премиум-эмодзи" in out["off"]
    assert out["on"] == "" and out["none"] == "" and out["unknown"] == ""


@needs_node
def test_premium_emoji_checkbox_saves_at_once(tmp_path):
    out = run_js(tmp_path, r"""
      const input = new FakeEl('input'); input.checked = true;
      routes.push({ method: 'PUT', path: 'api/settings/premium-emoji', respond: (c) => ({ body: { premium_emoji: c.body.enabled } }) });
      await pcb('savePremiumEmoji')(input);
      const ok = { body: calls[0].body, csrf: calls[0].headers['X-Requested-With'], checked: input.checked,
        state: pcb('state').premiumEmoji, toast: toasts.at(-1), disabled: input.disabled };
      routes.length = 0;
      routes.push({ method: 'PUT', path: 'api/settings/premium-emoji', respond: json({ error: 'Ошибка сервера' }, 500) });
      input.checked = false;
      await pcb('savePremiumEmoji')(input);
      return { ok, failedChecked: input.checked, failedToast: toasts.at(-1) };
    """)
    assert out["ok"]["body"] == {"enabled": True} and out["ok"]["csrf"] == "pcb"
    assert out["ok"]["checked"] is True and out["ok"]["state"] is True and out["ok"]["disabled"] is False
    assert out["ok"]["toast"]["type"] == "success"
    assert out["failedChecked"] is True  # the failed change is put back
    assert out["failedToast"]["type"] == "error"


def test_premium_emoji_settings_card_wording():
    card = APP_JS[APP_JS.index("function premiumCardHtml"):APP_JS.index("async function savePremiumEmoji")]
    assert "Премиум-эмодзи" in card
    assert ("Telegram показывает премиум-эмодзи в каналах, только если у бота есть дополнительный юзернейм, "
            "купленный на <a href=\"https://fragment.com\" target=\"_blank\" rel=\"noopener noreferrer\">Fragment</a> "
            "(за TON) и привязанный к боту. Без него в опубликованных постах они заменяются обычными эмодзи.") in card
    assert "У бота есть юзернейм с Fragment — премиум-эмодзи доступны" in card
    assert ("Выключено — редактор предупреждает, что премиум-эмодзи в посте станут обычными, и бот отправляет их "
            "обычными. Включено — публикуются как есть. То же самое — в боте: ⚙️ Настройки → ✨ Премиум-эмодзи.") in card
    assert "api.put('api/settings/premium-emoji', { enabled })" in APP_JS


def test_cheatsheet_mentions_fragment_for_custom_emoji():
    sheet = APP_JS[APP_JS.index("function cheatsheetHtml"):APP_JS.index("function toolbarHtml")]
    row = next(line for line in sheet.splitlines() if "tg://emoji?id=" in line)
    assert "Fragment" in row


# ------------------------------------------------------------------ QA: finding 15 (panel side)

@needs_node
def test_delete_from_channels_dialog_names_the_waiting_channels(tmp_path):
    """Review2 #15 (panel): deleting a post queued to more channels also cancels those — the
    confirmation says so, like the bot. A 'deleted' post still shown somewhere offers «Удалить из
    каналов», not «Удалить» (the record)."""
    out = run_js(tmp_path, r"""
      const dialog = pcb('deleteChannelsDialog');
      const queued = { id: 3, status: 'scheduled', targets: [
        { channel_id: 1, title: 'A', status: 'sent' },
        { channel_id: 2, title: 'B <b>', status: 'pending', scheduled_at: 2000000000, queued: true },
        { channel_id: 3, title: 'C', status: 'pending', scheduled_at: null, queued: false }] };
      const sentOnly = { id: 4, status: 'sent', targets: [{ channel_id: 1, title: 'A', status: 'sent' }] };
      const actions = (p) => pcb('postActionsHtml')(p).join(' ');
      return {
        queued: dialog(queued), sent: dialog(sentOnly),
        deletedShown: actions({ id: 5, status: 'deleted', targets: [{ channel_id: 1, status: 'sent' }] }),
        deletedGone: actions({ id: 6, status: 'deleted', targets: [{ channel_id: 1, status: 'deleted' }] }),
      };
    """)
    assert out["queued"]["html"] == "<p>Публикация в «B &lt;b&gt;» по расписанию тоже будет отменена.</p>"
    assert out["queued"]["message"].startswith("Сообщение будет удалено в 1 ")
    assert out["sent"]["html"] == ""
    assert 'data-action="delete-channels"' in out["deletedShown"] and 'data-action="delete"' not in out["deletedShown"]
    assert 'data-action="delete"' in out["deletedGone"] and "delete-channels" not in out["deletedGone"]
