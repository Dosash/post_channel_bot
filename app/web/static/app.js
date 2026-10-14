/**
 * Post Channel Bot — web panel (vanilla JS SPA, no build step, no external resources).
 *
 * Every URL is relative to <base href> (the server puts the panel prefix there): requests go to
 * "api/…" without a leading slash, so the panel works at the site root and under a prefix.
 * URLs returned by the API already include the prefix and are used as-is.
 *
 * Sections of this file:
 *   1. Utilities: escaping, formatting, storage
 *   2. Icons: inline SVG (lucide)
 *   3. API client (every request sends `X-Requested-With: pcb`; 401 → login screen)
 *   4. UI primitives: shell (topbar, drawer, theme, status), toasts, modals, busy buttons,
 *      empty/loading states
 *   5. Telegram HTML sanitizer (used for the live post preview)
 *   6. Auth: login screen
 *   7. Router (hash based) + unsaved-changes guard
 *   8. Media helpers; pages: overview, compose (media gallery, folders, publish modes), posts,
 *      content plan, schedule editor, channels & folders, stats, settings
 *   9. Boot
 *
 * Security: every piece of user/API-provided text goes through esc() before it is put into
 * HTML; post HTML coming from api/render is rebuilt node by node from an allow-list.
 */
(() => {
  'use strict';

  // ======================================================================= 1. Utilities

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  /** Escapes any value for HTML text content and quoted attribute values. */
  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => ESC_MAP[c]);
  }

  /** Integer for ids that go into attributes and URLs. */
  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? Math.trunc(n) : 0;
  }

  /** Returns the URL if it uses a scheme that is safe for links, else ''. */
  function safeHref(url) {
    const u = String(url ?? '').trim();
    return /^(https?:\/\/|tg:\/\/|mailto:)/i.test(u) ? u : '';
  }

  /**
   * Images are only loaded from this server or from local blob: URLs: a path from the API (it
   * already carries the panel prefix, "/…"), a path relative to <base> ("api/…") or blob:.
   */
  function safeSrc(url) {
    const u = String(url ?? '').trim();
    return /^\/(?!\/)/.test(u) || /^api\//.test(u) || /^blob:/i.test(u) ? u : '';
  }

  const NF = new Intl.NumberFormat('ru-RU');
  const NF_COMPACT = new Intl.NumberFormat('ru-RU', { notation: 'compact', maximumFractionDigits: 1 });

  function fmtNum(n) {
    if (n === null || n === undefined || n === '' || Number.isNaN(Number(n))) return '—';
    return NF.format(Number(n));
  }

  function fmtSigned(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
    const v = Number(n);
    return `${v > 0 ? '+' : v < 0 ? '−' : ''}${NF.format(Math.abs(v))}`;
  }

  function fmtCompact(n) {
    return Math.abs(n) >= 10000 ? NF_COMPACT.format(n) : NF.format(n);
  }

  function fmtSize(bytes) {
    return `${(bytes / 1024 / 1024).toFixed(1).replace('.', ',')} МБ`;
  }

  const DTF_DATE = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });
  const DTF_DAY_SHORT = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', timeZone: 'UTC' });
  const DTF_DAY_LONG = new Intl.DateTimeFormat('ru-RU', {
    weekday: 'short', day: 'numeric', month: 'long', timeZone: 'UTC',
  });

  const tsDate = (ts) => new Date(Number(ts) * 1000);
  const nowTs = () => Math.floor(Date.now() / 1000);

  // Times are shown and entered in the bot's timezone (state.serverTz), like the bot does: the
  // publishing slots are defined there. See zonedParts() below.

  /** "14.10.2026, 14:30". */
  function fmtDateTime(ts) {
    if (!ts) return '—';
    const p = zonedParts(ts);
    return `${pad2(p.d)}.${pad2(p.m)}.${p.y}, ${p.time}`;
  }

  function fmtDate(ts) {
    if (!ts) return '—';
    const p = zonedParts(ts);
    return DTF_DATE.format(new Date(Date.UTC(p.y, p.m - 1, p.d)));
  }

  /** "сегодня в 14:30" / "вчера в 09:00" / "завтра в 10:00" / "12.10.2026, 14:30". */
  function fmtWhen(ts) {
    if (!ts) return '—';
    const p = zonedParts(ts);
    const key = zonedDayKey(p);
    if (key === zonedDayKey(zonedParts(nowTs()))) return `сегодня в ${p.time}`;
    if (key === zonedDayKey(zonedParts(nowTs() - 86400))) return `вчера в ${p.time}`;
    if (key === zonedDayKey(zonedParts(nowTs() + 86400))) return `завтра в ${p.time}`;
    return fmtDateTime(ts);
  }

  /** "YYYY-MM-DD" (a calendar day from the API) → Date at UTC midnight, formatted in UTC. */
  function isoDay(s) {
    const [y, m, d] = String(s || '').split('-').map(Number);
    return new Date(Date.UTC(y || 1970, (m || 1) - 1, d || 1));
  }
  const dayShort = (s) => DTF_DAY_SHORT.format(isoDay(s));
  const dayLong = (s) => DTF_DAY_LONG.format(isoDay(s));

  /** Unix seconds → value for <input type="datetime-local"> in the bot's timezone. */
  function toZonedInput(ts) {
    const p = zonedParts(ts);
    return `${p.y}-${pad2(p.m)}-${pad2(p.d)}T${p.time}`;
  }

  /** datetime-local value (wall time in the bot's timezone) → unix seconds, or null. */
  function fromZonedInput(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(value || ''));
    if (!m) return null;
    const wall = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5])) / 1000;
    let ts = wall;
    for (let i = 0; i < 2; i += 1) { // second pass settles DST transitions
      const p = zonedParts(ts);
      const [hh, mm] = p.time.split(':').map(Number);
      ts += wall - Date.UTC(p.y, p.m - 1, p.d, hh, mm) / 1000;
    }
    return ts;
  }

  function plural(n, forms) {
    const a = Math.abs(Number(n) || 0) % 100;
    const b = a % 10;
    if (a > 10 && a < 20) return forms[2];
    if (b > 1 && b < 5) return forms[1];
    if (b === 1) return forms[0];
    return forms[2];
  }

  const MEMBERS_FORMS = ['подписчик', 'подписчика', 'подписчиков'];
  const CHANNELS_IN_FORMS = ['канале', 'каналах', 'каналах'];
  const POSTS_FORMS = ['пост', 'поста', 'постов'];

  function debounce(fn, ms) {
    let timer = 0;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  function storageGet(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null; // storage may be blocked (private mode); the panel works without it
    }
  }

  function storageSet(key, value) {
    try {
      if (value === null || value === undefined) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch {
      /* storage unavailable — preferences simply are not remembered */
    }
  }

  const BROWSER_TZ = (() => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    } catch {
      return '';
    }
  })();

  const pad2 = (n) => String(n).padStart(2, '0');

  // ---- dates in the bot's timezone (publishing slots are defined there)

  const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  const WEEKDAYS_GEN = ['понедельника', 'вторника', 'среды', 'четверга', 'пятницы', 'субботы', 'воскресенья'];
  const EN_WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const zoneFormatters = new Map();

  function zoneFormatter(tz) {
    const key = tz || '';
    if (!zoneFormatters.has(key)) {
      const opts = {
        year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', weekday: 'short', hourCycle: 'h23',
      };
      let f;
      try {
        f = new Intl.DateTimeFormat('en-GB', tz ? { ...opts, timeZone: tz } : opts);
      } catch {
        f = new Intl.DateTimeFormat('en-GB', opts); // unknown zone name → browser time
      }
      zoneFormatters.set(key, f);
    }
    return zoneFormatters.get(key);
  }

  /** Calendar parts of a timestamp in the bot's timezone (browser zone until it is known). */
  function zonedParts(ts) {
    const parts = {};
    zoneFormatter(state.serverTz).formatToParts(tsDate(ts)).forEach((p) => {
      parts[p.type] = p.value;
    });
    return {
      y: Number(parts.year),
      m: Number(parts.month),
      d: Number(parts.day),
      time: `${parts.hour === '24' ? '00' : parts.hour}:${parts.minute}`,
      wd: Math.max(0, EN_WEEKDAYS.indexOf(parts.weekday)),
    };
  }

  const zonedDayKey = (p) => p.y * 10000 + p.m * 100 + p.d;

  /** "Ср 15.10 09:00" in the bot's timezone — the way the bot and the plan show slots. */
  function fmtSlot(ts) {
    if (!ts) return '—';
    const p = zonedParts(ts);
    return `${WEEKDAYS[p.wd]} ${pad2(p.d)}.${pad2(p.m)} ${p.time}`;
  }

  /** "сегодня в 09:00" / "завтра в 09:00" / "Ср 15.10 в 09:00" in the bot's timezone. */
  function fmtSlotWhen(ts) {
    if (!ts) return '—';
    const p = zonedParts(ts);
    const today = zonedParts(nowTs());
    const tomorrow = zonedParts(nowTs() + 86400);
    if (zonedDayKey(p) === zonedDayKey(today)) return `сегодня в ${p.time}`;
    if (zonedDayKey(p) === zonedDayKey(tomorrow)) return `завтра в ${p.time}`;
    return `${WEEKDAYS[p.wd]} ${pad2(p.d)}.${pad2(p.m)} в ${p.time}`;
  }

  /** True when the browser and the bot use different timezones (times may look shifted). */
  const tzDiffers = () => Boolean(state.serverTz && BROWSER_TZ && state.serverTz !== BROWSER_TZ);

  // ---- weekly publishing slots: {0: ['09:00', …], …, 6: […]}, 0 = Monday (same rules as the bot)

  const TIME_RE = /^(\d{1,2})[:.](\d{2})$/;

  function cleanTime(value) {
    const m = TIME_RE.exec(String(value ?? '').trim());
    if (!m) return null;
    const h = Number(m[1]);
    const min = Number(m[2]);
    return h > 23 || min > 59 ? null : `${pad2(h)}:${pad2(min)}`;
  }

  /** Any slots object from the API ({"0": [...]} or {0: [...]}) → 7 sorted unique lists. */
  function normSlots(obj) {
    const out = [[], [], [], [], [], [], []];
    if (obj && typeof obj === 'object') {
      Object.keys(obj).forEach((key) => {
        const day = Number(key);
        if (!Number.isInteger(day) || day < 0 || day > 6) return;
        const list = Array.isArray(obj[key]) ? obj[key] : [];
        out[day] = Array.from(new Set(list.map(cleanTime).filter(Boolean))).sort();
      });
    }
    return out;
  }

  const slotsCount = (days) => days.reduce((n, list) => n + list.length, 0);

  /** 7 lists → API shape {"0": [...], …} (empty days included). */
  const slotsToApi = (days) => Object.fromEntries(days.map((list, i) => [String(i), list.slice()]));

  function daysText(days) {
    if (days.length === 7) return 'ежедневно';
    const runs = [];
    days.forEach((d) => {
      const last = runs[runs.length - 1];
      if (last && last[last.length - 1] === d - 1) last.push(d);
      else runs.push([d]);
    });
    return runs.map((run) => {
      const names = run.map((d) => WEEKDAYS[d].toLowerCase());
      return run.length >= 3 ? `${names[0]}-${names[names.length - 1]}` : names.join(', ');
    }).join(', ');
  }

  /** Canonical text of a schedule, like the bot shows it: "пн-пт 09:00, 18:00\nсб, вс 12:00". */
  function slotsToText(days) {
    const groups = new Map();
    days.forEach((list, d) => {
      if (!list.length) return;
      const key = list.join(', ');
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(d);
    });
    return Array.from(groups, ([times, ds]) => `${daysText(ds)} ${times}`).join('\n');
  }

  function slotsSummary(days) {
    const text = slotsToText(days);
    return text ? text.split('\n').join(' · ') : 'не настроено';
  }

  const DAY_NAMES = [
    ['пн', 'пон', 'понед', 'понедельник', 'понедельника', 'понедельникам', 'mon', 'monday', 'mondays', 'mo'],
    ['вт', 'вто', 'втор', 'вторник', 'вторника', 'вторникам', 'tue', 'tues', 'tuesday', 'tuesdays', 'tu'],
    ['ср', 'сре', 'сред', 'среда', 'среду', 'среды', 'средам', 'wed', 'wednesday', 'wednesdays', 'we'],
    ['чт', 'чет', 'четв', 'четверг', 'четверга', 'четвергам', 'thu', 'thur', 'thurs', 'thursday', 'thursdays', 'th'],
    ['пт', 'пят', 'пятн', 'пятница', 'пятницу', 'пятницы', 'пятницам', 'fri', 'friday', 'fridays', 'fr'],
    ['сб', 'суб', 'субб', 'суббота', 'субботу', 'субботы', 'субботам', 'sat', 'saturday', 'saturdays', 'sa'],
    ['вс', 'вос', 'воск', 'воскр', 'воскресенье', 'воскресенья', 'воскресеньям', 'sun', 'sunday', 'sundays', 'su'],
  ];
  const DAY_ALIAS = new Map(DAY_NAMES.flatMap((names, d) => names.map((n) => [n, d])));
  const ALL_DAYS = [0, 1, 2, 3, 4, 5, 6];
  const DAY_GROUPS = new Map([
    ...['ежедневно', 'ежедн', 'daily', 'everyday', 'всегда'].map((w) => [w, ALL_DAYS]),
    ...['будни', 'будням', 'будние', 'weekdays', 'workdays'].map((w) => [w, [0, 1, 2, 3, 4]]),
    ...['выходные', 'выходным', 'weekend', 'weekends'].map((w) => [w, [5, 6]]),
  ]);
  const DAY_FILLERS = new Set(['в', 'во', 'и', 'с', 'со', 'at', 'on', 'and', 'from']);
  const DAY_RANGE_WORDS = new Set(['по', 'до', 'to', 'till', 'until', 'through']);

  const dayWord = (tok) => tok.replace(/[.:]+$/, '');

  function dayRange(a, b) {
    const out = [];
    for (let i = 0; i <= (b - a + 7) % 7; i += 1) out.push((a + i) % 7);
    return out;
  }

  /**
   * Client-side reading of the schedule text format (the server parser stays authoritative; this
   * one only lets the editor switch from text to the visual mode). Throws Error with a line number.
   */
  function parseSlotsText(text) {
    const result = [[], [], [], [], [], [], []];
    String(text || '').split('\n').forEach((rawLine, idx) => {
      const lineNo = idx + 1;
      let line = rawLine.toLowerCase().replace(/ё/g, 'е');
      line = line
        .replace(/(каждый|кажд\.?)\s+день/g, ' ежедневно ')
        .replace(/все\s+дни/g, ' ежедневно ')
        .replace(/every\s*day/g, ' ежедневно ')
        .replace(/(по\s+)?будн(ие|им)\s+дн(и|ям)/g, ' будни ')
        .replace(/(по\s+)?выходн(ые|ым)\s+дн(и|ям)/g, ' выходные ')
        .replace(/\s*[-–—−]\s*/g, '-');
      const tokens = line.split(/[\s,;/+&]+/).filter(Boolean);
      if (!tokens.length) return;
      const rules = [];
      let days = null;
      let times = [];
      const flush = () => {
        if (days && !times.length) throw new Error(`Строка ${lineNo}: для дней не указано время`);
        if (times.length) rules.push([days || ALL_DAYS, times]);
        days = null;
        times = [];
      };
      for (let i = 0; i < tokens.length; i += 1) {
        const tok = tokens[i];
        const time = cleanTime(tok.replace(/[.:]+$/, ''));
        if (time) {
          times.push(time);
          continue;
        }
        if (TIME_RE.test(tok.replace(/[.:]+$/, ''))) throw new Error(`Строка ${lineNo}: не понял время «${tok}»`);
        let found = null;
        const word = dayWord(tok);
        if (DAY_ALIAS.has(word)) found = [DAY_ALIAS.get(word)];
        else if (DAY_GROUPS.has(word)) found = DAY_GROUPS.get(word);
        else if (tok.includes('-')) {
          const [a, b, extra] = tok.split('-');
          if (extra === undefined && DAY_ALIAS.has(dayWord(a)) && DAY_ALIAS.has(dayWord(b))) {
            found = dayRange(DAY_ALIAS.get(dayWord(a)), DAY_ALIAS.get(dayWord(b)));
          } else if (cleanTime(a) && cleanTime(b)) {
            throw new Error(`Строка ${lineNo}: интервалы времени («${tok}») не поддерживаются — перечислите время через запятую`);
          }
        } else if (DAY_RANGE_WORDS.has(word) && days && days.length && !times.length) {
          const next = tokens[i + 1] && dayWord(tokens[i + 1]);
          if (next && DAY_ALIAS.has(next)) {
            const start = days[days.length - 1];
            days = Array.from(new Set([...days, ...dayRange(start, DAY_ALIAS.get(next))]));
            i += 1;
            continue;
          }
        }
        if (found) {
          if (times.length) flush();
          days = Array.from(new Set([...(days || []), ...found]));
          continue;
        }
        if (DAY_FILLERS.has(word) || DAY_RANGE_WORDS.has(word)) continue;
        throw new Error(`Строка ${lineNo}: не понял «${tok}» — нужны дни недели (пн-пт, сб, вс…) и время (09:00)`);
      }
      flush();
      rules.forEach(([ds, ts]) => ds.forEach((d) => {
        result[d] = Array.from(new Set([...result[d], ...ts])).sort();
      }));
    });
    return result;
  }

  // ======================================================================= 2. Icons

  const ICON_PATHS = {
    logo: '<path d="M21.2 4.6 3.3 11.5c-.9.4-.9 1.6 0 1.9l4.4 1.4 1.7 5.3c.2.7 1.1.9 1.6.4l2.5-2.3 4.6 3.3c.6.4 1.4.1 1.6-.6l2.9-14.7c.2-1-.7-1.8-1.6-1.4z"/><path d="m8 14.6 9-6.6"/>',
    home: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    edit: '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.38 2.62a2.12 2.12 0 1 1 3 3L12 15l-4 1 1-4Z"/>',
    posts: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8M16 13H8M16 17H8"/>',
    channels: '<path d="m3 11 18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
    chart: '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9M13 17V5M8 17v-3"/>',
    settings: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    trash: '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="m6 7 1 13h10l1-13"/><path d="M9 7V4h6v3"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.8-4"/><path d="M4 3v4h4"/><path d="M4 13a8 8 0 0 0 14.8 4"/><path d="M20 21v-4h-4"/>',
    eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    eyeOff: '<path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6A17 17 0 0 0 2 12s3.6 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.8-9.8"/><path d="m16 7 3 3"/><path d="m18.5 4.5 2 2"/>',
    send: '<path d="M21 3 10 14"/><path d="m21 3-7 18-4-7-7-4 18-7z"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
    link: '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1"/><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
    up: '<path d="m6 15 6-6 6 6"/>',
    down: '<path d="m6 9 6 6 6-6"/>',
    logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="m10 17-5-5 5-5"/><path d="M5 12h11"/>',
    external: '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    arrowUpRight: '<path d="M7 17 17 7"/><path d="M9 7h8v8"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18 14.2a6.5 6.5 0 0 1 3.5 5.8"/>',
    heart: '<path d="M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7.3a4.3 4.3 0 0 1 7.5 2.5C19.5 15.4 12 20 12 20z"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    alert: '<path d="M10.3 4.2 2.5 18a2 2 0 0 0 1.7 3h15.6a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z"/><path d="M12 10v4M12 17h.01"/>',
    quote: '<path d="M5 5v14"/><path d="M9 8h10M9 12h10M9 16h6"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    code: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4"/>',
    spoiler: '<path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6A17 17 0 0 0 2 12s3.6 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    upload: '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
    text: '<path d="M5 6h14M5 10h14M5 14h9M5 18h6"/>',
    bot: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9.5 17h5"/>',
    palette: '<path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.8-.9 1.8-1.9 0-.5-.2-.9-.5-1.3-.3-.3-.5-.8-.5-1.3 0-1 .8-1.8 1.8-1.8H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3z"/><path d="M7.5 11.5h.01M10 7.5h.01M14.5 7.5h.01"/>',
    star: '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3z"/>',
    calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.2h7.5A2.5 2.5 0 0 1 21 9.7v7.8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/>',
    folderPlus: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.2h7.5A2.5 2.5 0 0 1 21 9.7v7.8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/><path d="M12 11v5M9.5 13.5h5"/>',
    play: '<path d="M8 5.6v12.8a.6.6 0 0 0 .92.5l10.1-6.4a.6.6 0 0 0 0-1L8.92 5.1A.6.6 0 0 0 8 5.6z"/>',
    video: '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="m16 10.5 5-3v9l-5-3"/>',
    album: '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/>',
    left: '<path d="m15 6-6 6 6 6"/>',
    right: '<path d="m9 6 6 6-6 6"/>',
    grip: '<path d="M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01"/>',
    bolt: '<path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12z"/>',
    queue: '<path d="M4 6h16M4 12h11M4 18h7"/><path d="m16 15 4 3-4 3"/>',
    compact: '<path d="M4 4h16M4 20h16"/><path d="M12 7v10"/><path d="m9 10 3-3 3 3M9 14l3 3 3-3"/>',
    move: '<path d="M12 3v18M3 12h18"/><path d="m9 6 3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/>',
    dashboard: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    newspaper: '<path d="M15 18h-5M18 14h-8"/><path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-4 0v-9a2 2 0 0 1 2-2h2"/><rect width="8" height="4" x="10" y="6" rx="1"/>',
    back: '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2m-7.07-14.07 1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    monitor: '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8M12 17v4"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    sparkles: '<path d="M9.94 15.5a2 2 0 0 0-1.44-1.44l-6.13-1.58a.5.5 0 0 1 0-.96L8.5 9.94A2 2 0 0 0 9.94 8.5l1.58-6.14a.5.5 0 0 1 .96 0L14.06 8.5a2 2 0 0 0 1.44 1.44l6.14 1.58a.5.5 0 0 1 0 .96L15.5 14.06a2 2 0 0 0-1.44 1.44l-1.58 6.14a.5.5 0 0 1-.96 0z"/><path d="M20 3v4M22 5h-4M4 17v2M5 18H3"/>',
  };

  /** Inline SVG icon; decorative (aria-hidden), the surrounding control carries the label. */
  function icon(name, cls = '') {
    return `<svg class="i${cls ? ` ${cls}` : ''}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICON_PATHS[name] || ''}</svg>`;
  }

  const AVATAR_COLORS = 7;

  function avatarHtml(id, title, size = '') {
    const letter = Array.from(String(title || '').trim())[0] || '#';
    const color = Math.abs(num(id)) % AVATAR_COLORS;
    return `<span class="avatar av-${color}${size ? ` ${size}` : ''}" aria-hidden="true">${esc(letter.toUpperCase())}</span>`;
  }

  function channelLink(ch) {
    const link = safeHref(ch && ch.link);
    if (link) return link;
    return ch && ch.username ? `https://t.me/${encodeURIComponent(ch.username)}` : '';
  }

  // ======================================================================= 3. API client

  class ApiError extends Error {
    constructor(message, status = 0, data = null) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.data = data;
      this.silent = false;
    }
  }

  /** Client-side validation problem; `focus` is a selector of the field to focus. */
  class ValidationError extends Error {
    constructor(message, focus = '') {
      super(message);
      this.name = 'ValidationError';
      this.focus = focus;
    }
  }

  function errorMessage(data, status) {
    const raw = data && typeof data.error === 'string' ? data.error : '';
    if (raw === 'auth_required') return 'Требуется вход';
    if (raw) return raw;
    if (status === 403) return 'Доступ запрещён';
    if (status === 404) return 'Не найдено';
    if (status === 413) return 'Файл слишком большой';
    if (status === 429) return 'Слишком много попыток, подождите немного';
    if (status >= 500) return 'Ошибка сервера. Подробности — в логах бота.';
    return `Ошибка запроса (${status})`;
  }

  async function request(method, path, body, { allow401 = false, headers: extra = null } = {}) {
    const headers = { ...(extra || {}), 'X-Requested-With': 'pcb', Accept: 'application/json' };
    const init = { method, headers, credentials: 'same-origin', cache: 'no-store' };
    if (body instanceof FormData) {
      init.body = body;
    } else if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch {
      throw new ApiError('Нет связи с сервером. Проверьте, что бот запущен.', 0);
    }
    let data = null;
    if ((res.headers.get('content-type') || '').includes('application/json')) {
      try {
        data = await res.json();
      } catch {
        data = null;
      }
    }
    if (res.status === 401 && !allow401) {
      showLogin();
      const err = new ApiError('Требуется вход', 401, data);
      err.silent = true;
      throw err;
    }
    if (!res.ok) throw new ApiError(errorMessage(data, res.status), res.status, data);
    return data ?? {};
  }

  /**
   * Multipart upload of one media file with progress (fetch cannot report upload progress).
   * Returns {promise, abort}; the promise resolves with the JSON body or rejects with ApiError.
   */
  function uploadWithProgress(file, onProgress) {
    const xhr = new XMLHttpRequest();
    const promise = new Promise((resolve, reject) => {
      xhr.open('POST', 'api/upload');
      xhr.setRequestHeader('X-Requested-With', 'pcb');
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
      });
      xhr.addEventListener('load', () => {
        let data = null;
        if ((xhr.getResponseHeader('content-type') || '').includes('application/json')) {
          try {
            data = JSON.parse(xhr.responseText);
          } catch {
            data = null;
          }
        }
        if (xhr.status === 401) {
          showLogin();
          const err = new ApiError('Требуется вход', 401, data);
          err.silent = true;
          reject(err);
        } else if (xhr.status < 200 || xhr.status >= 300) {
          reject(new ApiError(errorMessage(data, xhr.status), xhr.status, data));
        } else {
          resolve(data ?? {});
        }
      });
      xhr.addEventListener('error', () => reject(new ApiError('Нет связи с сервером. Проверьте, что бот запущен.', 0)));
      xhr.addEventListener('abort', () => {
        const err = new ApiError('Загрузка отменена', 0);
        err.silent = true;
        reject(err);
      });
      const fd = new FormData();
      fd.append('file', file, file.name || 'file');
      xhr.send(fd);
    });
    return { promise, abort: () => xhr.abort() };
  }

  const api = {
    get: (path, opts) => request('GET', path, undefined, opts),
    post: (path, body = {}, opts) => request('POST', path, body, opts),
    put: (path, body = {}, opts) => request('PUT', path, body, opts),
    del: (path, body, opts) => request('DELETE', path, body, opts),
  };

  /** Random UUID v4. crypto.randomUUID() needs a secure context, and the panel may run on plain http. */
  function uuid4() {
    const bytes = new Uint8Array(16);
    if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === 'function') globalThis.crypto.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 4122 variant
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  // ---- requests that may publish: X-Idempotency-Key (server: app/web/idempotency.py)

  const IDEMPOTENCY_HEADER = 'X-Idempotency-Key';
  // Answers that may hide a request the server did process: no answer at all (dropped connection)
  // or a proxy in between giving up (nginx 502 / 503 / 504 after its read timeout).
  const LOST_ANSWER_STATUSES = new Set([0, 502, 503, 504]);
  const IDEMPOTENT_RETRY_DELAYS = [1500, 4000]; // ms before each automatic retry after a lost answer
  let lostAnswer = null; // {sig, key} of the last request whose answer never came

  const isLostAnswer = (err) => err instanceof ApiError && LOST_ANSWER_STATUSES.has(err.status) && !(err.data && err.data.post);

  /**
   * POST api/posts, PUT api/posts/{id}, POST api/posts/{id}/publish — the requests that publish.
   * Every user action gets its own key, and the automatic retries after a lost answer reuse it:
   * the server replays the first request's answer instead of creating or publishing again (a
   * publication may outlast the proxy's timeout). A repeat of exactly the same request after the
   * retries ran out (the owner presses the button again) is still the same action: same key.
   */
  async function sendIdempotent(method, path, body = {}) {
    const sig = `${method} ${path} ${JSON.stringify(body)}`;
    const key = lostAnswer && lostAnswer.sig === sig ? lostAnswer.key : uuid4();
    lostAnswer = null;
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await request(method, path, body, { headers: { [IDEMPOTENCY_HEADER]: key } });
      } catch (err) {
        if (!isLostAnswer(err)) throw err;
        if (attempt >= IDEMPOTENT_RETRY_DELAYS.length) {
          lostAnswer = { sig, key };
          throw err;
        }
        await sleep(IDEMPOTENT_RETRY_DELAYS[attempt]);
      }
    }
  }

  /** Some endpoints may wrap the object ({"post": {...}}) — accept both shapes. */
  const unwrap = (data, key) => (data && typeof data === 'object' && data[key] && typeof data[key] === 'object' ? data[key] : data);

  async function loadChannels() {
    const data = await api.get('api/channels');
    const list = Array.isArray(data.channels) ? data.channels : [];
    updatePendingBadge(list.filter((c) => c.status === 'pending').length);
    return list;
  }

  /**
   * Channels of every status grouped by folders: {groups: [{folder|null, channels}], channels (flat),
   * folders}. Folders come in their order, the «Без папки» group (folder null) last.
   */
  async function loadGrouped() {
    let groups;
    try {
      const data = await api.get('api/channels/grouped');
      groups = Array.isArray(data.groups) ? data.groups : [];
    } catch (err) {
      if (!(err instanceof ApiError) || (err.status !== 404 && err.status !== 405)) throw err;
      const list = await loadChannels(); // a backend without folders: one unnamed group
      groups = list.length ? [{ folder: null, channels: list }] : [];
    }
    groups = groups
      .filter((g) => g && typeof g === 'object')
      .map((g) => ({
        folder: g.folder && typeof g.folder === 'object' ? g.folder : null,
        channels: Array.isArray(g.channels) ? g.channels : [],
      }));
    const channels = groups.flatMap((g) => g.channels);
    updatePendingBadge(channels.filter((c) => c.status === 'pending').length);
    return { groups, channels, folders: groups.filter((g) => g.folder).map((g) => g.folder) };
  }

  /** The bot's timezone (schedules and the plan use it); loaded once. */
  async function ensureServerTz() {
    if (state.serverTz) return state.serverTz;
    try {
      const s = await api.get('api/settings');
      state.serverTz = s.timezone || '';
      if (typeof s.premium_emoji === 'boolean') state.premiumEmoji = s.premium_emoji;
    } catch (err) {
      if (err.silent) throw err;
    }
    return state.serverTz;
  }

  /** Slot list of a channel row: `slots` from the API (or the raw slots_json column). */
  function channelSlots(ch) {
    if (ch && ch.slots && typeof ch.slots === 'object') return normSlots(ch.slots);
    if (ch && typeof ch.slots_json === 'string') {
      try {
        return normSlots(JSON.parse(ch.slots_json));
      } catch {
        return normSlots(null);
      }
    }
    return normSlots(null);
  }

  const channelHasSlots = (ch) => slotsCount(channelSlots(ch)) > 0;
  const channelSlotsSummary = (ch) => (ch && ch.slots_summary) || slotsSummary(channelSlots(ch));

  // ======================================================================= 4. UI primitives

  function toast(message, type = 'info', timeout) {
    const box = $('#toasts');
    if (!box) return;
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    const iconName = type === 'error' ? 'alert' : type === 'success' ? 'check' : 'info';
    el.innerHTML = `${icon(iconName, 'toast-icon')}<span class="toast-text"></span><button type="button" class="toast-close" aria-label="Закрыть уведомление">${icon('x')}</button>`;
    $('.toast-text', el).textContent = String(message || '');
    box.appendChild(el);
    while (box.children.length > 4) box.firstElementChild.remove();
    requestAnimationFrame(() => el.classList.add('show'));
    const close = () => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 220);
    };
    $('.toast-close', el).addEventListener('click', close);
    setTimeout(close, timeout ?? (type === 'error' ? 6500 : 3500));
  }

  /** Server messages like "Ряд 2, кнопка 1: …" → selector of that field in the buttons editor. */
  function buttonErrorFocus(message) {
    const m = /Ряд (\d+), кнопка (\d+)/.exec(String(message || ''));
    if (!m || !state.composer) return '';
    const field = /ссылк|url/i.test(message) ? 'url' : 'text';
    return `.btn-item[data-row="${Number(m[1]) - 1}"][data-col="${Number(m[2]) - 1}"] [data-field="${field}"]`;
  }

  function handleError(err) {
    if (!err || err.silent) return;
    if (!(err instanceof ApiError) && !(err instanceof ValidationError)) console.error(err);
    toast(err.message || 'Что-то пошло не так', 'error');
    const focus = err.focus || (err instanceof ApiError ? buttonErrorFocus(err.message) : '');
    if (focus) {
      const field = document.querySelector(focus);
      if (field) field.focus();
    }
  }

  let modalSeq = 0;

  /**
   * Opens a modal <dialog>. `body` is an HTML string (caller escapes it) or a Node.
   * Resolves with the `value` of the clicked action, or null when dismissed (Esc, backdrop, ×).
   *
   * Actions with `submit: true` call `onSubmit(value, dlg)` first while the dialog stays open
   * (spinner on the button): a thrown error is shown inside the dialog, `false` keeps it open, any
   * other result closes it and becomes the resolved value (`undefined` → the action value).
   */
  function openModal({ title, body = '', actions = [], wide = false, onSubmit = null }) {
    return new Promise((resolve) => {
      const titleId = `modal-title-${++modalSeq}`;
      const dlg = document.createElement('dialog');
      dlg.className = `modal${wide ? ' modal-wide' : ''}`;
      dlg.setAttribute('aria-labelledby', titleId);
      const foot = actions.map((a) => `<button type="button" class="btn btn-${a.kind || 'secondary'}" data-modal-value="${esc(a.value)}"${a.submit ? ' data-submit="1"' : ''}${a.autofocus ? ' autofocus' : ''}>${a.icon ? icon(a.icon) : ''}<span>${esc(a.label)}</span></button>`).join('');
      dlg.innerHTML = `<div class="modal-box">
        <div class="modal-head"><h2 id="${titleId}">${esc(title)}</h2>
          <button type="button" class="icon-btn" data-modal-close aria-label="Закрыть">${icon('x')}</button></div>
        <div class="modal-body"></div>
        <p class="modal-error" role="alert" hidden></p>
        ${foot ? `<div class="modal-foot">${foot}</div>` : ''}
      </div>`;
      const bodyEl = $('.modal-body', dlg);
      if (body instanceof Node) bodyEl.appendChild(body);
      else bodyEl.innerHTML = body;
      const errEl = $('.modal-error', dlg);

      const opener = document.activeElement;
      let result = null;
      let downOnBackdrop = false;
      let working = false;
      const close = () => {
        if (!working) dlg.close();
      };
      dlg.addEventListener('cancel', (e) => {
        if (working) e.preventDefault();
      });
      dlg.addEventListener('mousedown', (e) => {
        downOnBackdrop = e.target === dlg;
      });
      dlg.addEventListener('click', (e) => {
        if (e.target === dlg) {
          if (downOnBackdrop) close();
          return;
        }
        if (e.target.closest('[data-modal-close]')) {
          close();
          return;
        }
        const btn = e.target.closest('[data-modal-value]');
        if (!btn || working) return;
        const value = btn.dataset.modalValue;
        if (!onSubmit || btn.dataset.submit !== '1') {
          result = value;
          dlg.close();
          return;
        }
        working = true;
        errEl.hidden = true;
        $$('.modal-foot .btn', dlg).forEach((b) => { b.disabled = true; });
        btn.classList.add('is-busy');
        Promise.resolve()
          .then(() => onSubmit(value, dlg))
          .then((res) => {
            working = false;
            if (res === false) return;
            result = res === undefined ? value : res;
            dlg.close();
          })
          .catch((err) => {
            working = false;
            if (err && err.silent) return;
            if (!(err instanceof ApiError) && !(err instanceof ValidationError)) console.error(err);
            errEl.textContent = (err && err.message) || 'Что-то пошло не так';
            errEl.hidden = false;
            if (err && err.focus) {
              const field = $(err.focus, dlg);
              if (field) field.focus();
            }
          })
          .finally(() => {
            if (!dlg.open) return;
            btn.classList.remove('is-busy');
            $$('.modal-foot .btn', dlg).forEach((b) => { b.disabled = false; });
          });
      });
      dlg.addEventListener('close', () => {
        dlg.remove();
        if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus({ preventScroll: true });
        resolve(result);
      });
      document.body.appendChild(dlg);
      dlg.showModal();
    });
  }

  /** Small form dialog with one text field. Resolves with the result of `save(value)` or null. */
  function promptModal({ title, label, value = '', placeholder = '', confirmLabel = 'Сохранить', maxLength = 40, hint = '', save }) {
    const body = document.createElement('div');
    body.innerHTML = `<label class="field"><span class="label">${esc(label)}</span>
      <input class="input" data-prompt maxlength="${num(maxLength)}" autocomplete="off" placeholder="${esc(placeholder)}"></label>
      ${hint ? `<p class="hint">${esc(hint)}</p>` : ''}`;
    const input = $('[data-prompt]', body);
    input.value = value;
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.isComposing) {
        e.preventDefault();
        const btn = input.closest('dialog') && $('[data-submit="1"]', input.closest('dialog'));
        if (btn) btn.click();
      }
    });
    setTimeout(() => {
      input.focus();
      input.select();
    }, 30);
    return openModal({
      title,
      body,
      actions: [
        { label: 'Отмена', value: 'cancel' },
        { label: confirmLabel, value: 'ok', kind: 'primary', submit: true },
      ],
      onSubmit: async () => {
        const text = input.value.trim();
        if (!text) throw new ValidationError(`${label}: заполните поле`, '[data-prompt]');
        return save(text);
      },
    });
  }

  /** Confirmation dialog (replacement for window.confirm). Resolves true/false. */
  async function confirmModal({ title, message = '', html = '', confirmLabel = 'Подтвердить', cancelLabel = 'Отмена', danger = false, confirmIcon = '' }) {
    const body = `${message ? `<p>${esc(message)}</p>` : ''}${html}`;
    const value = await openModal({
      title,
      body,
      actions: [
        { label: cancelLabel, value: 'cancel', kind: 'secondary', autofocus: danger },
        { label: confirmLabel, value: 'ok', kind: danger ? 'danger' : 'primary', autofocus: !danger, icon: confirmIcon },
      ],
    });
    return value === 'ok';
  }

  /** Disables a button and shows a spinner while `fn` runs. Ignores clicks on a busy button. */
  async function busy(btn, fn) {
    if (btn && btn.disabled) return undefined;
    if (btn) {
      btn.disabled = true;
      btn.classList.add('is-busy');
      btn.setAttribute('aria-busy', 'true');
    }
    try {
      return await fn();
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.classList.remove('is-busy');
        btn.removeAttribute('aria-busy');
      }
    }
  }

  /**
   * Delegated click handling for `[data-action]` elements inside `root`. A page root also handles
   * its actions in the topbar (pageHead() renders them there, outside of the page element).
   */
  function onAction(root, handlers) {
    const hosts = [root];
    if (root === state.pageRoot && state.pageActions) hosts.push(state.pageActions);
    hosts.forEach((host) => host.addEventListener('click', (e) => {
      const el = e.target.closest('[data-action]');
      if (!el || !host.contains(el)) return;
      const handler = handlers[el.dataset.action];
      if (!handler) return;
      e.preventDefault();
      if (el.disabled) return;
      Promise.resolve()
        .then(() => handler(el, e))
        .catch(handleError);
    }));
  }

  /** Delegated <form> submit handling by form id; the submit button shows a spinner. */
  function onSubmit(root, handlers) {
    root.addEventListener('submit', (e) => {
      const form = e.target;
      const handler = handlers[form.id];
      if (!handler) return;
      e.preventDefault();
      const btn = form.querySelector('[type="submit"]');
      busy(btn, () => handler(form)).catch(handleError);
    });
  }

  // ---- shell: topbar title / subtitle / page actions

  /** New page: a fresh (listener-free) actions slot and the default title of the section. */
  function resetHead(title) {
    const old = $('#page-actions');
    const fresh = old.cloneNode(false);
    old.replaceWith(fresh);
    state.pageActions = fresh;
    setHead({ title });
  }

  /**
   * Fills the topbar. `subtitle` and `actions` are HTML (callers escape dynamic parts). On phones
   * the action labels collapse into icons, so every button gets its label as a tooltip.
   */
  function setHead({ title, subtitle = '', actions = '' } = {}) {
    if (title !== undefined) {
      $('#page-title').textContent = title;
      setPageTitle(title);
    }
    const sub = $('#page-sub');
    sub.innerHTML = subtitle;
    sub.hidden = !subtitle;
    sub.title = sub.textContent.trim();
    const box = state.pageActions || $('#page-actions');
    box.innerHTML = actions;
    $$('.btn', box).forEach((b) => {
      if (!b.title) b.title = b.textContent.trim();
    });
  }

  /** Puts the page title, subtitle and actions into the topbar; returns '' (nothing in the page). */
  function pageHead(title, { subtitle = '', actions = '' } = {}) {
    setHead({ title, subtitle, actions });
    return '';
  }

  /** Empty state block. `text` and `action` are HTML (callers escape dynamic parts). */
  function emptyState(iconName, title, text = '', action = '') {
    return `<div class="empty">
      <div class="empty-icon">${icon(iconName)}</div>
      <h3>${esc(title)}</h3>
      ${text ? `<p>${text}</p>` : ''}
      ${action ? `<div class="empty-actions">${action}</div>` : ''}
    </div>`;
  }

  /** Skeleton placeholder while a page or a block loads (no bare «Загрузка…» text). */
  function loadingHtml(lines = 3) {
    const widths = [['38%', '100%', '72%'], ['30%', '86%', '64%'], ['44%', '92%', '58%']];
    const cards = Array.from({ length: lines }, (_, i) => {
      const w = widths[i % widths.length];
      return `<div class="card sk-card" style="--d:${(i * 0.08).toFixed(2)}s">${w.map((x) => `<div class="skeleton" style="width:${x}"></div>`).join('')}</div>`;
    }).join('');
    return `<div class="loading" role="status" aria-live="polite"><span class="sr-only">Загрузка…</span>${cards}</div>`;
  }

  function newPostButton(label = 'Новый пост') {
    return `<a class="btn btn-primary" href="#/compose">${icon('plus')}<span>${esc(label)}</span></a>`;
  }

  // Badge tones of the design system: neutral / accent / info / success / warning / danger.
  const POST_STATUS = {
    draft: ['Черновик', 'neutral'],
    scheduled: ['Запланирован', 'accent'],
    sending: ['Публикуется', 'info'],
    sent: ['Опубликован', 'success'],
    partial: ['Частично', 'warning'],
    failed: ['Ошибка', 'danger'],
    deleted: ['Удалён', 'neutral'],
  };
  const TARGET_STATUS = {
    pending: ['Ожидает', 'warning'],
    sent: ['Опубликован', 'success'],
    failed: ['Ошибка', 'danger'],
    deleted: ['Удалён', 'neutral'],
  };
  const CHANNEL_STATUS = {
    active: ['Активен', 'success'],
    pending: ['Ожидает подтверждения', 'warning'],
    ignored: ['Скрыт', 'neutral'],
    left: ['Нет доступа', 'danger'],
  };

  /** Status badge with a dot (statuses of posts, targets, channels). */
  function badge(map, status) {
    const [label, tone] = map[status] || [status || '—', 'neutral'];
    return `<span class="badge b-${tone}"><span class="dot" aria-hidden="true"></span>${esc(label)}</span>`;
  }

  const statusBadge = (status) => badge(POST_STATUS, status);

  function updatePendingBadge(count) {
    $$('[data-pending]').forEach((el) => {
      el.hidden = !count;
      el.textContent = count ? String(count) : '';
      el.setAttribute('aria-label', count ? `Ожидают подтверждения: ${count}` : '');
    });
  }

  function setPageTitle(title) {
    document.title = title === 'Посты' ? 'Посты — панель' : `${title} — Посты`;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Clipboard API is unavailable on plain http — fall back to a hidden textarea.
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try {
        ok = document.execCommand('copy');
      } catch {
        ok = false;
      }
      ta.remove();
      return ok;
    }
  }

  // ======================================================================= 5. Telegram HTML sanitizer

  const ALLOWED_TAGS = new Set([
    'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'a', 'code', 'pre',
    'blockquote', 'tg-spoiler', 'span', 'tg-emoji',
  ]);

  /**
   * Rebuilds Telegram HTML from an allow-list: only known tags survive, attributes are limited to
   * a[href] (http/https/tg/mailto), code[class=language-*], span[class=tg-spoiler] and
   * blockquote[expandable]. Any other element is replaced with its text.
   */
  function sanitizeTelegramHtml(html) {
    const tpl = document.createElement('template');
    tpl.innerHTML = String(html ?? '');
    const frag = document.createDocumentFragment();
    for (const child of Array.from(tpl.content.childNodes)) {
      const clean = cleanNode(child);
      if (clean) frag.appendChild(clean);
    }
    return frag;
  }

  function cleanNode(node) {
    if (node.nodeType === Node.TEXT_NODE) return document.createTextNode(node.nodeValue);
    if (node.nodeType !== Node.ELEMENT_NODE) return null;
    const tag = node.localName;
    if (!ALLOWED_TAGS.has(tag)) return document.createTextNode(node.textContent || '');
    const el = document.createElement(tag);
    if (tag === 'a') {
      const href = safeHref(node.getAttribute('href'));
      if (href) {
        el.setAttribute('href', href);
        el.setAttribute('target', '_blank');
        el.setAttribute('rel', 'noopener noreferrer nofollow');
      }
    } else if (tag === 'code') {
      const cls = node.getAttribute('class') || '';
      if (/^language-[\w#+.-]{1,40}$/.test(cls)) el.className = cls;
    } else if (tag === 'span') {
      if ((node.getAttribute('class') || '').trim() === 'tg-spoiler') el.className = 'tg-spoiler';
    } else if (tag === 'blockquote') {
      if (node.hasAttribute('expandable')) el.setAttribute('expandable', '');
    }
    for (const child of Array.from(node.childNodes)) {
      const clean = cleanNode(child);
      if (clean) el.appendChild(clean);
    }
    if (tag === 'pre') {
      const code = el.firstElementChild;
      if (code && code.localName === 'code' && code.className.startsWith('language-')) {
        el.dataset.lang = code.className.slice('language-'.length);
      }
    }
    return el;
  }

  // ======================================================================= 6. Auth

  const state = {
    auth: { password_set: false, authenticated: true, password_source: null },
    routeToken: 0,
    currentHash: location.hash || '#/',
    loginShown: false,
    composer: null,
    botUsername: '', // the bot that publishes (from api/overview): the «forward a ready post» hint links to it
    serverTz: '',
    premiumEmoji: null, // «У бота есть юзернейм с Fragment» (api/settings, api/render); null = unknown
    pageRoot: null, // element of the current page (its actions live in the topbar: pageActions)
    pageActions: null,
  };

  function showLogin(message = '') {
    const box = $('#login');
    $('#login-msg').textContent = message || 'Панель защищена паролем. Введите его, чтобы продолжить.';
    $('#login-error').textContent = '';
    $('#login-hint').textContent = state.auth.password_source === 'env'
      ? 'Пароль задан в .env (WEB_PASSWORD) на сервере бота.'
      : 'Забыли пароль? Сбросьте его в боте: ⚙️ Настройки → «Сбросить пароль веб-панели».';
    if (state.loginShown) return;
    state.loginShown = true;
    box.hidden = false;
    $('#app').inert = true;
    document.body.classList.add('is-locked');
    const input = $('#login-password');
    input.value = '';
    setTimeout(() => input.focus(), 30);
  }

  function hideLogin() {
    state.loginShown = false;
    $('#login').hidden = true;
    $('#app').inert = false;
    document.body.classList.remove('is-locked');
  }

  async function onLoginSubmit(e) {
    e.preventDefault();
    const input = $('#login-password');
    const errorEl = $('#login-error');
    const password = input.value;
    if (!password) {
      errorEl.textContent = 'Введите пароль';
      input.focus();
      return;
    }
    await busy($('#login-submit'), async () => {
      try {
        await api.post('api/auth/login', { password }, { allow401: true });
      } catch (err) {
        const generic = !err.data || !err.data.error || err.data.error === 'auth_required';
        errorEl.textContent = err.status === 401 && generic ? 'Неверный пароль' : err.message;
        input.select();
        return;
      }
      state.auth = { ...state.auth, password_set: true, authenticated: true };
      hideLogin();
      refreshShell();
      // Keep an unsaved post on screen: the user only has to repeat the last action.
      if (state.composer && state.composer.dirty) toast('Вы вошли. Повторите последнее действие.', 'info');
      else route();
    });
  }

  // ======================================================================= 7. Router

  const PAGES = {
    '': { nav: 'overview', title: 'Обзор', render: pageOverview },
    // An existing post (#/compose/<id>) belongs to «Посты»; only a new one to «Новый пост».
    compose: { nav: (r) => (r.segs[1] ? 'posts' : 'compose'), title: 'Новый пост', render: pageCompose },
    posts: { nav: 'posts', title: 'Посты', render: pagePosts },
    plan: { nav: 'plan', title: 'Контент-план', render: pagePlan },
    channels: { nav: 'channels', title: 'Каналы', render: pageChannels },
    stats: { nav: 'stats', title: 'Статистика', render: pageStats },
    settings: { nav: 'settings', title: 'Настройки', render: pageSettings },
  };

  function parseRoute() {
    const raw = (location.hash || '#/').replace(/^#/, '');
    const [pathPart, queryPart = ''] = raw.split('?');
    const segs = pathPart.split('/').filter(Boolean).map((s) => decodeURIComponent(s));
    return { segs, query: new URLSearchParams(queryPart) };
  }

  /** Changes the URL without re-rendering (and without triggering the hashchange guard). */
  function replaceHash(hash) {
    if (location.hash !== hash) history.replaceState(null, '', hash);
    state.currentHash = hash;
  }

  function setActiveNav(key) {
    $$('[data-nav]').forEach((a) => {
      if (a.dataset.nav === key) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }

  function errorStateHtml(err) {
    return emptyState(
      'alert',
      err && err.status === 404 ? 'Не найдено' : 'Не удалось загрузить данные',
      esc(err && err.message ? err.message : 'Неизвестная ошибка'),
      '<button type="button" class="btn btn-primary" data-retry>Повторить</button> <a class="btn btn-secondary" href="#/">На главную</a>',
    );
  }

  async function route() {
    if (state.loginShown) return;
    const r = parseRoute();
    const page = PAGES[r.segs[0] || ''];
    if (!page) {
      replaceHash('#/');
      route();
      return;
    }
    const token = ++state.routeToken;
    state.composer = null;
    state.currentHash = location.hash || '#/';
    setActiveNav(typeof page.nav === 'function' ? page.nav(r) : page.nav);
    closeDrawer();

    const view = $('#view');
    const root = document.createElement('div');
    root.className = 'page';
    root.innerHTML = loadingHtml();
    state.pageRoot = root;
    resetHead(page.title);
    view.replaceChildren(root);
    window.scrollTo(0, 0);

    const ctx = { alive: () => token === state.routeToken };
    try {
      await page.render(root, r, ctx);
    } catch (err) {
      if (!ctx.alive() || err.silent) return;
      if (!(err instanceof ApiError)) console.error(err);
      root.innerHTML = errorStateHtml(err);
      const retry = $('[data-retry]', root);
      if (retry) retry.addEventListener('click', () => route());
    }
  }

  async function onHashChange() {
    const target = location.hash || '#/';
    if (target === state.currentHash) return;
    if (!target.startsWith('#/')) {
      // An in-page anchor (e.g. "#view"), not a route: restore the route, the browser has already
      // moved focus to the fragment target.
      history.replaceState(null, '', state.currentHash);
      return;
    }
    const cmp = state.composer;
    if (cmp && cmp.dirty) {
      // Put the old URL back while we ask; navigate only after confirmation.
      history.replaceState(null, '', state.currentHash);
      const leave = await confirmModal({
        title: 'Уйти без сохранения?',
        message: 'В посте есть несохранённые изменения — они пропадут.',
        confirmLabel: 'Уйти',
        cancelLabel: 'Остаться',
        danger: true,
      });
      if (!leave) return;
      cmp.dirty = false;
      location.hash = target;
      return;
    }
    route();
  }

  // ======================================================================= 8a. Media helpers

  const MEDIA_TYPES = ['photo', 'video', 'animation'];
  const MEDIA_LABELS = { photo: 'Фото', video: 'Видео', animation: 'GIF' };
  const MEDIA_WORDS = { photo: 'фото', video: 'видео', animation: 'GIF' };
  const MAX_ALBUM = 10;
  const PHOTO_MAX_BYTES = 10 * 1024 * 1024;
  const VIDEO_MAX_BYTES = 50 * 1024 * 1024;
  const ALBUM_GIF_ERROR = 'GIF нельзя добавить в альбом — отправьте его отдельным постом';
  const ALBUM_BUTTONS_ERROR = 'Telegram не поддерживает кнопки у альбомов. Оставьте одно фото/видео или уберите кнопки.';

  const mediaType = (t) => (MEDIA_TYPES.includes(t) ? t : t === 'gif' ? 'animation' : 'photo');

  /** Media items of a post from the API (V2 `media`, or the V1 single-picture fields). */
  function postMedia(p) {
    if (p && Array.isArray(p.media)) {
      return p.media
        .filter((m) => m && typeof m === 'object')
        .map((m, i) => ({ ...m, type: mediaType(m.type), index: Number.isInteger(m.index) ? m.index : i }));
    }
    if (p && p.has_photo) return [{ type: 'photo', url: p.photo_url || `api/posts/${num(p.id)}/photo`, index: 0 }];
    return [];
  }

  /** "Фото" / "Видео" / "GIF" / "Альбом: 3 фото, 1 видео" / ''. */
  function describeMedia(items) {
    if (!items.length) return '';
    if (items.length === 1) return MEDIA_LABELS[items[0].type] || 'Фото';
    const counts = {};
    items.forEach((m) => { counts[m.type] = (counts[m.type] || 0) + 1; });
    return `Альбом: ${MEDIA_TYPES.filter((t) => counts[t]).map((t) => `${counts[t]} ${MEDIA_WORDS[t]}`).join(', ')}`;
  }

  /** Summary from the plan API: {media_kind, media_count} → the same words. */
  function describeKind(kind, count) {
    if (kind === 'album') return `Альбом · ${num(count)}`;
    return MEDIA_LABELS[kind] || '';
  }

  const isGifFile = (m) => /\.gif$/i.test(m.path || '') || /\.gif([?#]|$)/i.test(m.url || '') || m.mime === 'image/gif';

  /** <img>/<video> for a media item (always muted). `autoplay` plays it in a loop (GIF preview). */
  function mediaElHtml(item, { autoplay = false } = {}) {
    const src = safeSrc(item.localUrl || item.url);
    if (!src) return `<span class="media-ph">${icon(item.type === 'photo' ? 'image' : 'video')}</span>`;
    if (item.type === 'photo' || isGifFile(item)) {
      return `<img src="${esc(src)}" alt="" decoding="async" data-media>`;
    }
    // "#t=0.1" makes browsers show the first frame of a paused video instead of a black box.
    return autoplay
      ? `<video src="${esc(src)}" muted playsinline autoplay loop aria-hidden="true" data-media></video>`
      : `<video src="${esc(src)}#t=0.1" muted playsinline loop preload="metadata" aria-hidden="true" data-media></video>`;
  }

  function onMediaError(e) {
    const el = e.target;
    if (!(el instanceof HTMLElement) || !el.hasAttribute('data-media')) return;
    if (el.tagName === 'VIDEO' && !el.dataset.fallback) {
      // An animation may be stored as a real GIF file: try it as an image.
      const img = document.createElement('img');
      img.alt = '';
      img.src = (el.getAttribute('src') || '').replace(/#t=[\d.]+$/, '');
      img.setAttribute('data-media', '');
      img.dataset.fallback = '1';
      el.replaceWith(img);
      return;
    }
    const ph = document.createElement('span');
    ph.className = 'media-ph';
    ph.title = 'Предпросмотр недоступен';
    ph.innerHTML = `${icon('image')}<span class="sr-only">Предпросмотр недоступен</span>`;
    el.replaceWith(ph);
  }

  /**
   * List thumbnail of the first media item. Videos are shown as an icon tile: loading every video
   * of a list would download files (a Telegram file is fetched by the server on first view).
   */
  function mediaThumbHtml(items, size = '', title = '') {
    const first = items[0];
    if (!first) return `<span class="thumb thumb-empty ${size}">${icon('text')}</span>`;
    const src = safeSrc(first.url);
    const asImage = src && (first.type === 'photo' || isGifFile(first));
    const inner = asImage
      ? `<img src="${esc(src)}" alt="" loading="lazy" decoding="async" data-media>`
      : `<span class="thumb-video">${icon(first.type === 'photo' ? 'image' : 'video')}</span>`;
    let tag = '';
    if (first.type === 'animation') tag = '<span class="thumb-tag">GIF</span>';
    else if (first.type === 'video') tag = `<span class="thumb-play" aria-hidden="true">${icon('play', 'i-fill')}</span>`;
    const count = items.length > 1
      ? `<span class="thumb-count" title="${esc(title || describeMedia(items))}">${icon('album')}${items.length}</span>`
      : '';
    return `<span class="thumb ${size}${asImage ? '' : ' thumb-empty'}">${inner}${tag}${count}</span>`;
  }

  /** Thumbnail for a plan item ({id, thumb_type, media_count}). */
  function summaryThumbHtml(post) {
    const type = post && post.thumb_type ? mediaType(post.thumb_type) : null;
    if (!type) return mediaThumbHtml([], 'sm');
    const first = { type, url: safeSrc(post.thumb_url) || (type === 'photo' ? `api/posts/${num(post.id)}/media/0` : '') };
    const items = Array.from({ length: Math.max(1, num(post.media_count)) }, (_, i) => (i === 0 ? first : { type: 'photo' }));
    return mediaThumbHtml(items, 'sm', describeKind(post.media_kind, post.media_count));
  }

  // ======================================================================= 8b. Overview page

  async function pageOverview(root, r, ctx) {
    setPageTitle('Обзор');
    const [ov, channels, postsData, upcoming] = await Promise.all([
      api.get('api/overview'),
      loadChannels(),
      api.get('api/posts?status=all&limit=5&offset=0'),
      // Upcoming publications are shown separately: the recent list is ordered by activity, so a
      // post scheduled long ago for next week would not appear in it.
      api.get('api/plan/timeline?days=7').then((d) => (Array.isArray(d.items) ? d.items : [])).catch((err) => {
        if (err.silent) throw err;
        return null;
      }),
    ]);
    if (!ctx.alive()) return;
    applyOverviewToShell(ov);

    const s = ov.stats || {};
    const active = channels.filter((c) => c.status === 'active');
    const pending = channels.filter((c) => c.status === 'pending');
    const posts = Array.isArray(postsData.posts) ? postsData.posts : [];
    const bot = ov.bot || {};
    const subtitle = bot.username
      ? `Бот <a href="https://t.me/${esc(encodeURIComponent(bot.username))}" target="_blank" rel="noopener noreferrer">@${esc(bot.username)}</a> публикует посты в ваши каналы`
      : 'Публикация постов в ваши каналы';
    const soon = (upcoming || []).filter((it) => num(it.ts) >= nowTs() - 60).slice(0, 6);

    root.innerHTML = `
      ${pageHead('Обзор', { subtitle, actions: newPostButton() })}
      ${pending.length ? pendingBannerHtml(pending) : ''}
      <section class="tiles" aria-label="Ключевые показатели">
        ${kpiHtml({ label: 'Каналы', value: fmtNum(s.channels_active), sub: s.channels_pending ? `ещё ${fmtNum(s.channels_pending)} ${plural(s.channels_pending, ['ждёт', 'ждут', 'ждут'])} подтверждения` : 'подключено', href: '#/channels' })}
        ${kpiHtml({ label: 'Подписчики', value: fmtNum(s.members_total), sub: 'во всех каналах', href: '#/stats' })}
        ${kpiHtml({ label: 'Опубликовано', value: fmtNum(s.posts_sent), sub: plural(s.posts_sent, POSTS_FORMS), href: '#/posts?status=sent' })}
        ${kpiHtml({ label: 'Запланировано', value: fmtNum(s.posts_scheduled), sub: 'в контент-плане', href: '#/plan' })}
        ${kpiHtml({ label: 'Черновики', value: fmtNum(s.posts_draft), sub: 'не опубликованы', href: '#/posts?status=draft' })}
      </section>

      <div class="grid-2">
        ${upcoming === null ? '' : `<section class="card flush">
          <div class="card-head"><h2>Ближайшие публикации</h2><a class="btn btn-ghost btn-sm" href="#/plan">${icon('calendar')}<span>Контент-план</span></a></div>
          ${soon.length
            ? `<div class="row-list">${soon.map(upcomingRowHtml).join('')}</div>`
            : emptyState('calendar', 'На неделю ничего не запланировано', 'Настройте расписание каналов и ставьте посты в очередь — они выйдут сами.', '<a class="btn btn-secondary" href="#/plan">Открыть контент-план</a>')}
        </section>`}
        <section class="card flush">
          <div class="card-head"><h2>Последние посты</h2><a class="btn btn-ghost btn-sm" href="#/posts">Все посты</a></div>
          ${posts.length
            ? `<div class="row-list">${posts.map(postRowHtml).join('')}</div>`
            : emptyState('edit', 'Постов ещё нет', 'Создайте первый пост: текст с разметкой, фото или видео и цветные кнопки.', newPostButton('Написать пост'))}
        </section>
      </div>

      <section class="section">
        <div class="section-head"><h2>Каналы</h2><a class="btn btn-ghost btn-sm" href="#/channels">Все каналы</a></div>
        ${active.length
          ? `<div class="grid-cards">${active.map(channelMiniHtml).join('')}</div>`
          : emptyState('channels', 'Каналов пока нет', 'Сделайте бота администратором канала с правом публикации — он сам предложит добавить канал. Или добавьте канал вручную.', '<a class="btn btn-primary" href="#/channels">Добавить канал</a>')}
      </section>`;

    onAction(root, pendingHandlers(() => route()));
  }

  function upcomingRowHtml(it) {
    const post = it.post || {};
    const ch = it.channel || {};
    const kind = describeKind(post.media_kind, post.media_count);
    return `<a class="post-row" href="#/compose/${num(post.id)}">
      ${summaryThumbHtml(post)}
      <span class="post-row-main">
        <span class="post-row-text">${esc(post.excerpt || '(без текста)')}</span>
        <span class="post-row-meta">${esc(fmtSlotWhen(it.ts))} · ${esc(ch.title || 'Канал')}${kind ? ` · ${esc(kind)}` : ''}</span>
      </span>
      <span class="badge b-accent">${esc(it.time || zonedParts(it.ts).time)}</span>
    </a>`;
  }

  /** Stat tile: label, value (HTML), optional hint; a link when `href` is given. */
  function kpiHtml({ label, value, sub = '', href = '', extra = '' }) {
    const tag = href ? 'a' : 'div';
    return `<${tag} class="tile"${href ? ` href="${esc(href)}"` : ''}>
      <span class="tile-label">${esc(label)}</span>
      <span class="tile-value">${value}</span>
      ${sub || extra ? `<span class="tile-hint">${extra}${esc(sub)}</span>` : ''}
    </${tag}>`;
  }

  function pendingBannerHtml(pending) {
    const items = pending.map((c) => `
      <li class="pending-item">
        ${avatarHtml(c.id, c.title, 'sm')}
        <div class="pending-info"><b>${esc(c.title || 'Без названия')}</b>${c.username ? `<span class="muted small">@${esc(c.username)}</span>` : ''}</div>
        <div class="pending-actions">
          <button type="button" class="btn btn-primary btn-sm" data-action="approve" data-id="${num(c.id)}">${icon('check')}<span>Добавить</span></button>
          <button type="button" class="btn btn-secondary btn-sm" data-action="ignore" data-id="${num(c.id)}">Игнорировать</button>
        </div>
      </li>`).join('');
    return `<section class="banner" aria-label="Новые каналы">
      <div class="banner-icon">${icon('channels')}</div>
      <div class="banner-body">
        <h2>Бота назначили администратором — добавить ${pending.length > 1 ? 'каналы' : 'канал'}?</h2>
        <p>После подтверждения в ${pending.length > 1 ? 'них' : 'нём'} можно будет публиковать посты.</p>
        <ul class="pending-list">${items}</ul>
      </div>
    </section>`;
  }

  /** Approve / ignore handlers shared by the overview and channels pages. */
  function pendingHandlers(onDone) {
    return {
      approve: (el) => busy(el, async () => {
        const res = await api.post(`api/channels/${num(el.dataset.id)}/approve`);
        const ch = unwrap(res, 'channel');
        toast(ch && ch.title ? `Канал «${ch.title}» добавлен` : 'Канал добавлен', 'success');
        onDone();
      }),
      ignore: (el) => busy(el, async () => {
        await api.post(`api/channels/${num(el.dataset.id)}/ignore`);
        toast('Канал скрыт. Его можно вернуть на странице «Каналы».', 'info');
        onDone();
      }),
    };
  }

  function channelMiniHtml(ch) {
    return `<a class="card ch-mini" href="#/stats/${num(ch.id)}">
      ${avatarHtml(ch.id, ch.title)}
      <span class="ch-mini-info">
        <b>${esc(ch.title || 'Без названия')}</b>
        <span class="muted small">${ch.username ? `@${esc(ch.username)}` : 'приватный канал'}</span>
      </span>
      <span class="ch-mini-num"><b>${fmtNum(ch.members_count)}</b><span class="muted small">${esc(plural(ch.members_count, MEMBERS_FORMS))}</span></span>
    </a>`;
  }

  function postExcerpt(p) {
    if (p.excerpt) return p.excerpt;
    const media = postMedia(p);
    if (!media.length) return '(пустой пост)';
    return media.length > 1 ? `${describeMedia(media)} без подписи` : `${MEDIA_LABELS[media[0].type]} без подписи`;
  }

  /** Times of the post's targets that are waiting in the plan (pending with a time), ascending. */
  function pendingTimes(p) {
    return (p.targets || [])
      .filter((t) => t.status === 'pending' && t.scheduled_at)
      .map((t) => num(t.scheduled_at))
      .sort((a, b) => a - b);
  }

  function postTimeText(p) {
    switch (p.status) {
      case 'scheduled': {
        const times = pendingTimes(p);
        const first = times[0] || p.scheduled_at;
        return new Set(times).size > 1 ? `Ближайшая публикация ${fmtWhen(first)}` : `Запланирован на ${fmtWhen(first)}`;
      }
      case 'sent':
      case 'partial':
      case 'deleted':
        return p.sent_at ? `Опубликован ${fmtWhen(p.sent_at)}` : `Создан ${fmtWhen(p.created_at)}`;
      case 'sending':
        return 'Публикуется…';
      default:
        return `Создан ${fmtWhen(p.created_at)}`;
    }
  }

  const targetTitle = (t) => t.title || t.channel_title || `Канал ${t.channel_id}`;

  function postRowHtml(p) {
    const titles = (p.targets || []).map(targetTitle).join(', ');
    return `<a class="post-row" href="#/compose/${num(p.id)}">
      ${mediaThumbHtml(postMedia(p), 'sm')}
      <span class="post-row-main">
        <span class="post-row-text">${esc(postExcerpt(p))}</span>
        <span class="post-row-meta">${esc(postTimeText(p))}${titles ? ` · ${esc(titles)}` : ''}</span>
      </span>
      ${statusBadge(p.status)}
    </a>`;
  }

  // ======================================================================= 8c. Compose page

  const TEXT_LIMIT = 4096;
  const CAPTION_LIMIT = 1024;
  const MAX_ROWS = 10;
  const MAX_PER_ROW = 8;
  const FILE_KINDS = {
    'image/jpeg': 'photo', 'image/png': 'photo', 'image/webp': 'photo',
    'video/mp4': 'video', 'video/quicktime': 'video', 'image/gif': 'animation',
  };
  const EXT_MIME = {
    jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp',
    mp4: 'video/mp4', m4v: 'video/mp4', mov: 'video/quicktime', gif: 'image/gif',
  };
  const MEDIA_ACCEPT = 'image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime,.mov,.mp4,.gif';
  const UPLOAD_FAILED = 'Не все файлы загрузились — добавьте их снова и повторите';
  const BUTTON_STYLES = [
    { value: '', label: 'Обычная', cls: 'sw-default' },
    { value: 'primary', label: 'Синяя', cls: 'sw-primary' },
    { value: 'success', label: 'Зелёная', cls: 'sw-success' },
    { value: 'danger', label: 'Красная', cls: 'sw-danger' },
  ];
  const STYLE_VALUES = new Set(['primary', 'success', 'danger']);
  const OPTIONS = [
    ['silent', 'Без звука', 'Подписчики получат уведомление без звука'],
    ['protect', 'Запрет пересылки', 'Пост нельзя будет переслать или сохранить'],
    ['disable_preview', 'Без превью ссылок', 'Не показывать карточку ссылки (для постов без медиа)'],
    ['caption_above', 'Подпись над медиа', 'Текст будет над фото или видео, а не под ним'],
  ];
  const PUB_MODES = {
    keep: { label: 'Не менять время', hint: 'каналы выйдут в своё время', icon: 'clock' },
    now: { label: 'Сразу', hint: 'пост выйдет сразу после подтверждения', icon: 'bolt' },
    queue: { label: 'В очередь по расписанию', hint: 'в ближайший свободный слот каждого канала', icon: 'queue' },
    at: { label: 'Ко времени', hint: 'одна дата и время для всех каналов', icon: 'calendar' },
  };
  const TOOLBAR = [
    { fmt: 'bold', label: 'B', title: 'Жирный (Ctrl+B)', cls: 'tb-bold' },
    { fmt: 'italic', label: 'I', title: 'Курсив (Ctrl+I)', cls: 'tb-italic' },
    { fmt: 'underline', label: 'U', title: 'Подчёркнутый (Ctrl+U)', cls: 'tb-underline' },
    { fmt: 'strike', label: 'S', title: 'Зачёркнутый', cls: 'tb-strike' },
    { sep: true },
    { fmt: 'spoiler', iconName: 'spoiler', title: 'Спойлер' },
    { fmt: 'code', iconName: 'code', title: 'Моноширинный / блок кода' },
    { fmt: 'link', iconName: 'link', title: 'Ссылка (Ctrl+K)' },
    { sep: true },
    { fmt: 'quote', iconName: 'quote', title: 'Цитата' },
    { fmt: 'list', iconName: 'list', title: 'Список' },
  ];
  const WRAP_MARKS = { bold: '**', italic: '*', underline: '__', strike: '~~', spoiler: '||', code: '`' };
  const WRAP_PLACEHOLDERS = {
    bold: 'жирный текст', italic: 'курсив', underline: 'подчёркнутый', strike: 'зачёркнутый', spoiler: 'спойлер', code: 'код',
  };

  const emptyButton = () => ({ text: '', url: '', style: '' });

  function normalizeRows(rows) {
    if (!Array.isArray(rows)) return [];
    return rows
      .filter(Array.isArray)
      .map((row) => row.filter((b) => b && typeof b === 'object').map((b) => ({
        text: String(b.text ?? ''),
        url: String(b.url ?? ''),
        style: STYLE_VALUES.has(b.style) ? b.style : '',
      })))
      .filter((row) => row.length);
  }

  const hasSentTarget = (post) => ((post && post.targets) || []).some((t) => t.status === 'sent');
  /** Channels the post still waits for: a time or a place in a channel's queue. */
  const waitingTargets = (post) => ((post && post.targets) || []).filter((t) => t.status === 'pending' && (t.scheduled_at || t.queued));

  /**
   * Editor mode of a post. A post already out in some channel is edited in place (apply) even
   * while its other channels are still waiting in the plan (status 'scheduled').
   */
  function postMode(post) {
    const mode = composerMode(post && post.status);
    return mode === 'draft' || mode === 'scheduled' ? (hasSentTarget(post) ? 'sent' : mode) : mode;
  }

  /** Maps a post status to the editor mode. */
  function composerMode(status) {
    switch (status) {
      case 'draft':
      case 'failed':
        return 'draft';
      case 'scheduled':
        return 'scheduled';
      case 'sent':
      case 'partial':
        return 'sent';
      case undefined:
      case null:
        return 'new';
      default:
        return 'readonly'; // sending, deleted
    }
  }

  function newComposer(root, ctx, data) {
    return {
      root,
      ctx,
      channels: data.channels,
      groups: data.groups,
      postId: null,
      post: null,
      copyOf: null,
      mode: 'new',
      text: '',
      // Media items: {key, type, path, fileId, url, localUrl, index, state: 'ready'|'uploading',
      // progress, mime, name, abort, cancelled}. `index` = position in the saved post (server).
      media: [],
      mediaSeq: 0, // bumped on every media change (tells whether the server's list still matches)
      savedMediaSeq: -1,
      jobs: new Set(), // running uploads: promises resolving true (ok/cancelled) or false (failed)
      uploadFails: 0,
      replaceKey: '',
      buttons: [],
      btnAlbum: null, // album state the buttons editor was last rendered for (it locks the inputs)
      channelIds: new Set(),
      lockedIds: new Set(), // channels where the post is already out (scheduled mode)
      options: { disable_preview: false, silent: false, protect: false, caption_above: false },
      pubMode: 'now',
      scheduledAt: null,
      next: null, // {channelId: ts | null} — next queue slot per selected channel
      nextError: '',
      nextSeq: 0,
      nextTimer: 0,
      dirty: false,
      editSeq: 0,
      render: null, // last api/render result
      renderSeq: 0,
      renderTimer: 0,
      droppedChannels: 0,
      locked: false,
      busyAction: '',
    };
  }

  const activeChannels = (cmp) => cmp.channels.filter((c) => c.status === 'active');

  /** Folder groups with their active channels only (empty groups dropped). */
  function activeGroups(cmp) {
    return cmp.groups
      .map((g) => ({ folder: g.folder, channels: g.channels.filter((c) => c.status === 'active') }))
      .filter((g) => g.channels.length);
  }

  function readOptions(post) {
    const src = post && post.options && typeof post.options === 'object' ? post.options : post || {};
    return {
      disable_preview: Boolean(src.disable_preview),
      silent: Boolean(src.silent),
      protect: Boolean(src.protect),
      caption_above: Boolean(src.caption_above),
    };
  }

  let mediaKeySeq = 0;
  const newMediaKey = () => `m${++mediaKeySeq}`;

  function mediaFromPost(post) {
    return postMedia(post).map((m) => ({
      key: newMediaKey(),
      type: m.type,
      path: typeof m.path === 'string' && m.path ? m.path : null,
      fileId: typeof m.file_id === 'string' && m.file_id ? m.file_id : null,
      url: safeSrc(m.url) || (post.id ? `api/posts/${num(post.id)}/media/${num(m.index)}` : ''),
      localUrl: '',
      index: m.index,
      state: 'ready',
      mime: m.mime || '',
    }));
  }

  /** Channel selection from the post's targets (only active channels can be picked). */
  function applyTargets(cmp, post) {
    const targets = (post && post.targets) || [];
    const ids = targets.map((t) => num(t.channel_id));
    if (cmp.mode === 'draft' || cmp.mode === 'scheduled') {
      cmp.lockedIds = new Set(targets.filter((t) => t.status === 'sent' || t.status === 'deleted').map((t) => num(t.channel_id)));
      const activeIds = new Set(activeChannels(cmp).map((c) => num(c.id)));
      const open = ids.filter((id) => !cmp.lockedIds.has(id));
      const kept = open.filter((id) => activeIds.has(id));
      cmp.droppedChannels = open.length - kept.length;
      cmp.channelIds = new Set(kept);
    } else {
      cmp.lockedIds = new Set();
      cmp.droppedChannels = 0;
      cmp.channelIds = new Set(ids);
    }
  }

  function loadPostIntoComposer(cmp, post) {
    cmp.postId = num(post.id);
    cmp.post = post;
    cmp.copyOf = null;
    cmp.mode = postMode(post);
    cmp.text = post.text_md || '';
    cmp.buttons = normalizeRows(post.buttons);
    cmp.options = readOptions(post);
    cmp.media = mediaFromPost(post);
    cmp.mediaSeq += 1;
    cmp.scheduledAt = post.scheduled_at || null;
    cmp.pubMode = cmp.mode === 'scheduled' ? 'keep' : 'now';
    cmp.next = null;
    applyTargets(cmp, post);
    cmp.dirty = false;
  }

  /** Duplicate: the media are sent back by reference with `copy_from` — nothing is re-uploaded. */
  function copyPostIntoComposer(cmp, post) {
    cmp.copyOf = num(post.id);
    cmp.text = post.text_md || '';
    cmp.buttons = normalizeRows(post.buttons);
    cmp.options = readOptions(post);
    cmp.media = mediaFromPost(post);
    const activeIds = new Set(activeChannels(cmp).map((c) => num(c.id)));
    cmp.channelIds = new Set((post.targets || []).map((t) => num(t.channel_id)).filter((id) => activeIds.has(id)));
    cmp.dirty = true;
  }

  async function pageCompose(root, r, ctx) {
    const rawId = r.segs[1];
    const id = rawId ? Number(rawId) : null;
    if (rawId && !Number.isInteger(id)) throw new ApiError('Неверный адрес поста', 404);
    const copyId = !rawId && r.query.get('copy') ? num(r.query.get('copy')) : null;
    setPageTitle(id ? `Пост #${id}` : 'Новый пост');

    const sourceId = id || copyId;
    const [data, source] = await Promise.all([
      loadGrouped(),
      sourceId ? api.get(`api/posts/${sourceId}`) : Promise.resolve(null),
      ensureServerTz(),
    ]);
    if (!ctx.alive()) return;

    const cmp = newComposer(root, ctx, data);
    const post = source ? unwrap(source, 'post') : null;
    let notice = '';
    if (id && post) {
      loadPostIntoComposer(cmp, post);
    } else if (copyId && post) {
      copyPostIntoComposer(cmp, post);
    } else {
      // Prefill from the content plan: #/compose?channel=<id>&at=<ts> or &mode=queue.
      const active = activeChannels(cmp);
      const wanted = num(r.query.get('channel'));
      if (wanted && active.some((c) => num(c.id) === wanted)) cmp.channelIds.add(wanted);
      else if (active.length === 1) cmp.channelIds.add(num(active[0].id));
      const at = num(r.query.get('at'));
      if (at) {
        cmp.pubMode = 'at';
        if (at > nowTs() + 60) cmp.scheduledAt = at;
        else notice = 'Время этого слота уже прошло — выберите другое';
      } else if (r.query.get('mode') === 'queue') {
        cmp.pubMode = 'queue';
      }
    }
    state.composer = cmp;
    bindComposer(cmp);
    renderComposer(cmp);
    if (cmp.pubMode === 'queue') refreshNext(cmp);
    if (cmp.droppedChannels) toast('Некоторые каналы поста больше недоступны и убраны из выбора', 'info');
    if (notice) toast(notice, 'info');
  }

  function tzHint() {
    let hint = `Время — по часовому поясу бота${state.serverTz ? ` (${state.serverTz})` : ''}, как в расписании каналов.`;
    if (tzDiffers()) hint += ` У вас сейчас ${BROWSER_TZ} — часы на устройстве могут показывать другое время.`;
    return hint;
  }

  // ---------------------------------------------------------------- compose: rendering

  function cheatsheetHtml() {
    const rows = [
      ['**жирный**', '<b>жирный</b>'],
      ['*курсив* или _курсив_', '<i>курсив</i>'],
      ['__подчёркнутый__', '<u>подчёркнутый</u>'],
      ['~~зачёркнутый~~', '<s>зачёркнутый</s>'],
      ['||спойлер||', '<span class="cheat-spoiler">спойлер</span>'],
      ['`код`', '<code>код</code>'],
      ['```python … ```', 'блок кода с подсветкой языка'],
      ['[текст](https://…)', '<a>ссылка</a>'],
      ['![😀](tg://emoji?id=…)', 'премиум-эмодзи<span class="cheat-note">в канале — только если у бота есть юзернейм с Fragment (Настройки → Премиум-эмодзи), иначе станет обычным 😀</span>'],
      ['> цитата', 'цитата'],
      ['>> цитата', 'сворачиваемая цитата'],
      ['# Заголовок', '<b>Заголовок</b>'],
      ['- пункт', '• пункт'],
      ['\\*', 'символ без форматирования'],
    ];
    return `<table class="cheat"><tbody>${rows.map(([md, out]) => `<tr><td><code>${esc(md)}</code></td><td>${out}</td></tr>`).join('')}</tbody></table>
      <p class="hint">Горячие клавиши: Ctrl+B, Ctrl+I, Ctrl+U, Ctrl+K — ссылка, Ctrl+S — сохранить. Картинку можно вставить из буфера обмена.</p>`;
  }

  function toolbarHtml() {
    return TOOLBAR.map((t) => {
      if (t.sep) return '<span class="tb-sep" aria-hidden="true"></span>';
      const content = t.iconName ? icon(t.iconName) : `<span class="${t.cls}">${t.label}</span>`;
      return `<button type="button" class="tb-btn" data-action="fmt" data-fmt="${t.fmt}" title="${esc(t.title)}" aria-label="${esc(t.title)}">${content}</button>`;
    }).join('');
  }

  /** A new post: ready posts are imported by forwarding them to the bot (the panel cannot do it). */
  function forwardHintHtml() {
    const bot = state.botUsername
      ? `боту <a class="callout-link" href="https://t.me/${esc(encodeURIComponent(state.botUsername))}" target="_blank" rel="noopener noreferrer">@${esc(state.botUsername)}</a>`
      : 'боту в Telegram';
    return `<div class="callout" id="cmp-forward-hint">${icon('info')}<div><b>Есть готовый пост?</b> Перешлите его ${bot} — из канала или от кого угодно. Бот перенесёт текст с форматированием и эмодзи, фото/видео и кнопки-ссылки с цветами и создаст черновик — он появится здесь, в «Постах».</div></div>`;
  }

  function renderComposer(cmp) {
    const root = cmp.root;
    const ro = cmp.mode === 'readonly';
    // Keep the caret when the editor is rebuilt around text the user is typing.
    const active = document.activeElement;
    const caret = active && active.id === 'cmp-text' ? [active.selectionStart, active.selectionEnd] : null;
    root.innerHTML = `
      <div id="cmp-head"></div>
      <div class="composer${ro ? ' is-readonly' : ''}">
        <div class="cmp-editor">
          ${!cmp.postId && !ro ? forwardHintHtml() : ''}
          <section class="card" aria-labelledby="cmp-text-label">
            <div class="card-head"><h2 id="cmp-text-label">Текст</h2><span class="counter" id="cmp-counter" aria-live="polite"></span></div>
            <div class="md-toolbar" role="toolbar" aria-label="Форматирование текста">${toolbarHtml()}</div>
            <textarea id="cmp-text" class="input md-input" rows="9" aria-labelledby="cmp-text-label"
              placeholder="Текст поста. Поддерживается Markdown: **жирный**, *курсив*, [ссылка](https://…)"></textarea>
            <p class="field-error" id="cmp-render-error" role="status" hidden></p>
            <div class="cmp-premium-warn" id="cmp-premium-warn" aria-live="polite"></div>
            <details class="cheatsheet"><summary>Шпаргалка по разметке</summary>${cheatsheetHtml()}</details>
          </section>

          <section class="card" id="cmp-media-card" aria-labelledby="cmp-media-label">
            <div class="card-head"><h2 id="cmp-media-label">Медиа</h2><span class="muted small" id="cmp-media-sum"></span></div>
            <div id="cmp-media" tabindex="-1"></div>
            <input type="file" id="cmp-file" accept="${MEDIA_ACCEPT}" multiple hidden>
          </section>

          <section class="card" id="cmp-btns-card" aria-labelledby="cmp-btns-label">
            <div class="card-head"><h2 id="cmp-btns-label">Кнопки</h2><span class="muted small">до ${MAX_ROWS} рядов, до ${MAX_PER_ROW} в ряду</span></div>
            <div id="cmp-buttons" tabindex="-1"></div>
          </section>
        </div>

        <aside class="card cmp-preview" aria-labelledby="cmp-preview-label">
          <div class="card-head"><h2 id="cmp-preview-label">Предпросмотр</h2><span class="preview-label">${icon('eye')}<span>Так пост увидят подписчики</span></span></div>
          <div class="tg-window">
            <div class="tg-header" id="tg-header"></div>
            <div class="tg-chat">
              <div class="tg-msg">
                <div class="tg-bubble" id="tg-bubble">
                  <div class="tg-media" id="tg-media" hidden></div>
                  <div class="tg-text" id="tg-text"></div>
                  <div class="tg-meta">${icon('eye', 'tg-eye')}<time id="tg-time"></time></div>
                </div>
                <div class="tg-kb" id="tg-kb" hidden></div>
              </div>
            </div>
          </div>
          <p class="hint preview-note">Цвет кнопок виден в актуальных версиях Telegram; в старых кнопки будут обычными.</p>
        </aside>

        <section class="card cmp-publish" id="cmp-publish" aria-label="Публикация"></section>
      </div>`;

    const ta = $('#cmp-text', root);
    ta.value = cmp.text; // set via .value: a leading newline would be lost in innerHTML
    ta.readOnly = ro;
    $$('.tb-btn', root).forEach((b) => { b.disabled = ro; });
    autosize(ta);
    if (caret && !ro) {
      ta.focus({ preventScroll: true });
      ta.setSelectionRange(caret[0], caret[1]);
    }

    renderHead(cmp);
    renderMedia(cmp);
    renderButtonsEditor(cmp);
    renderPublish(cmp);
    renderPreviewHeader(cmp);
    renderPreviewButtons(cmp);
    renderPreviewMedia(cmp);
    updatePreviewTime(cmp);
    requestRender(cmp, 0);
  }

  function renderHead(cmp) {
    const el = $('#cmp-head', cmp.root);
    const p = cmp.post;
    let title = 'Новый пост';
    let sub = 'Пост ещё не сохранён';
    if (cmp.postId && p) {
      title = `Пост #${cmp.postId}`;
      if (cmp.mode === 'draft') {
        sub = p.status === 'failed'
          ? `Публикация не удалась${p.error ? `: ${p.error}` : ''}. Исправьте и попробуйте снова.`
          : `Создан ${fmtWhen(p.created_at)}`;
      } else if (cmp.mode === 'scheduled') {
        const times = pendingTimes(p);
        sub = new Set(times).size > 1
          ? `В контент-плане: ближайшая публикация ${fmtWhen(times[0])}, каналы выходят в своё время`
          : `Будет опубликован ${fmtWhen(times[0] || p.scheduled_at)}`;
      } else if (cmp.mode === 'sent') {
        const planned = pendingTimes(p);
        let state0 = p.status === 'partial' ? 'Опубликован не во все каналы' : 'Опубликован';
        if (p.status === 'scheduled') state0 = `Опубликован в части каналов, в остальных — ${planned.length ? fmtWhen(planned[0]) : 'по плану'}`;
        sub = `${state0}${p.sent_at && p.status !== 'scheduled' ? ` ${fmtWhen(p.sent_at)}` : ''}. Изменения применяются кнопкой «Сохранить изменения в каналах».`;
      } else {
        sub = p.status === 'sending'
          ? 'Пост сейчас публикуется — редактирование недоступно.'
          : 'Пост удалён из каналов. Создайте копию, чтобы опубликовать его снова.';
      }
    } else if (cmp.copyOf) {
      sub = `Копия поста #${cmp.copyOf} — ещё не сохранена`;
    }

    const actions = [];
    if (p && (cmp.mode === 'scheduled' || cmp.mode === 'sent')) {
      const t = (p.targets || []).find((x) => x.status === 'pending' && x.scheduled_at);
      if (t) actions.push(`<a class="btn btn-secondary btn-sm" href="#/plan/${num(t.channel_id)}">${icon('calendar')}<span>В плане</span></a>`);
    }
    if (cmp.postId) actions.push(`<a class="btn btn-secondary btn-sm" href="#/compose?copy=${num(cmp.postId)}">${icon('copy')}<span>Дублировать</span></a>`);
    if (cmp.postId && p && ['draft', 'scheduled', 'failed', 'deleted'].includes(p.status) && !hasSentTarget(p)) {
      actions.push(`<button type="button" class="btn btn-ghost btn-sm is-danger" data-action="delete-post">${icon('trash')}<span>Удалить</span></button>`);
    }
    // Title and actions go to the topbar; the status line (badge, «не сохранено», details such as
    // the publishing error) stays on the page, where it has room on phones too.
    setHead({ title, actions: actions.join('') });
    const failed = cmp.mode === 'draft' && p && p.status === 'failed';
    el.className = `cmp-status${failed ? ' is-failed' : ''}`;
    el.innerHTML = `${p ? statusBadge(p.status) : ''}<span class="dirty-flag" id="cmp-dirty"${cmp.dirty ? '' : ' hidden'}>не сохранено</span>
      <span class="cmp-status-text">${failed ? icon('alert') : ''}<span>${esc(sub)}</span></span>`;
    syncBusy(cmp);
  }

  // ---------------------------------------------------------------- compose: media gallery

  const canToggleGif = (m) => m.state === 'ready' && Boolean(m.path) && /\.(mp4|mov)$/i.test(m.path);

  function mediaTileHtml(cmp, m, i, total) {
    const ro = cmp.mode === 'readonly';
    const sent = cmp.mode === 'sent';
    const loading = m.state === 'uploading';
    const n = i + 1;
    const tools = [];
    if (!ro && total > 1) {
      tools.push(`<button type="button" class="icon-btn sm" data-action="media-left" aria-label="Переместить файл ${n} влево"${i === 0 ? ' disabled' : ''}>${icon('left')}</button>`);
      tools.push(`<button type="button" class="icon-btn sm" data-action="media-right" aria-label="Переместить файл ${n} вправо"${i === total - 1 ? ' disabled' : ''}>${icon('right')}</button>`);
    }
    if (!ro && sent && !loading) {
      tools.push(`<button type="button" class="btn btn-ghost btn-sm" data-action="media-replace" aria-label="Заменить файл ${n}">${icon('refresh')}<span>Заменить</span></button>`);
    }
    if (!ro && !sent) {
      const label = loading ? `Отменить загрузку файла ${n}` : `Убрать файл ${n}`;
      tools.push(`<button type="button" class="icon-btn sm is-danger" data-action="media-remove" aria-label="${label}" title="${loading ? 'Отменить' : 'Убрать'}">${icon(loading ? 'x' : 'trash')}</button>`);
    }
    const gif = !ro && total === 1 && canToggleGif(m)
      ? `<label class="media-gif" title="GIF в Telegram — зацикленное видео без звука"><input type="checkbox" data-media-gif${m.type === 'animation' ? ' checked' : ''}><span>как GIF</span></label>`
      : '';
    const pct = Math.round((m.progress || 0) * 100);
    const progress = loading
      ? `<span class="media-progress" role="progressbar" aria-label="Загрузка файла ${n}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${pct}%"></span></span>`
      : '';
    return `<li class="media-tile${loading ? ' is-loading' : ''}" data-key="${esc(m.key)}">
      <div class="media-frame">
        ${mediaElHtml(m)}
        <span class="media-badge mb-${esc(m.type)}">${esc(MEDIA_LABELS[m.type] || 'Фото')}</span>
        ${total > 1 ? `<span class="media-num" aria-hidden="true">${n}</span>` : ''}
        ${loading ? '<span class="spinner" aria-hidden="true"></span>' : ''}
        ${progress}
      </div>
      ${tools.length || gif ? `<div class="media-tools">${tools.join('')}${gif}</div>` : ''}
    </li>`;
  }

  function renderMedia(cmp) {
    const box = $('#cmp-media', cmp.root);
    if (!box) return;
    const ro = cmp.mode === 'readonly';
    const sent = cmp.mode === 'sent';
    const items = cmp.media;
    $('#cmp-media-sum', cmp.root).textContent = items.length ? describeMedia(items) : 'необязательно';

    // Re-rendering must not throw keyboard focus out of the gallery.
    const focused = document.activeElement;
    const keep = focused && box.contains(focused)
      ? { key: focused.closest('[data-key]') && focused.closest('[data-key]').dataset.key, action: focused.dataset.action }
      : null;

    if (!items.length) {
      box.innerHTML = sent || ro
        ? `<p class="hint">${sent ? 'Пост опубликован без медиа — добавить его уже нельзя (ограничение Telegram).' : 'Без медиа.'}</p>`
        : `<div class="dropzone" id="cmp-drop" role="button" tabindex="0" data-action="media-pick"
            aria-label="Добавить фото, видео или GIF: перетащите файлы или нажмите, чтобы выбрать">
            <span class="dropzone-icon">${icon('image')}</span>
            <span><b>Перетащите фото, видео или GIF</b> или <span class="link-like">выберите файлы</span></span>
            <span class="muted small">Фото JPEG, PNG, WebP до 10 МБ · видео MP4, MOV и GIF до 50 МБ. До ${MAX_ALBUM} файлов — получится альбом. Картинку можно вставить из буфера (Ctrl+V).</span>
          </div>`;
      if (keep) $('#cmp-drop', box)?.focus();
      return;
    }
    const single = items.length === 1;
    const canAdd = !ro && !sent && items.length < MAX_ALBUM && !(single && items[0].type === 'animation');
    const add = canAdd
      ? `<li class="media-tile media-add-tile"><button type="button" class="media-add" data-action="media-pick" aria-label="Добавить файлы (ещё до ${MAX_ALBUM - items.length})">${icon('plus')}<span>Добавить</span><span class="muted small">ещё до ${MAX_ALBUM - items.length}</span></button></li>`
      : '';
    let note = '';
    if (sent) {
      note = single
        ? 'У опубликованного поста медиа можно заменить (на фото, видео или GIF), но не убрать.'
        : 'У опубликованного альбома файлы можно заменить и поменять местами, но не добавить или убрать.';
    } else if (single && items[0].type === 'animation') {
      note = 'GIF публикуется отдельным сообщением — в альбом его добавить нельзя.';
    } else if (!single) {
      note = 'Несколько файлов публикуются альбомом: подпись — у альбома, кнопки недоступны (ограничение Telegram).';
    }
    box.innerHTML = `<ul class="media-grid" aria-label="Файлы поста">${items.map((m, i) => mediaTileHtml(cmp, m, i, items.length)).join('')}${add}</ul>
      ${note ? `<p class="hint">${esc(note)}</p>` : ''}`;

    if (keep) {
      const tile = keep.key && $(`[data-key="${CSS.escape(keep.key)}"]`, box);
      let target = tile && keep.action ? $(`[data-action="${keep.action}"]:not([disabled])`, tile) : null;
      if (!target && tile) target = $('button:not([disabled]), input', tile);
      if (!target) target = $('[data-action="media-pick"]', box) || $('.media-tile button:not([disabled])', box);
      if (target) target.focus();
      else box.focus();
    }
  }

  function updateProgress(cmp, item) {
    const tile = $(`#cmp-media [data-key="${CSS.escape(item.key)}"]`, cmp.root);
    const bar = tile && $('.media-progress', tile);
    if (!bar) return;
    const pct = Math.round((item.progress || 0) * 100);
    bar.setAttribute('aria-valuenow', String(pct));
    bar.firstElementChild.style.width = `${pct}%`;
  }

  /** Everything that depends on the media list: gallery, preview, buttons lock, caption limit. */
  function mediaChanged(cmp) {
    cmp.mediaSeq += 1;
    if (!cmp.ctx.alive() || state.composer !== cmp) return;
    renderMedia(cmp);
    renderPreviewMedia(cmp);
    // The buttons editor depends on the media only through the album lock: rebuild it when that
    // flips, never when an upload starts or finishes — the owner may be typing in a button field.
    if ((cmp.media.length > 1) !== cmp.btnAlbum) renderButtonsEditor(cmp);
    renderPreviewButtons(cmp);
    requestRender(cmp, 0);
  }

  function fileMime(file) {
    if (FILE_KINDS[file.type]) return file.type;
    const ext = (/\.([a-z0-9]+)$/i.exec(file.name || '') || [])[1];
    return ext ? EXT_MIME[ext.toLowerCase()] || '' : '';
  }

  function checkFile(file) {
    const name = file.name || 'файл';
    const mime = fileMime(file);
    const type = FILE_KINDS[mime];
    if (!type) return { error: `«${name}»: поддерживаются фото JPEG, PNG, WebP, видео MP4, MOV и GIF` };
    const limit = type === 'photo' ? PHOTO_MAX_BYTES : VIDEO_MAX_BYTES;
    if (file.size > limit) {
      return { error: `«${name}» слишком большой: ${fmtSize(file.size)} (максимум ${type === 'photo' ? 10 : 50} МБ)` };
    }
    return { file, type, mime };
  }

  function toastErrors(errors) {
    Array.from(new Set(errors)).slice(0, 3).forEach((e) => toast(e, 'error'));
  }

  /** Adds picked / dropped / pasted files to the gallery (or replaces `replaceKey`) and uploads them. */
  function addFiles(cmp, fileList, { replaceKey = '' } = {}) {
    if (cmp.mode === 'readonly') return;
    const errors = [];
    let list = [];
    Array.from(fileList || []).forEach((f) => {
      const c = checkFile(f);
      if (c.error) errors.push(c.error);
      else list.push(c);
    });
    const sent = cmp.mode === 'sent';
    let replaceIndex = replaceKey ? cmp.media.findIndex((m) => m.key === replaceKey) : -1;
    if (!replaceKey && sent) {
      // A published post keeps its media count: a single file may only be replaced.
      if (!cmp.media.length) errors.push('К опубликованному посту без медиа его добавить нельзя (ограничение Telegram)');
      else if (cmp.media.length > 1) errors.push('В опубликованный альбом нельзя добавить файлы — только заменить существующие');
      else replaceIndex = 0;
      if (replaceIndex < 0) list = [];
    }
    if (replaceIndex >= 0) {
      const pick = list[0];
      if (pick && cmp.media.length > 1 && pick.type === 'animation') errors.push(ALBUM_GIF_ERROR);
      else if (pick) startUpload(cmp, pick, replaceIndex);
      toastErrors(errors);
      return;
    }
    if (replaceKey) return; // the item to replace is gone
    if (list.length && cmp.media.some((m) => m.type === 'animation')) {
      errors.push(ALBUM_GIF_ERROR);
      list = [];
    } else if (list.some((x) => x.type === 'animation') && cmp.media.length + list.length > 1) {
      errors.push(ALBUM_GIF_ERROR);
      list = list.filter((x) => x.type !== 'animation');
    }
    const room = MAX_ALBUM - cmp.media.length;
    if (list.length > room) {
      errors.push(`В посте может быть не больше ${MAX_ALBUM} фото и видео — лишние файлы пропущены`);
      list = list.slice(0, Math.max(0, room));
    }
    list.forEach((x) => startUpload(cmp, x, -1));
    toastErrors(errors);
  }

  function startUpload(cmp, picked, replaceIndex) {
    const localUrl = URL.createObjectURL(picked.file);
    const item = {
      key: newMediaKey(),
      type: picked.type,
      path: null,
      fileId: null,
      url: '',
      localUrl,
      index: null,
      state: 'uploading',
      progress: 0,
      mime: picked.mime,
      name: picked.file.name || '',
      abort: null,
      cancelled: false,
    };
    const previous = replaceIndex >= 0 ? cmp.media[replaceIndex] : null;
    if (previous) cmp.media.splice(replaceIndex, 1, item);
    else cmp.media.push(item);
    markDirty(cmp);
    mediaChanged(cmp);

    const up = uploadWithProgress(picked.file, (p) => {
      item.progress = p;
      updateProgress(cmp, item);
    });
    item.abort = up.abort;
    const job = up.promise
      .then((res) => {
        if (!res || !res.path) throw new ApiError('Сервер не вернул имя файла');
        item.path = String(res.path);
        item.type = mediaType(res.type || item.type);
        item.url = safeSrc(res.url) || '';
        if (res.mime) item.mime = String(res.mime);
        item.state = 'ready';
        item.abort = null;
        if (item.type === 'animation' && cmp.media.length > 1 && cmp.media.includes(item)) {
          throw new ApiError(ALBUM_GIF_ERROR); // the server saw a GIF where the browser did not
        }
        markDirty(cmp);
        return true;
      })
      .catch((err) => {
        const idx = cmp.media.indexOf(item);
        if (idx >= 0) {
          if (previous) cmp.media.splice(idx, 1, previous);
          else cmp.media.splice(idx, 1);
        }
        URL.revokeObjectURL(localUrl);
        if (item.cancelled) return true; // removed by the user: not a failure
        cmp.uploadFails += 1;
        if (!err.silent) toast(`Не удалось загрузить${item.name ? ` «${item.name}»` : ' файл'}: ${err.message}`, 'error');
        return false;
      })
      .finally(() => {
        cmp.jobs.delete(job);
        mediaChanged(cmp);
      });
    cmp.jobs.add(job);
  }

  /** Waits for running uploads (including ones started meanwhile); false if any of them failed. */
  async function settleUploads(cmp) {
    let ok = true;
    while (cmp.jobs.size) {
      const results = await Promise.all(Array.from(cmp.jobs));
      if (results.some((x) => !x)) ok = false;
    }
    return ok;
  }

  /**
   * Before a publish-type action: wait for running uploads (spinner on the button) and refuse to go
   * on when an upload failed after the click — otherwise the post would go out without that file.
   */
  async function ensureUploads(cmp, btn, failSeq) {
    if (cmp.jobs.size) await busy(btn, () => settleUploads(cmp));
    if (cmp.uploadFails !== failSeq) throw new ValidationError(UPLOAD_FAILED, '#cmp-media');
  }

  function moveMedia(cmp, key, delta) {
    const i = cmp.media.findIndex((m) => m.key === key);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= cmp.media.length) return;
    [cmp.media[i], cmp.media[j]] = [cmp.media[j], cmp.media[i]];
    markDirty(cmp);
    mediaChanged(cmp);
  }

  function removeMedia(cmp, key) {
    const i = cmp.media.findIndex((m) => m.key === key);
    if (i < 0) return;
    const [item] = cmp.media.splice(i, 1);
    if (item.state === 'uploading' && item.abort) {
      item.cancelled = true;
      item.abort();
    } else if (item.localUrl) {
      URL.revokeObjectURL(item.localUrl);
    }
    markDirty(cmp);
    mediaChanged(cmp);
  }

  // ---------------------------------------------------------------- compose: buttons editor

  const hasButtons = (cmp) => cmp.buttons.some((row) => row.some((b) => b.text.trim() || b.url.trim()));

  function buttonItemHtml(b, r, c, ro) {
    const swatches = BUTTON_STYLES.map((s) => `<button type="button" class="swatch ${s.cls}" data-action="btn-style" data-style="${s.value}"
        aria-pressed="${(b.style || '') === s.value}" aria-label="${s.label}" title="${s.label}"${ro ? ' disabled' : ''}></button>`).join('');
    return `<div class="btn-item" data-row="${r}" data-col="${c}">
      <div class="btn-item-fields">
        <input class="input input-sm" data-field="text" value="${esc(b.text)}" maxlength="64" placeholder="Текст кнопки"
          aria-label="Текст кнопки ${c + 1} в ряду ${r + 1}"${ro ? ' disabled' : ''}>
        <input class="input input-sm" data-field="url" value="${esc(b.url)}" placeholder="https://… или @канал" inputmode="url"
          autocapitalize="off" autocomplete="off" spellcheck="false" aria-label="Ссылка кнопки ${c + 1} в ряду ${r + 1}"${ro ? ' disabled' : ''}>
      </div>
      <div class="btn-item-tools">
        <div class="swatches" role="group" aria-label="Цвет кнопки">${swatches}</div>
        ${ro ? '' : `<button type="button" class="icon-btn sm is-danger" data-action="btn-del" aria-label="Удалить кнопку ${c + 1} в ряду ${r + 1}">${icon('x')}</button>`}
      </div>
    </div>`;
  }

  function buttonRowHtml(row, r, total, ro) {
    const tools = ro ? '' : `<div class="btn-row-tools">
        <button type="button" class="icon-btn sm" data-action="row-up" data-row="${r}" aria-label="Поднять ряд ${r + 1}"${r === 0 ? ' disabled' : ''}>${icon('up')}</button>
        <button type="button" class="icon-btn sm" data-action="row-down" data-row="${r}" aria-label="Опустить ряд ${r + 1}"${r === total - 1 ? ' disabled' : ''}>${icon('down')}</button>
        <button type="button" class="icon-btn sm is-danger" data-action="row-del" data-row="${r}" aria-label="Удалить ряд ${r + 1}">${icon('trash')}</button>
      </div>`;
    const add = ro || row.length >= MAX_PER_ROW ? '' : `<button type="button" class="btn btn-ghost btn-sm btn-add" data-action="btn-add" data-row="${r}">${icon('plus')}<span>Кнопка</span></button>`;
    return `<div class="btn-row" data-row="${r}">
      <div class="btn-row-head"><span>Ряд ${r + 1}</span>${tools}</div>
      <div class="btn-row-items">${row.map((b, c) => buttonItemHtml(b, r, c, ro)).join('')}${add}</div>
    </div>`;
  }

  function renderButtonsEditor(cmp) {
    const box = $('#cmp-buttons', cmp.root);
    if (!box) return;
    const album = cmp.media.length > 1;
    cmp.btnAlbum = album;
    const ro = cmp.mode === 'readonly' || album;
    const rows = cmp.buttons;
    if (album) {
      const filled = hasButtons(cmp);
      box.innerHTML = `<div class="callout is-warn">${icon('info')}<div><b>Telegram не поддерживает кнопки у альбомов.</b>
          Оставьте одно фото или видео, чтобы добавить кнопки.${filled ? ' Сейчас у поста есть кнопки — уберите их, иначе пост не сохранится.' : ''}</div></div>
        ${rows.length ? `<div class="btn-rows">${rows.map((row, r) => buttonRowHtml(row, r, rows.length, true)).join('')}</div>` : ''}
        ${rows.length && cmp.mode !== 'readonly' ? `<button type="button" class="btn btn-secondary btn-sm" data-action="btns-clear">${icon('trash')}<span>Убрать кнопки</span></button>` : ''}`;
      return;
    }
    const intro = rows.length ? '' : '<p class="hint">Кнопки-ссылки под постом: «Купить», «Подписаться», «Сайт». Цвет — синий, зелёный, красный или обычный.</p>';
    const addRow = ro ? '' : `<button type="button" class="btn btn-secondary btn-sm" data-action="row-add"${rows.length >= MAX_ROWS ? ' disabled' : ''}>${icon('plus')}<span>Добавить ряд</span></button>`;
    box.innerHTML = `${intro}<div class="btn-rows">${rows.map((row, r) => buttonRowHtml(row, r, rows.length, ro)).join('')}</div>${addRow}`;
  }

  // ---------------------------------------------------------------- compose: channels & publishing

  function chipHtml(cmp, c) {
    const id = num(c.id);
    const locked = cmp.lockedIds.has(id);
    const on = locked || cmp.channelIds.has(id);
    return `<label class="chip${on ? ' on' : ''}${locked ? ' is-locked' : ''}"${locked ? ' title="Пост уже опубликован в этом канале"' : ''}>
      <input type="checkbox" data-channel="${id}"${on ? ' checked' : ''}${locked ? ' disabled' : ''}>
      ${avatarHtml(c.id, c.title, 'xs')}<span class="chip-text">${esc(c.title || 'Без названия')}</span>
      ${locked ? '<span class="sr-only"> — уже опубликован</span>' : ''}
      <span class="chip-check">${icon('check')}</span>
    </label>`;
  }

  function channelPickerHtml(cmp) {
    const active = activeChannels(cmp);
    if (!active.length) {
      return `<div class="callout">${icon('info')}<div>Нет подключённых каналов. <a href="#/channels">Добавьте канал</a>, чтобы публиковать посты. Сохранить черновик можно и без канала.</div></div>`;
    }
    const groups = activeGroups(cmp);
    let body;
    if (!groups.some((g) => g.folder)) {
      body = `<div class="chips" role="group" aria-labelledby="cmp-ch-title">${active.map((c) => chipHtml(cmp, c)).join('')}</div>`;
    } else {
      body = groups.map((g) => {
        const name = g.folder ? g.folder.name : 'Без папки';
        return `<div class="chip-group" role="group" aria-label="${esc(name)}" data-group="${g.folder ? num(g.folder.id) : 0}">
          <label class="chip-folder">
            <input type="checkbox" data-folder-toggle>
            ${icon('folder')}<span class="chip-folder-name">${esc(name)}</span><span class="count" data-folder-count></span>
            <span class="sr-only"> — выбрать все каналы папки</span>
          </label>
          <div class="chips">${g.channels.map((c) => chipHtml(cmp, c)).join('')}</div>
        </div>`;
      }).join('');
    }
    const tools = active.length > 1
      ? '<div class="chips-tools"><button type="button" class="btn btn-ghost btn-sm" data-action="ch-all">Выбрать все</button><button type="button" class="btn btn-ghost btn-sm" data-action="ch-none">Снять все</button></div>'
      : '';
    return `${body}${tools}`;
  }

  /** Syncs chip highlight and the folder toggles (checked / indeterminate / k of n) with the selection. */
  function updatePicker(cmp) {
    const box = $('#cmp-publish', cmp.root);
    if (!box) return;
    $$('input[data-channel]', box).forEach((input) => {
      if (input.disabled) return;
      input.checked = cmp.channelIds.has(num(input.dataset.channel));
      input.closest('.chip').classList.toggle('on', input.checked);
    });
    $$('.chip-group', box).forEach((group) => {
      const inputs = $$('input[data-channel]:not([disabled])', group);
      const k = inputs.filter((i) => i.checked).length;
      const toggle = $('[data-folder-toggle]', group);
      toggle.disabled = !inputs.length;
      toggle.checked = inputs.length > 0 && k === inputs.length;
      toggle.indeterminate = k > 0 && k < inputs.length;
      $('[data-folder-count]', group).textContent = `${k}/${inputs.length}`;
    });
  }

  function selectedChannels(cmp) {
    return activeChannels(cmp).filter((c) => cmp.channelIds.has(num(c.id)));
  }

  function targetsListHtml(post) {
    const targets = (post && post.targets) || [];
    if (!targets.length) return '<p class="hint">Каналы не выбраны.</p>';
    return `<ul class="target-list">${targets.map((t) => {
      const href = safeHref(t.link);
      const when = t.status === 'pending' && t.scheduled_at
        ? `в плане: ${fmtSlotWhen(t.scheduled_at)}`
        : t.status === 'sent' && t.sent_at ? fmtWhen(t.sent_at) : '';
      return `<li class="target-item">
        ${avatarHtml(t.channel_id, targetTitle(t), 'xs')}
        <span class="target-title">${esc(targetTitle(t))}</span>
        ${when ? `<span class="muted small">${esc(when)}</span>` : ''}
        ${badge(TARGET_STATUS, t.status)}
        ${href ? `<a class="target-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">Открыть${icon('external')}</a>` : ''}
        ${t.error ? `<p class="target-error">${esc(t.error)}</p>` : ''}
      </li>`;
    }).join('')}</ul>`;
  }

  /** Earliest time the post has in the plan (its kept channels), or 0. */
  function earliestPlanned(cmp) {
    const times = pendingTimes(cmp.post || {});
    return times[0] || num(cmp.post && cmp.post.scheduled_at);
  }

  /** The time a selected channel already has in the plan (scheduled mode), or 0. */
  function plannedTime(cmp, channelId) {
    const t = ((cmp.post && cmp.post.targets) || []).find((x) => num(x.channel_id) === num(channelId));
    return t && t.status === 'pending' && t.scheduled_at ? num(t.scheduled_at) : 0;
  }

  function slotRowHtml(c, right) {
    return `<li class="slot-row">${avatarHtml(c.id, c.title, 'xs')}<span class="slot-ch">${esc(c.title || 'Без названия')}</span><span class="slot-val">${right}</span></li>`;
  }

  function modeDetailHtml(cmp) {
    const chosen = selectedChannels(cmp);
    switch (cmp.pubMode) {
      case 'keep': {
        if (!chosen.length) return '<p class="hint">Каналы не выбраны.</p>';
        const first = earliestPlanned(cmp);
        const rows = chosen.map((c) => {
          const ts = plannedTime(cmp, c.id);
          const target = ((cmp.post && cmp.post.targets) || []).find((x) => num(x.channel_id) === num(c.id));
          if (ts) {
            return slotRowHtml(c, `<span class="slot-time">${esc(fmtSlotWhen(ts))}</span>${target && target.queued ? '<span class="badge b-accent">по расписанию</span>' : ''}`);
          }
          return slotRowHtml(c, first
            ? `<span class="slot-time">${esc(fmtSlotWhen(Math.max(first, nowTs() + 60)))}</span><span class="badge">новый канал</span>`
            : '<span class="slot-none">время не задано</span>');
        }).join('');
        const added = chosen.some((c) => !plannedTime(cmp, c.id));
        return `<ul class="slot-list">${rows}</ul>${added ? '<p class="hint">Новые каналы получат время первой публикации поста. Чтобы поставить их в очередь по расписанию, выберите «В очередь по расписанию».</p>' : ''}`;
      }
      case 'queue': {
        if (!chosen.length) return '<p class="hint">Выберите каналы — здесь появится время публикации в каждом.</p>';
        const rows = chosen.map((c) => {
          const v = cmp.next ? cmp.next[num(c.id)] : undefined;
          if (v === undefined) return slotRowHtml(c, '<span class="skeleton sk-inline" role="status"><span class="sr-only">считаем…</span></span>');
          if (v === null) {
            return slotRowHtml(c, `<span class="slot-none">нет расписания</span><button type="button" class="btn btn-ghost btn-sm" data-action="ch-schedule" data-id="${num(c.id)}">${icon('calendar')}<span>Настроить</span></button>`);
          }
          return slotRowHtml(c, `<span class="slot-time">${esc(fmtSlotWhen(v))}</span>`);
        }).join('');
        return `<ul class="slot-list">${rows}</ul>
          ${cmp.nextError ? `<p class="field-error">${esc(cmp.nextError)}</p>` : ''}
          <p class="hint">Пост займёт ближайший свободный слот расписания каждого канала${tzDiffers() ? ` (время — по часовому поясу бота, ${esc(state.serverTz)})` : ''}.</p>`;
      }
      case 'at': {
        const value = cmp.scheduledAt ? toZonedInput(cmp.scheduledAt) : '';
        return `<div class="when-row">
            <label class="sr-only" for="cmp-when">Дата и время публикации</label>
            <input type="datetime-local" id="cmp-when" class="input" value="${esc(value)}" min="${esc(toZonedInput(nowTs()))}" step="60">
            <div class="when-quick">
              <button type="button" class="btn btn-ghost btn-sm" data-action="when-quick" data-when="hour">Через час</button>
              <button type="button" class="btn btn-ghost btn-sm" data-action="when-quick" data-when="tomorrow">Завтра в 10:00</button>
            </div>
          </div>
          <p class="hint" id="cmp-tz-hint">${esc(tzHint())}</p>`;
      }
      default:
        return '<p class="hint">Пост выйдет во всех выбранных каналах сразу после подтверждения.</p>';
    }
  }

  function whenBlockHtml(cmp) {
    const modes = cmp.mode === 'scheduled' ? ['keep', 'now', 'queue', 'at'] : ['now', 'queue', 'at'];
    const radios = modes.map((m) => {
      const d = PUB_MODES[m];
      const on = cmp.pubMode === m;
      return `<label class="pub-mode${on ? ' on' : ''}">
        <input type="radio" name="cmp-pubmode" value="${m}"${on ? ' checked' : ''}>
        <span class="pub-mode-icon">${icon(d.icon)}</span>
        <span class="pub-mode-text"><span class="pub-mode-label">${esc(d.label)}</span><span class="pub-mode-hint">${esc(d.hint)}</span></span>
      </label>`;
    }).join('');
    return `<fieldset class="pub-block pub-when">
      <legend class="pub-title">Когда публиковать</legend>
      <div class="pub-modes">${radios}</div>
      <div class="pub-detail" id="cmp-mode-detail">${modeDetailHtml(cmp)}</div>
    </fieldset>`;
  }

  function actionButton(action, label, iconName, kind = 'secondary') {
    return `<button type="button" class="btn btn-${kind}" data-action="${action}">${icon(iconName)}<span>${esc(label)}</span></button>`;
  }

  function submitButtonHtml(cmp) {
    const labels = {
      keep: ['Сохранить изменения', 'check'],
      now: [cmp.mode === 'scheduled' ? 'Опубликовать сейчас' : 'Опубликовать', 'send'],
      queue: ['Поставить в очередь', 'queue'],
      at: ['Запланировать', 'clock'],
    };
    const [label, iconName] = labels[cmp.pubMode] || labels.now;
    return actionButton('submit', label, iconName, 'primary');
  }

  function actionsHtml(cmp) {
    switch (cmp.mode) {
      case 'new':
      case 'draft':
        return actionButton('save-draft', 'Сохранить черновик', 'edit')
          + actionButton('preview', 'Отправить мне превью', 'eye')
          + submitButtonHtml(cmp);
      case 'scheduled':
        return actionButton('preview', 'Отправить мне превью', 'eye')
          + actionButton('to-draft', 'Отменить расписание', 'x')
          + submitButtonHtml(cmp);
      case 'sent': {
        const p = cmp.post || {};
        const rest = (p.targets || []).some((t) => t.status === 'pending' || t.status === 'failed');
        const retry = p.status === 'scheduled' ? 'Опубликовать в остальные сейчас' : 'Допубликовать в остальные';
        return actionButton('preview', 'Отправить мне превью', 'eye')
          + (rest && (p.status === 'partial' || p.status === 'scheduled') ? actionButton('retry', retry, 'refresh') : '')
          + actionButton('apply', 'Сохранить изменения в каналах', 'check', 'primary');
      }
      default:
        return cmp.postId
          ? `<a class="btn btn-primary" href="#/compose?copy=${num(cmp.postId)}">${icon('copy')}<span>Создать копию</span></a>`
          : '';
    }
  }

  function renderPublish(cmp) {
    const el = $('#cmp-publish', cmp.root);
    const ro = cmp.mode === 'readonly';
    const sent = cmp.mode === 'sent';
    const options = OPTIONS.map(([key, label, hint]) => {
      const locked = ro || (sent && (key === 'silent' || key === 'protect'));
      return `<label class="switch${locked ? ' is-locked' : ''}">
        <input type="checkbox" data-option="${key}"${cmp.options[key] ? ' checked' : ''}${locked ? ' disabled' : ''}>
        <span class="switch-text"><span class="switch-label">${label}</span><span class="switch-hint">${hint}</span></span>
      </label>`;
    }).join('');
    el.innerHTML = `
      <div class="pub-block">
        <h2 class="pub-title" id="cmp-ch-title">Каналы</h2>
        ${sent || ro ? targetsListHtml(cmp.post) : channelPickerHtml(cmp)}
      </div>
      <div class="pub-block">
        <h2 class="pub-title">Настройки публикации</h2>
        <div class="options">${options}</div>
        ${sent ? '<p class="hint">«Без звука» и «Запрет пересылки» у опубликованного поста изменить нельзя.</p>' : ''}
      </div>
      ${sent || ro ? '' : whenBlockHtml(cmp)}
      <div class="pub-actions" id="cmp-actions">${actionsHtml(cmp)}</div>`;
    updatePicker(cmp);
    syncBusy(cmp);
  }

  /** Mode switch: only the details and the primary button change (focus stays on the radio). */
  function updateModeUi(cmp) {
    const detail = $('#cmp-mode-detail', cmp.root);
    if (detail) detail.innerHTML = modeDetailHtml(cmp);
    $$('.pub-mode', cmp.root).forEach((label) => {
      label.classList.toggle('on', $('input', label).value === cmp.pubMode);
    });
    const actions = $('#cmp-actions', cmp.root);
    if (actions) actions.innerHTML = actionsHtml(cmp);
    syncBusy(cmp);
  }

  function updateModeDetail(cmp) {
    if (!cmp.ctx.alive() || state.composer !== cmp) return;
    const detail = $('#cmp-mode-detail', cmp.root);
    if (!detail) return;
    // Do not rebuild the datetime field under the user's fingers.
    if (cmp.pubMode === 'at' && $('#cmp-when', detail)) return;
    detail.innerHTML = modeDetailHtml(cmp);
    syncBusy(cmp);
  }

  /** Next free queue slot per selected channel (GET api/plan/next). */
  async function loadNext(cmp) {
    const ids = Array.from(cmp.channelIds);
    const seq = ++cmp.nextSeq;
    if (!ids.length) {
      cmp.next = {};
      cmp.nextError = '';
      return cmp.next;
    }
    const qs = `channel_ids=${ids.map(String).join(',')}${cmp.postId ? `&post_id=${cmp.postId}` : ''}`;
    const data = await api.get(`api/plan/next?${qs}`);
    const raw = data && data.next && typeof data.next === 'object' ? data.next : {};
    const next = {};
    ids.forEach((id) => {
      const v = raw[String(id)];
      next[id] = v === null || v === undefined || v === '' ? null : num(v);
    });
    if (seq === cmp.nextSeq) {
      cmp.next = next;
      cmp.nextError = '';
    }
    return next;
  }

  function refreshNext(cmp, delay = 0) {
    clearTimeout(cmp.nextTimer);
    cmp.nextTimer = setTimeout(() => {
      loadNext(cmp)
        .catch((err) => {
          if (err.silent) return;
          cmp.nextError = `Не удалось получить расписание: ${err.message}`;
        })
        .finally(() => updateModeDetail(cmp));
    }, delay);
  }

  function previewChannel(cmp) {
    if (cmp.mode === 'sent' || cmp.mode === 'readonly') {
      const t = cmp.post && cmp.post.targets && cmp.post.targets[0];
      if (t) {
        const ch = cmp.channels.find((c) => num(c.id) === num(t.channel_id));
        return ch || { id: t.channel_id, title: targetTitle(t) };
      }
    }
    const active = activeChannels(cmp);
    return active.find((c) => cmp.channelIds.has(num(c.id))) || active[0] || { id: 0, title: 'Ваш канал' };
  }

  function renderPreviewHeader(cmp) {
    const ch = previewChannel(cmp);
    const sub = ch.members_count !== null && ch.members_count !== undefined
      ? `${fmtNum(ch.members_count)} ${plural(ch.members_count, MEMBERS_FORMS)}`
      : 'канал';
    $('#tg-header', cmp.root).innerHTML = `${avatarHtml(ch.id, ch.title, 'sm')}
      <div class="tg-header-text"><div class="tg-title">${esc(ch.title || 'Ваш канал')}</div><div class="tg-sub">${esc(sub)}</div></div>`;
  }

  function previewUrl(raw) {
    const u = String(raw || '').trim();
    if (!u) return '';
    if (u.startsWith('@')) return `https://t.me/${u.slice(1)}`;
    if (/^(https?|tg):\/\//i.test(u)) return u;
    if (/^[\w-]+(\.[\w-]+)+(\/\S*)?$/.test(u)) return `https://${u}`;
    return '';
  }

  function renderPreviewButtons(cmp) {
    const kb = $('#tg-kb', cmp.root);
    const rows = cmp.media.length > 1 ? [] : cmp.buttons
      .map((row) => row.filter((b) => b.text.trim() || b.url.trim()))
      .filter((row) => row.length);
    kb.hidden = !rows.length;
    kb.innerHTML = rows.map((row) => `<div class="tg-kb-row">${row.map((b) => {
      const style = STYLE_VALUES.has(b.style) ? b.style : 'default';
      const href = safeHref(previewUrl(b.url));
      const inner = `<span class="tg-kb-label">${esc(b.text.trim() || 'Кнопка')}</span>${icon('arrowUpRight', 'tg-kb-arrow')}`;
      return href
        ? `<a class="tg-kb-btn st-${style}" href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="${esc(href)}">${inner}</a>`
        : `<span class="tg-kb-btn st-${style}" title="${esc(b.url.trim() ? `Неверная ссылка: ${b.url}` : 'Ссылка не указана')}">${inner}</span>`;
    }).join('')}</div>`).join('');
  }

  /** Rows of a Telegram-like album mosaic by item count. */
  const ALBUM_ROWS = { 2: [2], 3: [1, 2], 4: [1, 3], 5: [2, 3], 6: [3, 3], 7: [2, 2, 3], 8: [2, 3, 3], 9: [3, 3, 3], 10: [3, 3, 4] };

  function albumHtml(items) {
    const rows = ALBUM_ROWS[items.length] || [items.length];
    let i = 0;
    return `<div class="tg-album" role="img" aria-label="${esc(describeMedia(items))}">${rows.map((n) => {
      const cells = items.slice(i, i + n);
      i += n;
      return `<div class="tg-album-row" data-n="${n}">${cells.map((m) => `<div class="tg-cell">${mediaElHtml(m)}${m.type === 'video' ? `<span class="tg-play sm" aria-hidden="true">${icon('play', 'i-fill')}</span>` : ''}</div>`).join('')}</div>`;
    }).join('')}</div>`;
  }

  function renderPreviewMedia(cmp) {
    const box = $('#tg-media', cmp.root);
    if (!box) return;
    const items = cmp.media;
    const sig = items.map((m) => `${m.key}:${m.type}:${m.localUrl || m.url}`).join('|');
    if (box.dataset.sig !== sig) {
      box.dataset.sig = sig;
      if (!items.length) {
        box.hidden = true;
        box.replaceChildren();
      } else if (items.length === 1) {
        const m = items[0];
        box.hidden = false;
        box.innerHTML = `<div class="tg-single">${mediaElHtml(m, { autoplay: m.type === 'animation' })}
          ${m.type === 'video' ? `<span class="tg-play" aria-hidden="true">${icon('play', 'i-fill')}</span>` : ''}
          ${m.type === 'animation' ? '<span class="tg-gif">GIF</span>' : ''}</div>`;
      } else {
        box.hidden = false;
        box.innerHTML = albumHtml(items);
      }
    }
    updateBubbleClasses(cmp);
  }

  function updateBubbleClasses(cmp) {
    const bubble = $('#tg-bubble', cmp.root);
    const textEl = $('#tg-text', cmp.root);
    if (!bubble || !textEl) return;
    const hasMedia = cmp.media.length > 0;
    const hasText = Boolean(cmp.text.trim());
    bubble.classList.toggle('has-media', hasMedia);
    bubble.classList.toggle('caption-above', hasMedia && hasText && cmp.options.caption_above);
    bubble.classList.toggle('meta-overlay', hasMedia && (!hasText || cmp.options.caption_above));
    textEl.hidden = hasMedia && !hasText;
  }

  function updatePreviewTime(cmp) {
    const p = cmp.post;
    const ts = (p && (p.sent_at || p.scheduled_at)) || cmp.scheduledAt;
    $('#tg-time', cmp.root).textContent = zonedParts(ts || nowTs()).time;
  }

  // ---------------------------------------------------------------- compose: live render

  function requestRender(cmp, delay = 300) {
    clearTimeout(cmp.renderTimer);
    cmp.renderTimer = setTimeout(() => doRender(cmp), delay);
  }

  async function doRender(cmp) {
    const seq = ++cmp.renderSeq;
    const hasMedia = cmp.media.length > 0;
    const fallbackLimit = hasMedia ? CAPTION_LIMIT : TEXT_LIMIT;
    if (!cmp.text.trim()) {
      applyRender(cmp, { html: '', length: 0, limit: fallbackLimit, error: null, hasCustomEmoji: false });
      return;
    }
    let result;
    try {
      result = await api.post('api/render', { text_md: cmp.text, has_media: hasMedia, has_photo: hasMedia });
    } catch (err) {
      if (err.silent) return;
      // A 4xx may still carry render details; otherwise show the message as a render error.
      result = { ...(err.data || {}), error: err.message };
    }
    if (seq !== cmp.renderSeq || !cmp.ctx.alive()) return;
    applyRender(cmp, {
      html: typeof result.html === 'string' ? result.html : '',
      length: Number.isFinite(Number(result.length)) ? Number(result.length) : null,
      limit: Number(result.limit) || fallbackLimit,
      error: result.error || null,
      hasCustomEmoji: typeof result.has_custom_emoji === 'boolean' ? result.has_custom_emoji : null,
      premiumEmoji: typeof result.premium_emoji === 'boolean' ? result.premium_emoji : null,
    });
  }

  /** Custom emoji in the markdown, `![😀](tg://emoji?id=…)` (the server's has_custom_emoji wins). */
  const CUSTOM_EMOJI_RE = /!\[[^\]\n]*\]\(\s*tg:\/\/emoji\?id=\d+\s*\)/i;
  const PREMIUM_SETTINGS_HREF = '#/settings?card=premium-emoji';

  /**
   * SPEC_V23: premium emoji in the post while the bot has no Fragment username — Telegram (and the
   * bot) will show them as the plain fallback emoji. The callout is only rebuilt when it toggles,
   * so the live region does not repeat itself on every keystroke.
   */
  function updatePremiumWarn(cmp, res) {
    const box = $('#cmp-premium-warn', cmp.root);
    if (!box) return;
    if (typeof res.premiumEmoji === 'boolean') state.premiumEmoji = res.premiumEmoji;
    const has = typeof res.hasCustomEmoji === 'boolean' ? res.hasCustomEmoji : CUSTOM_EMOJI_RE.test(cmp.text);
    const show = cmp.mode !== 'readonly' && has && state.premiumEmoji === false;
    if (box.dataset.on === String(show)) return;
    box.dataset.on = String(show);
    box.innerHTML = show
      ? `<div class="callout is-warn">${icon('alert')}<div>В посте есть премиум-эмодзи — в канале они станут обычными: у бота нет юзернейма с Fragment (<a class="callout-link" href="${PREMIUM_SETTINGS_HREF}">Настройки → Премиум-эмодзи</a>)</div></div>`
      : '';
  }

  /**
   * Telegram treats the "\n" right after a quote / code block as that block's line end, while
   * `white-space: pre-wrap` would render it as an extra empty line — drop it in the preview.
   */
  function trimBlockNewlines(container) {
    $$('blockquote, pre', container).forEach((block) => {
      const next = block.nextSibling;
      if (next && next.nodeType === Node.TEXT_NODE && next.nodeValue.startsWith('\n')) {
        next.nodeValue = next.nodeValue.slice(1);
      }
    });
  }

  function applyRender(cmp, res) {
    cmp.render = res;
    const root = cmp.root;
    const textEl = $('#tg-text', root);
    if (!textEl) return;
    textEl.replaceChildren();
    if (res.html) {
      textEl.appendChild(sanitizeTelegramHtml(res.html));
      trimBlockNewlines(textEl);
      $$('blockquote[expandable]', textEl).forEach((bq) => {
        bq.setAttribute('aria-expanded', 'false');
        bq.title = 'Нажмите, чтобы развернуть';
      });
    } else if (!cmp.text.trim()) {
      textEl.innerHTML = '<span class="tg-placeholder">Здесь появится текст поста</span>';
    } else {
      textEl.textContent = cmp.text; // render failed: show the raw text rather than nothing
    }
    updateBubbleClasses(cmp);

    const counter = $('#cmp-counter', root);
    const over = res.length !== null && res.length > res.limit;
    counter.textContent = `${res.length === null ? '—' : fmtNum(res.length)} / ${fmtNum(res.limit)}`;
    counter.classList.toggle('over', over);
    counter.title = cmp.media.length ? 'Длина подписи к медиа (лимит Telegram — 1024)' : 'Длина текста (лимит Telegram — 4096)';

    const errEl = $('#cmp-render-error', root);
    errEl.hidden = !res.error;
    errEl.textContent = res.error || '';
    updatePremiumWarn(cmp, res);
  }

  // ---------------------------------------------------------------- compose: editing helpers

  function autosize(ta) {
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(Math.max(ta.scrollHeight + 2, 200), 640)}px`;
  }

  function markDirty(cmp) {
    if (cmp.mode === 'readonly') return;
    cmp.editSeq += 1;
    cmp.dirty = true;
    const flag = $('#cmp-dirty', cmp.root);
    if (flag) flag.hidden = false;
  }

  function setClean(cmp, seqAtStart) {
    // Edits made while a request was in flight keep the post dirty.
    cmp.dirty = cmp.editSeq !== seqAtStart;
    const flag = $('#cmp-dirty', cmp.root);
    if (flag) flag.hidden = !cmp.dirty;
  }

  /** Replaces [start, end) keeping the native undo stack when the browser supports it. */
  function replaceRange(ta, start, end, text, selStart, selEnd) {
    ta.focus();
    ta.setSelectionRange(start, end);
    let done = false;
    try {
      done = document.execCommand('insertText', false, text);
    } catch {
      done = false;
    }
    if (!done) ta.setRangeText(text, start, end, 'end');
    ta.setSelectionRange(selStart, selEnd);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function wrapSelection(ta, kind) {
    const mark = WRAP_MARKS[kind];
    const { selectionStart: s, selectionEnd: e, value } = ta;
    const sel = value.slice(s, e);
    const m = mark.length;

    if (kind === 'code' && sel.includes('\n')) {
      const block = `\`\`\`\n${sel.replace(/\n$/, '')}\n\`\`\``;
      replaceRange(ta, s, e, block, s + 4, s + 4 + sel.replace(/\n$/, '').length);
      return;
    }
    // Toggle off: the selection itself is wrapped.
    if (sel.length >= m * 2 && sel.startsWith(mark) && sel.endsWith(mark)) {
      const inner = sel.slice(m, sel.length - m);
      replaceRange(ta, s, e, inner, s, s + inner.length);
      return;
    }
    // Toggle off: markers right around the selection (for "*" make sure it is not part of "**").
    const before = value.slice(s - m, s);
    const after = value.slice(e, e + m);
    const singleOk = m > 1 || (value[s - m - 1] !== mark && value[e + m] !== mark);
    if (s >= m && before === mark && after === mark && singleOk) {
      replaceRange(ta, s - m, e + m, sel, s - m, e - m);
      return;
    }
    if (!sel.trim()) {
      const ph = WRAP_PLACEHOLDERS[kind] || 'текст';
      replaceRange(ta, s, e, `${sel}${mark}${ph}${mark}`, s + sel.length + m, s + sel.length + m + ph.length);
      return;
    }
    // Keep surrounding spaces outside of the markers: "word " → "**word** ".
    const lead = sel.match(/^\s*/)[0];
    const trail = sel.match(/\s*$/)[0];
    const core = sel.slice(lead.length, sel.length - trail.length);
    const start = s + lead.length + m;
    replaceRange(ta, s, e, `${lead}${mark}${core}${mark}${trail}`, start, start + core.length);
  }

  function insertLink(ta) {
    const { selectionStart: s, selectionEnd: e, value } = ta;
    const sel = value.slice(s, e);
    if (/^(https?:\/\/|tg:\/\/|t\.me\/)\S+$/i.test(sel.trim())) {
      const label = 'текст ссылки';
      replaceRange(ta, s, e, `[${label}](${sel.trim()})`, s + 1, s + 1 + label.length);
      return;
    }
    const label = sel || 'текст ссылки';
    const urlStart = s + label.length + 3; // "[" + label + "]("
    replaceRange(ta, s, e, `[${label}](https://)`, urlStart, urlStart + 'https://'.length);
  }

  /** Adds (or removes, if every line already has it) a prefix to all lines in the selection. */
  function prefixLines(ta, prefix) {
    const { selectionStart: s, selectionEnd: e, value } = ta;
    const lineStart = value.lastIndexOf('\n', s - 1) + 1;
    const endPos = e > s && value[e - 1] === '\n' ? e - 1 : e;
    let lineEnd = value.indexOf('\n', endPos);
    if (lineEnd === -1) lineEnd = value.length;
    const lines = value.slice(lineStart, lineEnd).split('\n');
    let next;
    if (lines.every((l) => !l.trim())) {
      next = [prefix + lines.join('\n')];
    } else if (lines.every((l) => l.startsWith(prefix) || !l.trim())) {
      next = lines.map((l) => (l.startsWith(prefix) ? l.slice(prefix.length) : l));
    } else {
      next = lines.map((l) => (l.trim() && !l.startsWith(prefix) ? prefix + l : l));
    }
    const text = next.join('\n');
    const caret = lineStart + text.length;
    replaceRange(ta, lineStart, lineEnd, text, s === e ? caret : lineStart, caret);
  }

  function applyFormat(ta, kind) {
    if (!ta || ta.readOnly) return;
    if (WRAP_MARKS[kind]) wrapSelection(ta, kind);
    else if (kind === 'link') insertLink(ta);
    else if (kind === 'quote') prefixLines(ta, '> ');
    else if (kind === 'list') prefixLines(ta, '- ');
  }

  /** Applies a structural change to the buttons and focuses `focusSel` afterwards. */
  function editButtons(cmp, mutate) {
    const focusSel = mutate(cmp.buttons);
    cmp.buttons = cmp.buttons.filter((row) => row.length);
    renderButtonsEditor(cmp);
    renderPreviewButtons(cmp);
    markDirty(cmp);
    if (focusSel) {
      const el = $(focusSel, cmp.root);
      if (el) el.focus();
    }
  }

  const itemSel = (r, c) => `.btn-item[data-row="${r}"][data-col="${c}"] [data-field="text"]`;

  function itemPos(el) {
    const item = el.closest('.btn-item');
    return item ? [num(item.dataset.row), num(item.dataset.col)] : [-1, -1];
  }

  /** True when the clipboard carries visible text (Office/Calc cells, rich text), not only a picture. */
  function clipboardHasText(cd) {
    const html = cd.getData('text/html');
    if (html) {
      // DOMParser documents are inert: no scripts run, no images load.
      return new DOMParser().parseFromString(html, 'text/html').body.textContent.trim() !== '';
    }
    const plain = cd.getData('text/plain').trim();
    return plain !== '' && !/^\S+:\/\/\S+$/.test(plain); // a lone URL comes with "Copy image"
  }

  // ---------------------------------------------------------------- compose: saving

  /**
   * Rows go out uncompacted so the server's "Ряд N, кнопка M" errors match the editor (the server
   * drops blank buttons and rows itself). Nothing filled in → no buttons.
   */
  function collectButtons(cmp) {
    let any = false;
    const rows = cmp.buttons.map((row, r) => row.map((b, c) => {
      const text = b.text.trim();
      const url = b.url.trim();
      if (text || url) {
        any = true;
        if (!text) throw new ValidationError(`Ряд ${r + 1}, кнопка ${c + 1}: укажите текст`, itemSel(r, c));
        if (!url) {
          throw new ValidationError(`Ряд ${r + 1}, кнопка ${c + 1}: укажите ссылку`, `.btn-item[data-row="${r}"][data-col="${c}"] [data-field="url"]`);
        }
      }
      return { text, url, style: STYLE_VALUES.has(b.style) ? b.style : null };
    }));
    return any ? rows : [];
  }

  /** Media rules of Telegram, checked before any save (the server checks them again). */
  function validateMediaRules(cmp) {
    const items = cmp.media;
    if (items.length > MAX_ALBUM) throw new ValidationError(`В посте может быть не больше ${MAX_ALBUM} фото и видео`, '#cmp-media');
    if (items.length > 1 && items.some((m) => m.type === 'animation')) throw new ValidationError(ALBUM_GIF_ERROR, '#cmp-media');
    if (items.length > 1 && hasButtons(cmp)) throw new ValidationError(ALBUM_BUTTONS_ERROR, '[data-action="btns-clear"]');
  }

  /**
   * Media of the request, in order. Items already stored (in this post or, with `copy_from`, in the
   * duplicated one) are echoed back by their `file_id` / `path`; `index` is their position there.
   */
  function mediaPayload(cmp) {
    return cmp.media.filter((m) => m.state === 'ready').map((m) => {
      const item = { type: m.type };
      if (m.fileId) item.file_id = m.fileId;
      if (m.path) item.path = m.path;
      if (Number.isInteger(m.index)) item.index = m.index;
      return item;
    });
  }

  /** `channelIds` overrides the editor's selection (a preview keeps the plan as it is). */
  function buildPayload(cmp, action, channelIds = null) {
    const body = {
      text_md: cmp.text,
      media: mediaPayload(cmp),
      buttons: collectButtons(cmp),
      channel_ids: Array.isArray(channelIds) ? channelIds.slice() : Array.from(cmp.channelIds),
      options: { ...cmp.options },
    };
    if (!cmp.postId && cmp.copyOf) body.copy_from = cmp.copyOf;
    if (action) body.action = action;
    if (action === 'schedule') body.scheduled_at = cmp.scheduledAt;
    return body;
  }

  /** Checks shared by publish / queue / schedule / apply. */
  function validateForSend(cmp, { needChannels = true } = {}) {
    if (!cmp.text.trim() && !cmp.media.length) throw new ValidationError('Пост пустой: добавьте текст или медиа', '#cmp-text');
    if (cmp.render && cmp.render.length !== null && cmp.render.length > cmp.render.limit) {
      throw new ValidationError(cmp.render.error || `Текст слишком длинный: ${cmp.render.length} из ${cmp.render.limit}`, '#cmp-text');
    }
    if (needChannels && !cmp.channelIds.size) {
      throw new ValidationError(activeChannels(cmp).length ? 'Выберите хотя бы один канал' : 'Сначала добавьте канал на странице «Каналы»');
    }
    collectButtons(cmp); // throws on half-filled buttons
    validateMediaRules(cmp);
  }

  function readWhen(cmp) {
    const input = $('#cmp-when', cmp.root);
    if (input) cmp.scheduledAt = fromZonedInput(input.value);
    return cmp.scheduledAt;
  }

  function validateWhen(cmp) {
    const ts = readWhen(cmp);
    if (!ts) throw new ValidationError('Укажите дату и время публикации', '#cmp-when');
    if (ts <= nowTs() + 30) throw new ValidationError('Это время уже прошло — выберите время в будущем', '#cmp-when');
  }

  function statusChangeMessage(cmp) {
    switch (cmp.post && cmp.post.status) {
      case 'sent':
      case 'partial':
        return cmp.dirty
          ? 'Пока вы редактировали, пост опубликовали. Ваши правки остались в редакторе — примените их кнопкой «Сохранить изменения в каналах».'
          : 'Пост уже опубликован — редактор обновлён.';
      case 'sending':
        return 'Пост сейчас публикуется — дождитесь окончания и попробуйте снова.';
      case 'deleted':
        return 'Пост удалён из каналов — его можно только дублировать.';
      default:
        return 'Статус поста изменился — проверьте настройки публикации и повторите.';
    }
  }

  const LOST_ANSWER_MESSAGE = 'Ответ сервера не дошёл, а статус поста уже изменился — редактор обновлён. Проверьте пост в каналах, прежде чем повторять.';

  /**
   * POST (new) or PUT (existing) the post. Returns the API response {post, report, schedule}.
   * `failSeq` = cmp.uploadFails when the user started the action: an upload that failed since then
   * stops the save (the post must not go out without that file). `channelIds` replaces the editor's
   * channel selection in the request.
   */
  async function savePost(cmp, action, { apply = false, failSeq, channelIds = null } = {}) {
    const fails = failSeq ?? cmp.uploadFails;
    if (!(await settleUploads(cmp)) || cmp.uploadFails !== fails) throw new ValidationError(UPLOAD_FAILED, '#cmp-media');
    validateMediaRules(cmp);
    const body = buildPayload(cmp, action, channelIds);
    if (apply) body.apply = true;
    const seq = cmp.editSeq;
    const mediaSeq = cmp.mediaSeq;
    let res;
    try {
      res = cmp.postId
        ? await sendIdempotent('PUT', `api/posts/${cmp.postId}`, body)
        : await sendIdempotent('POST', 'api/posts', body);
    } catch (err) {
      // The post may have changed status meanwhile (published by the scheduler, deleted from the
      // bot…): switch the editor to the real status, keeping the user's edits.
      const known = err instanceof ApiError && err.data && err.data.post && typeof err.data.post === 'object' ? err.data.post : null;
      if (!cmp.postId && known && known.id !== undefined) {
        cmp.savedMediaSeq = mediaSeq;
        adoptServerMedia(cmp, known);
        adoptStatus(cmp, known); // the server kept the post (e.g. publishing was refused)
      } else if (err instanceof ApiError && (err.status === 400 || err.status === 409) && cmp.postId) {
        if (await resyncPost(cmp, known)) err.message = statusChangeMessage(cmp);
      } else if (err instanceof ApiError && (err.status === 0 || err.status >= 500) && cmp.postId) {
        // No answer (proxy timeout during a long publication): the request may have gone through —
        // show the post as the server has it now instead of inviting a blind retry.
        if (await resyncPost(cmp, known)) err.message = LOST_ANSWER_MESSAGE;
      }
      throw err;
    }
    const post = res && res.post ? res.post : null;
    if (!post || post.id === undefined || post.id === null) throw new ApiError('Сервер вернул неожиданный ответ');
    cmp.savedMediaSeq = mediaSeq;
    setClean(cmp, seq);
    return res;
  }

  /** After a save: take the server's media references when the gallery did not change meanwhile. */
  function adoptServerMedia(cmp, post) {
    if (cmp.mediaSeq !== cmp.savedMediaSeq) return;
    const server = postMedia(post);
    if (server.length !== cmp.media.length) return;
    server.forEach((m, i) => {
      const local = cmp.media[i];
      local.index = m.index;
      if (m.file_id) local.fileId = m.file_id;
      if (m.path) local.path = m.path;
      if (safeSrc(m.url)) local.url = safeSrc(m.url);
    });
  }

  /** Re-renders the editor for a new post status, keeping text, media, buttons and options. */
  function adoptStatus(cmp, post) {
    if (state.composer !== cmp || !cmp.ctx.alive()) return;
    cmp.post = post;
    cmp.postId = num(post.id);
    cmp.copyOf = null;
    cmp.mode = postMode(post);
    if (cmp.mode === 'scheduled') {
      if (!PUB_MODES[cmp.pubMode]) cmp.pubMode = 'keep';
    } else if (cmp.pubMode === 'keep') {
      cmp.pubMode = 'now';
    }
    if (cmp.mode === 'draft' || cmp.mode === 'scheduled') {
      const keep = new Set(cmp.channelIds);
      applyTargets(cmp, post);
      cmp.channelIds = new Set(Array.from(keep).filter((id) => !cmp.lockedIds.has(id)));
    } else {
      applyTargets(cmp, post);
    }
    replaceHash(`#/compose/${cmp.postId}`);
    renderComposer(cmp);
  }

  /** After a draft save: keep the editor as is, just switch to "existing post" state. */
  function softUpdate(cmp, post) {
    if (state.composer !== cmp || !cmp.ctx.alive()) return;
    cmp.postId = num(post.id);
    cmp.post = post;
    cmp.copyOf = null;
    cmp.mode = postMode(post);
    if (cmp.mode !== 'scheduled' && cmp.pubMode === 'keep') cmp.pubMode = 'now';
    const shown = () => cmp.media.map((m) => m.localUrl || m.url).join('|');
    const urls = shown();
    adoptServerMedia(cmp, post);
    replaceHash(`#/compose/${cmp.postId}`);
    renderHead(cmp);
    renderPublish(cmp); // ends with syncBusy: a running action keeps every button locked
    if (shown() !== urls) {
      // e.g. a saved duplicate: its files are now served from this post, not from the source.
      renderMedia(cmp);
      renderPreviewMedia(cmp);
    }
  }

  /** After publish / schedule / apply: rebuild the editor from the server's version. */
  function reloadFrom(cmp, post) {
    if (state.composer !== cmp || !cmp.ctx.alive()) return;
    if (cmp.dirty) {
      // Typed while the request was in flight: keep those edits, only switch the status.
      adoptServerMedia(cmp, post);
      adoptStatus(cmp, post);
      return;
    }
    loadPostIntoComposer(cmp, post);
    replaceHash(`#/compose/${cmp.postId}`);
    renderComposer(cmp);
  }

  /**
   * Re-reads the post (or uses `known` from an error response) and switches the editor when its
   * status changed. Unsaved edits are kept. Returns true when the status changed.
   */
  async function resyncPost(cmp, known = null) {
    if (!cmp.postId) return false;
    let post = known && typeof known === 'object' && known.id !== undefined && known.status ? known : null;
    if (!post) {
      try {
        post = unwrap(await api.get(`api/posts/${cmp.postId}`), 'post');
      } catch {
        return false; // keep the current view; the error is shown anyway
      }
    }
    if (!post || state.composer !== cmp || !cmp.ctx.alive()) return false;
    if (post.status === (cmp.post && cmp.post.status)) return false;
    if (cmp.dirty) adoptStatus(cmp, post);
    else reloadFrom(cmp, post);
    return true;
  }

  /** Action buttons follow the lock even after a part of the editor was re-rendered. */
  function syncBusy(cmp) {
    const head = state.composer === cmp && state.pageActions ? $$('[data-action]', state.pageActions) : [];
    $$('#cmp-publish [data-action]', cmp.root).concat(head).forEach((b) => {
      b.disabled = cmp.locked || b.dataset.off === '1';
      b.classList.toggle('is-busy', cmp.locked && b.dataset.action === cmp.busyAction);
      if (cmp.locked && b.dataset.action === cmp.busyAction) b.setAttribute('aria-busy', 'true');
      else b.removeAttribute('aria-busy');
    });
  }

  function lockedNotice(cmp) {
    if (!cmp.locked) return false;
    toast('Дождитесь окончания текущего действия', 'info');
    return true;
  }

  /** Runs a composer action with all action buttons locked and a spinner on `action`'s button. */
  async function withLock(cmp, action, fn) {
    if (lockedNotice(cmp)) return;
    cmp.locked = true;
    cmp.busyAction = action;
    syncBusy(cmp);
    try {
      await fn();
    } finally {
      cmp.locked = false;
      cmp.busyAction = '';
      if (state.composer === cmp) syncBusy(cmp);
    }
  }

  function channelListHtml(channels) {
    return `<ul class="modal-list">${channels.map((c) => `<li>${avatarHtml(c.id, c.title, 'xs')}<span>${esc(c.title || 'Без названия')}</span>${c.members_count !== null && c.members_count !== undefined ? `<span class="muted small">${fmtNum(c.members_count)} ${esc(plural(c.members_count, MEMBERS_FORMS))}</span>` : ''}</li>`).join('')}</ul>`;
  }

  const channelNames = (list) => list.map((c) => `«${c.title || 'Без названия'}»`).join(', ');

  async function actSaveDraft(cmp) {
    await withLock(cmp, 'save-draft', async () => {
      collectButtons(cmp);
      const res = await savePost(cmp, 'draft');
      softUpdate(cmp, res.post);
      toast('Черновик сохранён', 'success');
    });
  }

  function actSubmit(cmp, btn) {
    if (cmp.mode === 'scheduled' && cmp.pubMode === 'keep') return actSaveKeep(cmp, btn);
    if (cmp.pubMode === 'queue') return actQueue(cmp, btn);
    if (cmp.pubMode === 'at') return actSchedule(cmp, btn);
    return actPublish(cmp, btn);
  }

  /** Scheduled post, «Не менять время»: content only, every channel keeps its time in the plan. */
  async function actSaveKeep(cmp, btn) {
    if (lockedNotice(cmp)) return;
    const failSeq = cmp.uploadFails;
    await ensureUploads(cmp, btn, failSeq);
    validateForSend(cmp);
    if (!earliestPlanned(cmp) && selectedChannels(cmp).some((c) => !plannedTime(cmp, c.id))) {
      throw new ValidationError('У поста нет времени публикации — выберите «В очередь по расписанию» или «Ко времени»', '#cmp-publish input[value="queue"]');
    }
    await withLock(cmp, 'submit', async () => {
      const res = await savePost(cmp, null, { failSeq });
      reloadFrom(cmp, res.post);
      toast('Изменения сохранены', 'success');
    });
  }

  async function actQueue(cmp, btn) {
    if (lockedNotice(cmp)) return;
    const failSeq = cmp.uploadFails;
    await ensureUploads(cmp, btn, failSeq);
    validateForSend(cmp);
    const next = await busy(btn, () => loadNext(cmp));
    if (!next) return;
    updateModeDetail(cmp);
    const chosen = selectedChannels(cmp);
    const none = chosen.filter((c) => next[num(c.id)] === null);
    if (none.length) {
      throw new ValidationError(`У ${none.length > 1 ? 'каналов' : 'канала'} ${channelNames(none)} не настроено расписание публикаций — нажмите «Настроить» или выберите другой способ`, '#cmp-mode-detail [data-action="ch-schedule"]');
    }
    const items = chosen.map((c) => `<li>${avatarHtml(c.id, c.title, 'xs')}<span>${esc(c.title || 'Без названия')}</span><span class="muted small">${esc(fmtSlotWhen(next[num(c.id)]))}</span></li>`).join('');
    const ok = await confirmModal({
      title: 'Поставить пост в очередь?',
      html: `<p>Пост выйдет в ближайший свободный слот расписания каждого канала:</p><ul class="modal-list">${items}</ul>`,
      confirmLabel: 'В очередь',
      confirmIcon: 'queue',
    });
    if (!ok) return;
    await withLock(cmp, 'submit', async () => {
      const res = await savePost(cmp, 'queue', { failSeq });
      reloadFrom(cmp, res.post);
      const times = Object.values(res.schedule && typeof res.schedule === 'object' ? res.schedule : {}).map(num).filter(Boolean).sort((a, b) => a - b);
      if (times.length > 1) toast(`Пост в очереди в ${times.length} ${plural(times.length, CHANNELS_IN_FORMS)}, первая публикация ${fmtSlotWhen(times[0])}`, 'success');
      else toast(`Пост в очереди: публикация ${fmtSlotWhen(times[0] || res.post.scheduled_at)}`, 'success');
    });
  }

  async function actSchedule(cmp, btn) {
    if (lockedNotice(cmp)) return;
    const failSeq = cmp.uploadFails;
    await ensureUploads(cmp, btn, failSeq);
    validateWhen(cmp);
    validateForSend(cmp);
    await withLock(cmp, 'submit', async () => {
      const res = await savePost(cmp, 'schedule', { failSeq });
      reloadFrom(cmp, res.post);
      toast(`Пост запланирован: ${fmtWhen(res.post.scheduled_at || cmp.scheduledAt)}`, 'success');
    });
  }

  async function actPublish(cmp, btn) {
    if (lockedNotice(cmp)) return;
    const failSeq = cmp.uploadFails;
    await ensureUploads(cmp, btn, failSeq);
    validateForSend(cmp);
    const chosen = selectedChannels(cmp);
    const ok = await confirmModal({
      title: 'Опубликовать пост?',
      html: `<p>Пост сразу появится в ${chosen.length} ${plural(chosen.length, CHANNELS_IN_FORMS)}:</p>${channelListHtml(chosen)}`,
      confirmLabel: 'Опубликовать',
      confirmIcon: 'send',
      danger: true,
    });
    if (!ok) return;
    await withLock(cmp, 'submit', async () => {
      if (!cmp.postId) {
        // A new post is stored as a draft first and published by its id: if the answer to a long
        // publication is lost (proxy timeout), pressing «Опубликовать» again reaches the same post
        // (409 while it is being sent, «уже опубликован» after) instead of creating a second one.
        const draft = await savePost(cmp, 'draft', { failSeq });
        cmp.postId = num(draft.post.id); // set even when the editor was left meanwhile
        softUpdate(cmp, draft.post);
      }
      const res = await savePost(cmp, 'publish', { failSeq });
      reloadFrom(cmp, res.post);
      showReport(res.report);
    });
  }

  /** Channels a scheduled post has in the plan now: its unsent targets in active channels. */
  function plannedChannelIds(cmp) {
    const activeIds = new Set(activeChannels(cmp).map((c) => num(c.id)));
    return ((cmp.post && cmp.post.targets) || [])
      .filter((t) => (t.status === 'pending' || t.status === 'failed') && activeIds.has(num(t.channel_id)))
      .map((t) => num(t.channel_id));
  }

  async function actPreview(cmp) {
    await withLock(cmp, 'preview', async () => {
      const editable = cmp.mode === 'new' || cmp.mode === 'draft' || cmp.mode === 'scheduled';
      if (editable && (!cmp.postId || cmp.dirty)) {
        if (cmp.mode === 'scheduled') {
          // A preview stores the content only and never changes the plan: saved without an action
          // (every channel keeps its time) and with the channels the post already has. A changed
          // selection would put new channels at the post's first time whatever mode is picked, so
          // it stays unsaved until «Поставить в очередь» / «Запланировать» / «Сохранить изменения».
          const planned = plannedChannelIds(cmp);
          const same = planned.length === cmp.channelIds.size && planned.every((id) => cmp.channelIds.has(id));
          const res = await savePost(cmp, null, same ? {} : { channelIds: planned });
          softUpdate(cmp, res.post);
          if (!same) markDirty(cmp);
        } else {
          const res = await savePost(cmp, 'draft');
          softUpdate(cmp, res.post);
        }
      }
      const res = await api.post(`api/posts/${cmp.postId}/preview`);
      const sent = num(res.sent);
      if (sent > 0) toast(`Превью отправлено в личные сообщения бота (${sent})`, 'success');
      else toast('Не удалось отправить превью. Напишите боту /start и попробуйте снова.', 'error');
      if (cmp.mode === 'sent' && cmp.dirty) toast('В превью — сохранённая версия: ваши правки ещё не применены', 'info');
    });
  }

  async function actToDraft(cmp) {
    await withLock(cmp, 'to-draft', async () => {
      const res = await savePost(cmp, 'draft');
      reloadFrom(cmp, res.post);
      toast('Расписание отменено, пост сохранён как черновик', 'success');
    });
  }

  async function actApply(cmp, btn) {
    if (lockedNotice(cmp)) return;
    const failSeq = cmp.uploadFails;
    await ensureUploads(cmp, btn, failSeq);
    validateForSend(cmp, { needChannels: false });
    const count = ((cmp.post && cmp.post.targets) || []).filter((t) => t.status === 'sent').length;
    const ok = await confirmModal({
      title: 'Изменить опубликованный пост?',
      message: `Текст, медиа и кнопки обновятся в ${count} ${plural(count, CHANNELS_IN_FORMS)}. Подписчики не получат нового уведомления.`,
      confirmLabel: 'Сохранить в каналах',
      confirmIcon: 'check',
      danger: true,
    });
    if (!ok) return;
    await withLock(cmp, 'apply', async () => {
      const res = await savePost(cmp, null, { apply: true, failSeq });
      const rep = res.report;
      if (rep && num(rep.fail_count) > 0 && num(rep.ok_count) === 0) {
        // No channel took the edit: the server kept the published version, the edits stay here
        // (unsaved) so they can be retried or discarded.
        keepEditsAfterFailedApply(cmp, res.post);
        showReport(rep, 'Изменения не применены', 'Каналы показывают прежнюю версию поста. Ваши правки остались в редакторе — устраните причину и нажмите «Сохранить изменения в каналах» ещё раз.');
        return;
      }
      reloadFrom(cmp, res.post);
      if (rep) showReport(rep, 'Изменения в каналах');
      else toast('Изменения сохранены', 'success');
    });
  }

  /** After an apply that no channel accepted: show the fresh targets (errors), keep the edits unsaved. */
  function keepEditsAfterFailedApply(cmp, post) {
    if (state.composer !== cmp || !cmp.ctx.alive()) return;
    if (post && typeof post === 'object' && post.id !== undefined) cmp.post = post;
    markDirty(cmp);
    renderHead(cmp);
    renderPublish(cmp);
  }

  async function actRetry(cmp) {
    if (lockedNotice(cmp)) return;
    const failed = ((cmp.post && cmp.post.targets) || []).filter((t) => t.status !== 'sent' && t.status !== 'deleted');
    const ok = await confirmModal({
      title: cmp.post && cmp.post.status === 'scheduled' ? 'Опубликовать в остальных каналах сейчас?' : 'Допубликовать пост?',
      html: `<p>Сохранённая версия поста будет отправлена сейчас в каналы, где её ещё нет:</p><ul class="modal-list">${failed.map((t) => `<li>${avatarHtml(t.channel_id, targetTitle(t), 'xs')}<span>${esc(targetTitle(t))}</span></li>`).join('')}</ul>`,
      confirmLabel: 'Отправить',
      confirmIcon: 'send',
      danger: true,
    });
    if (!ok) return;
    await withLock(cmp, 'retry', async () => {
      try {
        const res = await sendIdempotent('POST', `api/posts/${cmp.postId}/publish`);
        const post = res.post || null;
        if (post) reloadFrom(cmp, post);
        else await resyncPost(cmp);
        showReport(res.report);
      } catch (err) {
        await resyncPost(cmp, err.data && err.data.post);
        throw err;
      }
    });
  }

  async function actDeletePost(cmp) {
    if (lockedNotice(cmp)) return;
    const ok = await confirmModal({
      title: 'Удалить пост?',
      message: 'Запись о посте будет удалена из панели. Это действие нельзя отменить.',
      confirmLabel: 'Удалить',
      danger: true,
    });
    if (!ok) return;
    await api.del(`api/posts/${cmp.postId}`);
    cmp.dirty = false;
    toast('Пост удалён', 'success');
    location.hash = '#/posts';
  }

  async function actChannelSchedule(cmp, id) {
    const ch = cmp.channels.find((c) => num(c.id) === num(id));
    if (!ch) return;
    const updated = await editChannelSchedule(ch);
    if (!updated || !cmp.ctx.alive()) return;
    Object.assign(ch, updated);
    refreshNext(cmp);
  }

  function setQuickWhen(cmp, kind) {
    let ts;
    if (kind === 'hour') {
      ts = Math.ceil((nowTs() + 3600) / 300) * 300;
    } else {
      const p = zonedParts(nowTs() + 86400);
      ts = fromZonedInput(`${p.y}-${pad2(p.m)}-${pad2(p.d)}T10:00`);
    }
    const input = $('#cmp-when', cmp.root);
    if (input) input.value = toZonedInput(ts);
    cmp.scheduledAt = ts;
  }

  function composerShortcutAction(cmp) {
    if (cmp.mode === 'new' || cmp.mode === 'draft') return 'save-draft';
    if (cmp.mode === 'scheduled' && cmp.pubMode === 'keep') return 'submit';
    return '';
  }

  // ---------------------------------------------------------------- compose: events

  function pickFiles(cmp, replaceKey = '') {
    if (cmp.mode === 'readonly') return;
    const input = $('#cmp-file', cmp.root);
    cmp.replaceKey = replaceKey;
    input.multiple = !replaceKey && cmp.mode !== 'sent';
    input.click();
  }

  function bindComposer(cmp) {
    const root = cmp.root;
    const textarea = () => $('#cmp-text', root);
    const actionBtn = (name) => $(`#cmp-publish [data-action="${name}"]`, root);
    const tileKey = (el) => (el.closest('[data-key]') || {}).dataset?.key || '';
    const selectionChanged = () => {
      markDirty(cmp);
      updatePicker(cmp);
      renderPreviewHeader(cmp);
      if (cmp.pubMode === 'queue') refreshNext(cmp, 250);
      updateModeDetail(cmp);
    };

    onAction(root, {
      fmt: (el) => applyFormat(textarea(), el.dataset.fmt),
      'media-pick': () => pickFiles(cmp),
      'media-replace': (el) => pickFiles(cmp, tileKey(el)),
      'media-remove': (el) => removeMedia(cmp, tileKey(el)),
      'media-left': (el) => moveMedia(cmp, tileKey(el), -1),
      'media-right': (el) => moveMedia(cmp, tileKey(el), 1),
      'btns-clear': () => {
        cmp.buttons = [];
        markDirty(cmp);
        renderButtonsEditor(cmp);
        renderPreviewButtons(cmp);
        $('#cmp-buttons', root).focus();
        toast('Кнопки убраны', 'info');
      },
      'row-add': () => editButtons(cmp, (rows) => {
        if (rows.length >= MAX_ROWS) return '';
        rows.push([emptyButton()]);
        return itemSel(rows.length - 1, 0);
      }),
      'row-up': (el) => editButtons(cmp, (rows) => {
        const r = num(el.dataset.row);
        if (r > 0) [rows[r - 1], rows[r]] = [rows[r], rows[r - 1]];
        return `[data-action="row-up"][data-row="${Math.max(0, r - 1)}"]:not([disabled]), [data-action="row-down"][data-row="${Math.max(0, r - 1)}"]`;
      }),
      'row-down': (el) => editButtons(cmp, (rows) => {
        const r = num(el.dataset.row);
        if (r < rows.length - 1) [rows[r + 1], rows[r]] = [rows[r], rows[r + 1]];
        return `[data-action="row-down"][data-row="${r + 1}"]:not([disabled]), [data-action="row-up"][data-row="${r + 1}"]`;
      }),
      'row-del': (el) => editButtons(cmp, (rows) => {
        rows.splice(num(el.dataset.row), 1);
        return '[data-action="row-add"]';
      }),
      'btn-add': (el) => editButtons(cmp, (rows) => {
        const r = num(el.dataset.row);
        if (!rows[r] || rows[r].length >= MAX_PER_ROW) return '';
        rows[r].push(emptyButton());
        return itemSel(r, rows[r].length - 1);
      }),
      'btn-del': (el) => editButtons(cmp, (rows) => {
        const [r, c] = itemPos(el);
        if (rows[r]) rows[r].splice(c, 1);
        return '[data-action="row-add"]';
      }),
      'btn-style': (el) => {
        const [r, c] = itemPos(el);
        const b = cmp.buttons[r] && cmp.buttons[r][c];
        if (!b) return;
        b.style = el.dataset.style || '';
        $$('.swatch', el.parentElement).forEach((s) => s.setAttribute('aria-pressed', String(s === el)));
        renderPreviewButtons(cmp);
        markDirty(cmp);
      },
      'ch-all': () => {
        activeChannels(cmp).forEach((c) => {
          if (!cmp.lockedIds.has(num(c.id))) cmp.channelIds.add(num(c.id));
        });
        selectionChanged();
      },
      'ch-none': () => {
        cmp.channelIds.clear();
        selectionChanged();
      },
      'ch-schedule': (el) => actChannelSchedule(cmp, el.dataset.id),
      'when-quick': (el) => setQuickWhen(cmp, el.dataset.when),
      'save-draft': () => actSaveDraft(cmp),
      submit: (el) => actSubmit(cmp, el),
      preview: () => actPreview(cmp),
      'to-draft': () => actToDraft(cmp),
      apply: (el) => actApply(cmp, el),
      retry: () => actRetry(cmp),
      'delete-post': () => actDeletePost(cmp),
    });

    // Keep the textarea focused (and its selection) when clicking toolbar buttons.
    root.addEventListener('mousedown', (e) => {
      if (e.target.closest('.tb-btn')) e.preventDefault();
    });

    root.addEventListener('input', (e) => {
      const t = e.target;
      if (t.id === 'cmp-text') {
        if (cmp.mode === 'readonly') return;
        cmp.text = t.value;
        autosize(t);
        markDirty(cmp);
        updateBubbleClasses(cmp);
        requestRender(cmp);
        return;
      }
      const field = t.dataset && t.dataset.field;
      if (field === 'text' || field === 'url') {
        const [r, c] = itemPos(t);
        const b = cmp.buttons[r] && cmp.buttons[r][c];
        if (!b) return;
        b[field] = t.value;
        renderPreviewButtons(cmp);
        markDirty(cmp);
      }
    });

    root.addEventListener('change', (e) => {
      const t = e.target;
      if (t.id === 'cmp-file') {
        const files = Array.from(t.files || []);
        t.value = '';
        const replaceKey = cmp.replaceKey;
        cmp.replaceKey = '';
        if (files.length) addFiles(cmp, files, { replaceKey });
      } else if (t.dataset.channel !== undefined) {
        const id = num(t.dataset.channel);
        if (t.checked) cmp.channelIds.add(id);
        else cmp.channelIds.delete(id);
        selectionChanged();
      } else if (t.dataset.folderToggle !== undefined) {
        const group = t.closest('.chip-group');
        const ids = $$('input[data-channel]:not([disabled])', group).map((i) => num(i.dataset.channel));
        const all = ids.every((id) => cmp.channelIds.has(id));
        ids.forEach((id) => (all ? cmp.channelIds.delete(id) : cmp.channelIds.add(id)));
        selectionChanged();
      } else if (t.dataset.option) {
        cmp.options[t.dataset.option] = t.checked;
        markDirty(cmp);
        updateBubbleClasses(cmp);
      } else if (t.name === 'cmp-pubmode') {
        cmp.pubMode = PUB_MODES[t.value] ? t.value : 'now';
        updateModeUi(cmp);
        if (cmp.pubMode === 'queue') refreshNext(cmp);
      } else if (t.id === 'cmp-when') {
        cmp.scheduledAt = fromZonedInput(t.value);
      } else if (t.dataset.mediaGif !== undefined) {
        const item = cmp.media.find((m) => m.key === tileKey(t));
        if (!item) return;
        item.type = t.checked ? 'animation' : 'video';
        item.fileId = null; // Telegram file ids are typed: the file is re-sent from the upload
        markDirty(cmp);
        mediaChanged(cmp);
      }
    });

    root.addEventListener('keydown', (e) => {
      const t = e.target;
      if (t.id === 'cmp-drop' && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        pickFiles(cmp);
        return;
      }
      if (t.id !== 'cmp-text' || !(e.ctrlKey || e.metaKey) || e.altKey) return;
      // e.code keeps shortcuts working with the Russian keyboard layout.
      const map = { KeyB: 'bold', KeyI: 'italic', KeyU: 'underline', KeyK: 'link' };
      const kind = map[e.code];
      if (kind && !e.shiftKey) {
        e.preventDefault();
        applyFormat(t, kind);
      }
    });

    root.addEventListener('paste', (e) => {
      if (e.target.id !== 'cmp-text' || cmp.mode === 'readonly') return;
      const cd = e.clipboardData;
      const images = Array.from((cd && cd.files) || []).filter((f) => f.type.startsWith('image/'));
      // Spreadsheets and office apps put a picture of the cells next to the text: paste the text.
      if (!images.length || clipboardHasText(cd)) return;
      e.preventDefault();
      addFiles(cmp, images);
    });

    // Drag & drop of files onto the media card.
    const isFileDrag = (e) => Array.from((e.dataTransfer && e.dataTransfer.types) || []).includes('Files');
    root.addEventListener('dragover', (e) => {
      const card = e.target.closest('#cmp-media-card');
      if (!card || !isFileDrag(e) || cmp.mode === 'readonly') return;
      e.preventDefault();
      card.classList.add('is-drag');
    });
    root.addEventListener('dragleave', (e) => {
      const card = e.target.closest('#cmp-media-card');
      if (card && !card.contains(e.relatedTarget)) card.classList.remove('is-drag');
    });
    root.addEventListener('drop', (e) => {
      const card = e.target.closest('#cmp-media-card');
      if (!card || !isFileDrag(e)) return;
      e.preventDefault();
      card.classList.remove('is-drag');
      if (e.dataTransfer.files && e.dataTransfer.files.length) addFiles(cmp, e.dataTransfer.files);
    });

    // Preview interactions: reveal spoilers, expand/collapse expandable quotes, play videos.
    root.addEventListener('click', (e) => {
      const video = e.target.closest('.tg-single');
      if (video) {
        const v = $('video', video);
        if (v && !v.autoplay) {
          if (v.paused) v.play().catch(() => {});
          else v.pause();
          video.classList.toggle('is-playing', !v.paused);
        }
        return;
      }
      const textEl = e.target.closest('#tg-text');
      if (!textEl) return;
      const spoiler = e.target.closest('tg-spoiler, .tg-spoiler');
      if (spoiler && !spoiler.classList.contains('revealed')) {
        e.preventDefault();
        spoiler.classList.add('revealed');
        return;
      }
      const quote = e.target.closest('blockquote[expandable]');
      if (quote && !e.target.closest('a')) {
        const open = quote.classList.toggle('open');
        quote.setAttribute('aria-expanded', String(open));
      }
    });

    cmp.shortcut = () => {
      const action = composerShortcutAction(cmp);
      const btn = action && actionBtn(action);
      if (btn && !btn.disabled) btn.click();
    };
  }

  // ---------------------------------------------------------------- publish report

  function showReport(report, title = 'Результат публикации', note = '') {
    if (!report) return;
    const results = Array.isArray(report.results) ? report.results : [];
    const ok = Number.isFinite(Number(report.ok_count)) ? Number(report.ok_count) : results.filter((x) => x.ok).length;
    const fail = Number.isFinite(Number(report.fail_count)) ? Number(report.fail_count) : results.filter((x) => !x.ok).length;
    const total = ok + fail;
    let head;
    if (!total) head = `<div class="report-head is-warn">${icon('info')}<span>Нет каналов для отправки</span></div>`;
    else if (!fail) head = `<div class="report-head is-ok">${icon('check')}<span>Успешно: ${ok} из ${total}</span></div>`;
    else if (!ok) head = `<div class="report-head is-fail">${icon('alert')}<span>Не удалось: ${fail} из ${total}</span></div>`;
    else head = `<div class="report-head is-warn">${icon('alert')}<span>Успешно: ${ok} из ${total}, ошибок: ${fail}</span></div>`;
    const items = results.map((x) => {
      const href = safeHref(x.link);
      const name = esc(x.title || `Канал ${x.channel_id}`);
      const body = x.ok
        ? (href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">Открыть пост${icon('external')}</a>` : '<span class="muted">готово</span>')
        : `<span class="report-error">${esc(x.error || 'Ошибка')}</span>`;
      const note = x.note ? `<div class="report-note">${esc(x.note)}</div>` : '';
      return `<li class="report-item ${x.ok ? 'is-ok' : 'is-fail'}">${icon(x.ok ? 'check' : 'alert')}<div><b>${name}</b><div>${body}</div>${note}</div></li>`;
    }).join('');
    openModal({
      title,
      body: `${head}${items ? `<ul class="report-list">${items}</ul>` : ''}${note ? `<p class="hint">${esc(note)}</p>` : ''}`,
      actions: [
        { label: 'К списку постов', value: 'posts', kind: 'secondary' },
        { label: 'Готово', value: 'ok', kind: 'primary', autofocus: true },
      ],
    }).then((value) => {
      if (value === 'posts') location.hash = '#/posts?status=sent';
    });
  }

  // ======================================================================= 8d. Posts page

  const POST_TABS = [
    ['scheduled', 'Запланированные'],
    ['sent', 'Опубликованные'],
    ['draft', 'Черновики'],
    ['all', 'Все'],
  ];
  const POSTS_PAGE = 20;

  async function pagePosts(root, r, ctx) {
    setPageTitle('Посты');
    let status = r.query.get('status');
    if (!POST_TABS.some(([key]) => key === status)) status = storageGet('pcb.postsTab') || 'all';
    if (!POST_TABS.some(([key]) => key === status)) status = 'all';
    storageSet('pcb.postsTab', status);
    replaceHash(`#/posts?status=${status}`);

    const tabs = POST_TABS.map(([key, label]) => `<a class="tab" href="#/posts?status=${key}"${key === status ? ' aria-current="page"' : ''}>${esc(label)}</a>`).join('');
    root.innerHTML = `
      ${pageHead('Посты', { actions: `<a class="btn btn-secondary" href="#/plan">${icon('calendar')}<span>Контент-план</span></a>${newPostButton()}` })}
      <nav class="tabs" aria-label="Фильтр постов">${tabs}</nav>
      ${status === 'scheduled' ? `<p class="note">${icon('info')}<span>Сначала — ближайшие публикации. Время и порядок выхода по каналам удобнее менять в контент-плане.</span></p>` : ''}
      <div class="posts-list" id="posts-list" aria-live="polite">${loadingHtml(2)}</div>
      <div class="list-more" id="posts-more"></div>`;
    const listEl = $('#posts-list', root);
    const moreEl = $('#posts-more', root);
    let offset = 0;
    let total = 0;
    let posts = [];

    async function load(reset) {
      if (reset) {
        offset = 0;
        posts = [];
      }
      const [data] = await Promise.all([
        api.get(`api/posts?status=${status}&limit=${POSTS_PAGE}&offset=${offset}`),
        ensureServerTz(),
      ]);
      if (!ctx.alive()) return;
      const chunk = Array.isArray(data.posts) ? data.posts : [];
      total = Number(data.total) || 0;
      posts = posts.concat(chunk);
      offset += chunk.length;
      renderList();
    }

    function renderList() {
      if (!posts.length) {
        listEl.innerHTML = postsEmptyHtml(status);
        moreEl.innerHTML = '';
        return;
      }
      listEl.innerHTML = posts.map(postCardHtml).join('');
      moreEl.innerHTML = `<span class="muted small">Показано ${posts.length} из ${Math.max(total, posts.length)}</span>${
        posts.length < total ? '<button type="button" class="btn btn-secondary" data-action="more">Показать ещё</button>' : ''}`;
    }

    const reload = () => load(true).catch(handleError);
    const findPost = (el) => posts.find((p) => num(p.id) === num(el.closest('[data-post]').dataset.post));

    onAction(root, {
      more: (el) => busy(el, () => load(false)),
      refresh: () => reload(),
      publish: async (el) => {
        const p = findPost(el);
        if (!p) return;
        const targets = (p.targets || []).filter((t) => t.status !== 'sent' && t.status !== 'deleted');
        if (!targets.length) {
          toast('У поста не выбраны каналы — откройте его в редакторе', 'error');
          return;
        }
        const ok = await confirmModal({
          title: p.status === 'scheduled' ? 'Опубликовать сейчас?' : 'Опубликовать пост?',
          html: `<p>Пост сразу выйдет в ${targets.length} ${plural(targets.length, CHANNELS_IN_FORMS)}${p.status === 'scheduled' ? ', не дожидаясь расписания' : ''}:</p><ul class="modal-list">${targets.map((t) => `<li>${avatarHtml(t.channel_id, targetTitle(t), 'xs')}<span>${esc(targetTitle(t))}</span></li>`).join('')}</ul>`,
          confirmLabel: 'Опубликовать',
          confirmIcon: 'send',
          danger: true,
        });
        if (!ok) return;
        await busy(el, async () => {
          try {
            const res = await sendIdempotent('POST', `api/posts/${num(p.id)}/publish`);
            showReport(res.report);
          } finally {
            await reload();
          }
        });
      },
      unschedule: (el) => busy(el, async () => {
        const p = findPost(el);
        await api.post(`api/posts/${num(p.id)}/unschedule`);
        toast(hasSentTarget(p) ? 'Пост убран из плана — в остальные каналы он не выйдет' : 'Расписание отменено — пост перемещён в черновики', 'success');
        await reload();
      }),
      'delete-channels': async (el) => {
        const p = findPost(el);
        const ok = await confirmModal(deleteChannelsDialog(p));
        if (!ok) return;
        await busy(el, async () => {
          try {
            const res = await api.del(`api/posts/${num(p.id)}?from_channels=1`);
            if (res.report) showReport(res.report, 'Удаление из каналов');
            else toast('Пост удалён из каналов', 'success');
          } finally {
            await reload();
          }
        });
      },
      delete: async (el) => {
        const p = findPost(el);
        const ok = await confirmModal({
          title: 'Удалить запись?',
          message: p.status === 'deleted'
            ? 'Запись об удалённом посте исчезнет из панели.'
            : 'Пост будет удалён из панели без возможности восстановления.',
          confirmLabel: 'Удалить',
          danger: true,
        });
        if (!ok) return;
        await busy(el, async () => {
          await api.del(`api/posts/${num(p.id)}`);
          toast('Пост удалён', 'success');
          await reload();
        });
      },
    });

    await load(true);
  }

  /** «Удалить из каналов»: the confirmation. The server also takes the post out of the plan of the
   * channels still waiting for it (review2 #15) — the dialog says so, the way the bot does. */
  function deleteChannelsDialog(p) {
    const count = ((p && p.targets) || []).filter((t) => t.status === 'sent').length;
    const waiting = waitingTargets(p);
    const names = waiting.map((t) => `«${t.title || t.username || t.channel_id}»`).join(', ');
    return {
      title: 'Удалить пост из каналов?',
      message: `Сообщение будет удалено в ${count} ${plural(count, CHANNELS_IN_FORMS)}. Вернуть его не получится — только опубликовать заново.`,
      html: waiting.length ? `<p>${esc(`Публикация в ${names} по расписанию тоже будет отменена.`)}</p>` : '',
      confirmLabel: 'Удалить из каналов',
      danger: true,
    };
  }

  function postsEmptyHtml(status) {
    const texts = {
      scheduled: ['Нет запланированных постов', 'Поставьте пост в очередь по расписанию канала или укажите время — он выйдет автоматически.'],
      sent: ['Пока ничего не опубликовано', 'Опубликованные посты появятся здесь — их можно будет редактировать прямо в каналах.'],
      draft: ['Черновиков нет', 'Сохраняйте незаконченные посты как черновики и возвращайтесь к ним позже.'],
      all: ['Постов ещё нет', 'Создайте первый пост: текст с разметкой, фото или видео и цветные кнопки.'],
    };
    const [title, text] = texts[status] || texts.all;
    return emptyState('posts', title, esc(text), newPostButton('Написать пост'));
  }

  function postActionsHtml(p) {
    const id = num(p.id);
    const open = (label) => `<a class="btn btn-secondary btn-sm" href="#/compose/${id}">${icon('edit')}<span>${label}</span></a>`;
    const dup = `<a class="btn btn-ghost btn-sm" href="#/compose?copy=${id}">${icon('copy')}<span>Дублировать</span></a>`;
    const btn = (action, label, iconName, cls = 'btn-secondary') => `<button type="button" class="btn ${cls} btn-sm" data-action="${action}">${icon(iconName)}<span>${label}</span></button>`;
    const del = btn('delete', 'Удалить', 'trash', 'btn-ghost is-danger');
    const planTarget = (p.targets || []).find((t) => t.status === 'pending' && t.scheduled_at);
    const plan = planTarget ? `<a class="btn btn-ghost btn-sm" href="#/plan/${num(planTarget.channel_id)}">${icon('calendar')}<span>В плане</span></a>` : '';
    switch (p.status) {
      case 'draft':
        return [open('Открыть'), btn('publish', 'Опубликовать', 'send', 'btn-primary'), dup, del];
      case 'scheduled':
        if (hasSentTarget(p)) {
          return [open('Редактировать'), btn('publish', 'Опубликовать остальные сейчас', 'send', 'btn-primary'), plan, btn('unschedule', 'Убрать из плана', 'x'), dup, btn('delete-channels', 'Удалить из каналов', 'trash', 'btn-ghost is-danger')];
        }
        return [open('Редактировать'), btn('publish', 'Опубликовать сейчас', 'send', 'btn-primary'), plan, btn('unschedule', 'Отменить расписание', 'x'), dup, del];
      case 'sent':
        return [open('Редактировать'), dup, btn('delete-channels', 'Удалить из каналов', 'trash', 'btn-ghost is-danger')];
      case 'partial':
        return [open('Редактировать'), btn('publish', 'Допубликовать', 'refresh'), dup, btn('delete-channels', 'Удалить из каналов', 'trash', 'btn-ghost is-danger')];
      case 'failed':
        return [open('Открыть'), btn('publish', 'Повторить', 'refresh', 'btn-primary'), dup, del];
      case 'deleted':
        // Still shown in some channel (left by an older version): remove it there, not the record.
        return hasSentTarget(p) ? [open('Открыть'), dup, btn('delete-channels', 'Удалить из каналов', 'trash', 'btn-ghost is-danger')] : [open('Открыть'), dup, del];
      case 'sending':
        return [btn('refresh', 'Обновить', 'refresh')];
      default:
        return [open('Открыть'), dup];
    }
  }

  /** "15.10 09:00" — compact per-channel time for chips. */
  function fmtShort(ts) {
    const p = zonedParts(ts);
    return `${pad2(p.d)}.${pad2(p.m)} ${p.time}`;
  }

  function targetChipHtml(t) {
    const [label] = TARGET_STATUS[t.status] || [t.status || ''];
    const href = safeHref(t.link);
    const planned = t.status === 'pending' && t.scheduled_at;
    const when = planned ? `в плане на ${fmtSlot(t.scheduled_at)}` : t.status === 'sent' && t.sent_at ? `опубликован ${fmtDateTime(t.sent_at)}` : '';
    const title = [label, when, t.error].filter(Boolean).join(' · ');
    const time = planned ? `<span class="tchip-time">${icon('clock')}${esc(fmtShort(t.scheduled_at))}</span>` : '';
    const inner = `<span class="dot" aria-hidden="true"></span><span class="tchip-text">${esc(targetTitle(t))}</span>${time}${href ? icon('external') : ''}<span class="sr-only"> — ${esc([label, when].filter(Boolean).join(', '))}</span>`;
    const cls = `tchip t-${esc(t.status)}${planned ? ' is-planned' : ''}`;
    return href
      ? `<a class="${cls}" href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="${esc(title)}">${inner}</a>`
      : `<span class="${cls}" title="${esc(title)}">${inner}</span>`;
  }

  function postCardHtml(p) {
    const targets = p.targets || [];
    const media = postMedia(p);
    const reactions = targets.reduce((sum, t) => sum + (Number(t.reactions_total) || 0), 0);
    const buttonsCount = normalizeRows(p.buttons).reduce((n, row) => n + row.length, 0);
    const extras = [];
    if (media.length) extras.push(`<span class="post-extra" title="Медиа">${icon(media.length > 1 ? 'album' : media[0].type === 'photo' ? 'image' : 'video')}${esc(describeMedia(media))}</span>`);
    if (reactions) extras.push(`<span class="post-extra" title="Реакции">${icon('heart')}${fmtNum(reactions)}</span>`);
    if (buttonsCount) extras.push(`<span class="post-extra" title="Кнопки">${icon('link')}${buttonsCount}</span>`);
    return `<article class="card post-card" data-post="${num(p.id)}">
      <a class="post-thumb-link" href="#/compose/${num(p.id)}" aria-label="Открыть пост #${num(p.id)}">${mediaThumbHtml(media)}</a>
      <div class="post-body">
        <div class="post-meta">${statusBadge(p.status)}<span>${esc(postTimeText(p))}</span><span class="muted">#${num(p.id)}</span>${extras.join('')}</div>
        <p class="post-excerpt">${esc(postExcerpt(p))}</p>
        ${targets.length ? `<div class="tchips">${targets.map(targetChipHtml).join('')}</div>` : '<p class="hint">Каналы не выбраны</p>'}
        ${p.error && (p.status === 'failed' || p.status === 'partial') ? `<p class="post-error">${icon('alert')}<span>${esc(p.error)}</span></p>` : ''}
        <div class="post-actions">${postActionsHtml(p).filter(Boolean).join('')}</div>
      </div>
    </article>`;
  }

  // ======================================================================= 8e. Content plan page

  const PLAN_DAYS = 14;
  const TIMELINE_DAYS = 7;
  const DTF_DAY_MONTH = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', timeZone: 'UTC' });

  /** "Пн, 14 октября" + «Сегодня»/«Завтра» badge for a plan day ('YYYY-MM-DD' in the bot's zone). */
  function planDayTitle(date, weekday) {
    const today = zonedParts(nowTs());
    const tomorrow = zonedParts(nowTs() + 86400);
    const key = (p) => `${p.y}-${pad2(p.m)}-${pad2(p.d)}`;
    const wd = weekday || WEEKDAYS[(isoDay(date).getUTCDay() + 6) % 7];
    let tag = '';
    if (date === key(today)) tag = '<span class="badge b-accent">Сегодня</span>';
    else if (date === key(tomorrow)) tag = '<span class="badge">Завтра</span>';
    return `${esc(wd)}, ${esc(DTF_DAY_MONTH.format(isoDay(date)))}${tag}`;
  }

  const activeGroupsOf = (groups) => groups
    .map((g) => ({ folder: g.folder, channels: g.channels.filter((c) => c.status === 'active') }))
    .filter((g) => g.channels.length);

  function planNavHtml(groups, current) {
    const foldered = groups.some((g) => g.folder);
    const item = (c) => {
      const cur = num(c.id) === current;
      return `<a class="plan-nav-item${cur ? ' is-current' : ''}" href="#/plan/${num(c.id)}"${cur ? ' aria-current="page"' : ''}>
        ${avatarHtml(c.id, c.title, 'xs')}
        <span class="plan-nav-text"><b>${esc(c.title || 'Без названия')}</b><span>${esc(channelSlotsSummary(c))}</span></span>
      </a>`;
    };
    const all = `<a class="plan-nav-item${current ? '' : ' is-current'}" href="#/plan"${current ? '' : ' aria-current="page"'}>
      <span class="plan-nav-icon">${icon('calendar')}</span>
      <span class="plan-nav-text"><b>Все каналы</b><span>общая лента публикаций</span></span>
    </a>`;
    return all + groups.map((g) => `${foldered ? `<div class="plan-nav-folder">${icon('folder')}<span>${esc(g.folder ? g.folder.name : 'Без папки')}</span></div>` : ''}${g.channels.map(item).join('')}`).join('');
  }

  function planSelectHtml(groups, current) {
    const foldered = groups.some((g) => g.folder);
    const opt = (c) => `<option value="${num(c.id)}"${num(c.id) === current ? ' selected' : ''}>${esc(c.title || 'Без названия')}</option>`;
    return `<option value=""${current ? '' : ' selected'}>Все каналы</option>${groups.map((g) => (foldered
      ? `<optgroup label="${esc(g.folder ? g.folder.name : 'Без папки')}">${g.channels.map(opt).join('')}</optgroup>`
      : g.channels.map(opt).join(''))).join('')}`;
  }

  async function pagePlan(root, r, ctx) {
    setPageTitle('Контент-план');
    const [data] = await Promise.all([loadGrouped(), ensureServerTz()]);
    if (!ctx.alive()) return;
    const groups = activeGroupsOf(data.groups);
    const active = groups.flatMap((g) => g.channels);
    const head = pageHead('Контент-план', {
      subtitle: 'Расписание публикаций каналов: свободные слоты и очередь постов',
      actions: newPostButton(),
    });
    if (!active.length) {
      root.innerHTML = `${head}${emptyState('calendar', 'Нет каналов для плана', 'Подключите канал — затем настройте его расписание и ставьте посты в очередь.', '<a class="btn btn-primary" href="#/channels">Перейти к каналам</a>')}`;
      return;
    }
    let channelId = r.segs[1] ? num(r.segs[1]) : 0;
    if (channelId && !active.some((c) => num(c.id) === channelId)) {
      toast('Канал не найден или не подключён — показан общий план', 'info');
      channelId = 0;
      replaceHash('#/plan');
    }
    const ch = channelId ? active.find((c) => num(c.id) === channelId) : null;
    if (ch) setPageTitle(`План: ${ch.title || 'канал'}`);

    root.innerHTML = `${head}
      <div class="plan-layout">
        <nav class="card plan-side" aria-label="Каналы контент-плана">${planNavHtml(groups, channelId)}</nav>
        <div class="plan-pick">
          <label class="field"><span class="label">Канал</span>
            <select class="input" id="plan-channel">${planSelectHtml(groups, channelId)}</select></label>
        </div>
        <div class="plan-main" id="plan-main">${loadingHtml(3)}</div>
      </div>`;
    $('#plan-channel', root).addEventListener('change', (e) => {
      location.hash = e.target.value ? `#/plan/${num(e.target.value)}` : '#/plan';
    });
    const main = $('#plan-main', root);
    if (ch) await channelPlanView(main, ch, ctx);
    else await timelineView(main, ctx, data.folders, r.query.get('folder'));
  }

  /** Moves a planned post to another time: free slots of the channel or any date and time. */
  function movePostModal({ post, channelId, ts, freeSlots = [] }) {
    const body = document.createElement('div');
    const chips = freeSlots.filter((s) => s > nowTs() + 60 && s !== ts).slice(0, 8)
      .map((s) => `<button type="button" class="slot-chip" data-slot="${s}" aria-pressed="false">${esc(fmtSlot(s))}</button>`).join('');
    body.innerHTML = `<p>«${esc(post.excerpt || 'Пост')}» — сейчас ${esc(fmtSlotWhen(ts))}.</p>
      ${chips ? `<div class="field"><span class="label" id="mv-free">Свободные слоты</span><div class="slot-chips" role="group" aria-labelledby="mv-free">${chips}</div></div>` : ''}
      <label class="field"><span class="label">${chips ? 'Или точное время' : 'Новое время'}</span>
        <input type="datetime-local" class="input" data-move-when step="60" min="${esc(toZonedInput(nowTs()))}"></label>
      <p class="hint">${esc(tzHint())} Если это время уже занято другим постом, они поменяются местами.</p>`;
    const input = $('[data-move-when]', body);
    input.value = toZonedInput(ts);
    let picked = { ts: 0, queued: false };
    body.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-slot]');
      if (!chip) return;
      picked = { ts: num(chip.dataset.slot), queued: true };
      input.value = toZonedInput(picked.ts);
      $$('[data-slot]', body).forEach((c) => c.setAttribute('aria-pressed', String(c === chip)));
    });
    input.addEventListener('input', () => {
      picked = { ts: 0, queued: false };
      $$('[data-slot]', body).forEach((c) => c.setAttribute('aria-pressed', 'false'));
    });
    return openModal({
      title: 'Перенести пост',
      body,
      actions: [
        { label: 'Отмена', value: 'cancel' },
        { label: 'Перенести', value: 'ok', kind: 'primary', submit: true, icon: 'move' },
      ],
      onSubmit: async () => {
        const target = picked.ts || fromZonedInput(input.value);
        if (!target) throw new ValidationError('Выберите слот или укажите дату и время', '[data-move-when]');
        if (target <= nowTs() + 30) throw new ValidationError('Это время уже прошло — выберите время в будущем', '[data-move-when]');
        if (target === num(ts)) throw new ValidationError('Пост уже стоит на это время — выберите другое', '[data-move-when]');
        // Picking a schedule slot keeps the post in the channel's queue (it moves on compaction);
        // an explicit time pins it.
        await api.post('api/plan/move', { post_id: num(post.id), channel_id: num(channelId), ts: target, queued: picked.queued });
        return target;
      },
    });
  }

  /** «Опубликовать сейчас» for one channel of a planned post; the other channels keep their time. */
  async function publishPlannedNow(post, channel) {
    const ok = await confirmModal({
      title: 'Опубликовать сейчас?',
      html: `<p>«${esc(post.excerpt || 'Пост')}» выйдет в канале «${esc(channel.title || 'Без названия')}» сразу, не дожидаясь своего времени.</p><p class="muted small">В других каналах пост выйдет по своему расписанию.</p>`,
      confirmLabel: 'Опубликовать',
      confirmIcon: 'send',
      danger: true,
    });
    if (!ok) return false;
    const res = await sendIdempotent('POST', `api/posts/${num(post.id)}/publish`, { channel_ids: [num(channel.id)] });
    showReport(res.report);
    return true;
  }

  function planBadgesHtml(it) {
    const p = it.post || {};
    const out = [];
    const kind = describeKind(p.media_kind, p.media_count);
    if (kind) out.push(`<span class="badge">${esc(kind)}</span>`);
    if (p.status === 'sending') out.push('<span class="badge b-info">публикуется</span>');
    else if (num(it.ts) <= nowTs()) out.push('<span class="badge b-warning">выходит сейчас</span>');
    out.push(p.queued
      ? '<span class="badge b-accent" title="Время взято из расписания канала">в очереди</span>'
      : '<span class="badge" title="Время указано вручную — «Уплотнить очередь» его не сдвинет">точное время</span>');
    if (p.has_buttons) out.push(`<span class="post-extra" title="С кнопками">${icon('link')}</span>`);
    return out.join('');
  }

  function planItemHtml(it, { channel = null, first = false, showChannel = false } = {}) {
    const p = it.post || {};
    const id = num(p.id);
    const due = num(it.ts) <= nowTs() + 60 || p.status === 'sending';
    const tools = due ? '' : `
      ${showChannel ? '' : `<button type="button" class="icon-btn sm" data-action="plan-up" aria-label="Раньше — поменять местами с предыдущим постом"${first ? ' disabled' : ''}>${icon('up')}</button>
      <button type="button" class="icon-btn sm" data-action="plan-down" aria-label="Позже — поменять местами со следующим постом">${icon('down')}</button>`}
      <button type="button" class="btn btn-ghost btn-sm" data-action="plan-move">${icon('move')}<span>Перенести</span></button>
      <button type="button" class="btn btn-ghost btn-sm" data-action="plan-publish">${icon('send')}<span>Опубликовать сейчас</span></button>
      <button type="button" class="btn btn-ghost btn-sm is-danger" data-action="plan-unqueue">${icon('x')}<span>Убрать из плана</span></button>`;
    const chLine = showChannel && channel
      ? `<a class="plan-item-ch" href="#/plan/${num(channel.id)}">${avatarHtml(channel.id, channel.title, 'xs')}<span>${esc(channel.title || 'Канал')}</span></a>`
      : '';
    return `<article class="plan-item${due ? ' is-due' : ''}"${!due && !showChannel ? ' draggable="true"' : ''} data-post="${id}" data-ts="${num(it.ts)}"${channel ? ` data-channel="${num(channel.id)}"` : ''}>
      ${!due && !showChannel ? `<span class="plan-grip" title="Перетащите на другой слот" aria-hidden="true">${icon('grip')}</span>` : ''}
      ${summaryThumbHtml(p)}
      <div class="plan-item-main">
        ${chLine}
        <p class="plan-excerpt">${esc(p.excerpt || '(без текста)')}</p>
        <div class="plan-item-meta">${planBadgesHtml(it)}</div>
      </div>
      <div class="plan-item-actions">${tools}
        <a class="btn btn-ghost btn-sm" href="#/compose/${id}">${icon('edit')}<span>Открыть</span></a>
      </div>
    </article>`;
  }

  async function channelPlanView(main, ch, ctx) {
    let days = PLAN_DAYS;
    let plan = null;
    let drag = null;
    let refocus = null; // {post, action} to focus after a reload

    async function load() {
      const res = await api.get(`api/plan/channel/${num(ch.id)}?days=${days}`);
      if (!ctx.alive()) return;
      plan = res && typeof res === 'object' ? res : {};
      if (plan.channel && typeof plan.channel === 'object') Object.assign(ch, plan.channel);
      render();
    }

    const reload = () => load().catch(handleError);
    const allItems = () => (Array.isArray(plan.days) ? plan.days : []).flatMap((d) => (Array.isArray(d.items) ? d.items : []));
    const freeSlots = () => allItems().filter((it) => !it.post && it.slot).map((it) => num(it.ts));
    const findItem = (el) => {
      const card = el.closest('.plan-item');
      return card ? allItems().find((it) => it.post && num(it.post.id) === num(card.dataset.post) && num(it.ts) === num(card.dataset.ts)) : null;
    };

    function render() {
      const slots = plan.slots ? normSlots(plan.slots) : channelSlots(ch);
      // «Уплотнить очередь» depends on the schedule only: compaction moves every queued post of the
      // channel, also those beyond the loaded days — exactly when the near slots are all free.
      const hasSlots = slotsCount(slots) > 0;
      const summary = plan.slots_summary || slotsSummary(slots);
      const items = allItems();
      const firstFuture = items.find((it) => it.post && num(it.ts) > nowTs() + 60 && it.post.status !== 'sending');
      const planDays = Array.isArray(plan.days) ? plan.days : [];

      const dayHtml = (day) => {
        const list = Array.isArray(day.items) ? day.items : [];
        const title = planDayTitle(day.date, day.weekday);
        if (!list.length) {
          return `<section class="plan-day is-empty"><h3 class="plan-day-title">${title}</h3><p class="plan-day-empty">Нет слотов</p></section>`;
        }
        const rows = list.map((it) => {
          const ts = num(it.ts);
          const time = esc(it.time || zonedParts(ts).time);
          if (!it.post) {
            return `<li class="plan-slot is-free" data-ts="${ts}">
              <span class="plan-time">${time}</span>
              <a class="plan-free" href="#/compose?channel=${num(ch.id)}&amp;at=${ts}" aria-label="${time} — свободно. Новый пост в этот слот">
                <span class="plan-free-label">свободно</span><span class="plan-free-add">${icon('plus')}<span>Новый пост в этот слот</span></span></a>
            </li>`;
          }
          const first = firstFuture && firstFuture === it;
          return `<li class="plan-slot${it.slot ? '' : ' is-extra'}" data-ts="${ts}">
            <span class="plan-time">${time}</span>
            ${planItemHtml(it, { channel: ch, first })}
          </li>`;
        }).join('');
        return `<section class="plan-day"><h3 class="plan-day-title">${title}</h3><ol class="plan-slots">${rows}</ol></section>`;
      };

      main.innerHTML = `
        <header class="card plan-head">
          <div class="plan-head-main">
            ${avatarHtml(ch.id, ch.title)}
            <div class="plan-head-text">
              <h2>${esc(ch.title || 'Без названия')}</h2>
              <p class="plan-head-sub">${icon('calendar')}<span>${esc(summary)}</span></p>
            </div>
          </div>
          <div class="plan-head-actions">
            <button type="button" class="btn btn-secondary btn-sm" data-action="plan-slots">${icon('calendar')}<span>Расписание</span></button>
            <button type="button" class="btn btn-secondary btn-sm" data-action="plan-compact"${hasSlots ? '' : ' disabled'}>${icon('compact')}<span>Уплотнить очередь</span></button>
            <a class="btn btn-primary btn-sm" href="#/compose?channel=${num(ch.id)}&amp;mode=queue">${icon('plus')}<span>Пост в очередь</span></a>
          </div>
        </header>
        ${hasSlots ? '' : `<div class="callout is-warn">${icon('info')}<div>У канала нет расписания публикаций. Настройте его — здесь появятся свободные слоты, а посты можно будет ставить в очередь.
          <div class="callout-actions"><button type="button" class="btn btn-primary btn-sm" data-action="plan-slots">${icon('calendar')}<span>Настроить расписание</span></button></div></div></div>`}
        <p class="note">${icon('info')}<span>Перетащите пост на другой слот, чтобы перенести его, или используйте кнопки ↑ ↓ и «Перенести».${tzDiffers() ? ` Время — по часовому поясу бота (${esc(state.serverTz)}).` : ''}</span></p>
        <div class="plan-days">${planDays.map(dayHtml).join('')}</div>
        <div class="list-more"><button type="button" class="btn btn-secondary" data-action="plan-more">Показать ещё ${PLAN_DAYS} дней</button></div>`;

      if (refocus) {
        const card = $(`.plan-item[data-post="${refocus.post}"]`, main);
        const target = card && ($(`[data-action="${refocus.action}"]:not([disabled])`, card) || $('button:not([disabled]), a', card));
        if (target) target.focus();
        refocus = null;
      }
    }

    onAction(main, {
      'plan-more': (el) => busy(el, async () => {
        days = Math.min(days + PLAN_DAYS, 366);
        await load();
      }),
      'plan-slots': async () => {
        const updated = await editChannelSchedule(ch);
        if (updated) {
          Object.assign(ch, updated);
          await reload();
        }
      },
      'plan-compact': async (el) => {
        const ok = await confirmModal({
          title: 'Уплотнить очередь?',
          message: 'Посты из очереди займут ближайшие свободные слоты расписания, их порядок сохранится. Посты с точным временем останутся на месте.',
          confirmLabel: 'Уплотнить',
          confirmIcon: 'compact',
        });
        if (!ok) return;
        await busy(el, async () => {
          const res = await api.post('api/plan/compact', { channel_id: num(ch.id) });
          const moved = num(res && res.moved);
          toast(moved ? `Перенесено постов: ${moved}` : 'Очередь уже плотная — переносить нечего', moved ? 'success' : 'info');
          await load();
        });
      },
      'plan-up': (el) => shift(el, -1),
      'plan-down': (el) => shift(el, 1),
      'plan-move': async (el) => {
        const it = findItem(el);
        if (!it) return;
        const res = await movePostModal({ post: it.post, channelId: ch.id, ts: num(it.ts), freeSlots: freeSlots() });
        if (!res || res === 'cancel') return;
        toast(`Пост перенесён на ${fmtSlotWhen(res)}`, 'success');
        refocus = { post: num(it.post.id), action: 'plan-move' };
        await reload();
      },
      'plan-publish': async (el) => {
        const it = findItem(el);
        if (!it) return;
        await busy(el, async () => {
          try {
            await publishPlannedNow(it.post, ch);
          } finally {
            await reload();
          }
        });
      },
      'plan-unqueue': (el) => busy(el, async () => {
        const it = findItem(el);
        if (!it) return;
        await api.post('api/plan/unqueue', { post_id: num(it.post.id), channel_id: num(ch.id) });
        toast('Пост убран из плана канала — его можно найти в постах', 'success');
        await load();
      }),
    });

    async function shift(el, direction) {
      const it = findItem(el);
      if (!it) return;
      await busy(el, async () => {
        const res = await api.post('api/plan/shift', { post_id: num(it.post.id), channel_id: num(ch.id), direction });
        const moved = !(res && (res.moved === false || res.shifted === false || res.ok === false || res.result === false));
        if (!moved) toast(direction < 0 ? 'Раньше в плане постов нет' : 'Дальше в плане постов нет', 'info');
        refocus = { post: num(it.post.id), action: direction < 0 ? 'plan-up' : 'plan-down' };
        await load();
      });
    }

    // HTML5 drag-and-drop of planned posts onto another slot (free or taken: posts swap).
    const clearDrag = () => {
      $$('.is-over', main).forEach((x) => x.classList.remove('is-over'));
      $$('.is-dragging', main).forEach((x) => x.classList.remove('is-dragging'));
      main.classList.remove('is-dragging-plan');
    };
    main.addEventListener('dragstart', (e) => {
      const card = e.target.closest && e.target.closest('.plan-item[draggable="true"]');
      if (!card) return;
      drag = { post: num(card.dataset.post), ts: num(card.dataset.ts) };
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', `Пост #${drag.post}`);
      card.classList.add('is-dragging');
      main.classList.add('is-dragging-plan');
    });
    main.addEventListener('dragend', () => {
      drag = null;
      clearDrag();
    });
    main.addEventListener('dragover', (e) => {
      if (!drag) return;
      const slot = e.target.closest('.plan-slot');
      if (!slot || num(slot.dataset.ts) === drag.ts || num(slot.dataset.ts) <= nowTs() + 60) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      if (!slot.classList.contains('is-over')) {
        $$('.is-over', main).forEach((x) => x.classList.remove('is-over'));
        slot.classList.add('is-over');
      }
    });
    main.addEventListener('dragleave', (e) => {
      const slot = e.target.closest && e.target.closest('.plan-slot');
      if (slot && !slot.contains(e.relatedTarget)) slot.classList.remove('is-over');
    });
    main.addEventListener('drop', async (e) => {
      if (!drag) return;
      const slot = e.target.closest('.plan-slot');
      if (!slot) return;
      e.preventDefault();
      const { post } = drag;
      const ts = num(slot.dataset.ts);
      drag = null;
      clearDrag();
      try {
        await api.post('api/plan/move', { post_id: post, channel_id: num(ch.id), ts, queued: !slot.classList.contains('is-extra') });
        toast(`Пост перенесён на ${fmtSlotWhen(ts)}`, 'success');
      } catch (err) {
        handleError(err);
      }
      refocus = { post, action: 'plan-move' };
      await reload();
    });

    await load();
  }

  async function timelineView(main, ctx, folders, folderParam) {
    let days = TIMELINE_DAYS;
    let folder = folderParam !== null && folderParam !== '' && (folderParam === '0' || folders.some((f) => String(num(f.id)) === folderParam)) ? folderParam : '';
    let items = [];

    async function load() {
      const res = await api.get(`api/plan/timeline?days=${days}${folder !== '' ? `&folder_id=${encodeURIComponent(folder)}` : ''}`);
      if (!ctx.alive()) return;
      items = Array.isArray(res.items) ? res.items : [];
      render();
    }
    const reload = () => load().catch(handleError);
    const findItem = (el) => {
      const card = el.closest('.plan-item');
      return card ? items.find((it) => it.post && num(it.post.id) === num(card.dataset.post) && num(it.ts) === num(card.dataset.ts)
        && num(it.channel && it.channel.id) === num(card.dataset.channel)) : null;
    };

    function render() {
      const filter = folders.length ? `<label class="field plan-filter"><span class="label">Папка</span>
        <select class="input input-sm" id="plan-folder">
          <option value=""${folder === '' ? ' selected' : ''}>Все папки</option>
          ${folders.map((f) => `<option value="${num(f.id)}"${folder === String(num(f.id)) ? ' selected' : ''}>${esc(f.name)}</option>`).join('')}
          <option value="0"${folder === '0' ? ' selected' : ''}>Без папки</option>
        </select></label>` : '';
      const byDay = [];
      items.forEach((it) => {
        const date = it.date || (() => { const p = zonedParts(it.ts); return `${p.y}-${pad2(p.m)}-${pad2(p.d)}`; })();
        let group = byDay[byDay.length - 1];
        if (!group || group.date !== date) {
          group = { date, weekday: it.weekday, items: [] };
          byDay.push(group);
        }
        group.items.push(it);
      });
      const list = byDay.map((g) => `<section class="plan-day"><h3 class="plan-day-title">${planDayTitle(g.date, g.weekday)}</h3>
        <ol class="plan-slots">${g.items.map((it) => `<li class="plan-slot" data-ts="${num(it.ts)}">
          <span class="plan-time">${esc(it.time || zonedParts(it.ts).time)}</span>
          ${planItemHtml(it, { channel: it.channel || {}, showChannel: true })}
        </li>`).join('')}</ol></section>`).join('');
      main.innerHTML = `
        <header class="card plan-head">
          <div class="plan-head-main">
            <span class="plan-head-icon">${icon('calendar')}</span>
            <div class="plan-head-text"><h2>Все каналы</h2>
              <p class="plan-head-sub"><span>${items.length ? `${items.length} ${plural(items.length, ['публикация', 'публикации', 'публикаций'])} за ${days} ${plural(days, ['день', 'дня', 'дней'])}` : `ближайшие ${days} ${plural(days, ['день', 'дня', 'дней'])}`}</span></p></div>
          </div>
          ${filter ? `<div class="plan-head-actions">${filter}</div>` : ''}
        </header>
        ${tzDiffers() ? `<p class="note">${icon('info')}<span>Время — по часовому поясу бота (${esc(state.serverTz)}).</span></p>` : ''}
        ${items.length
          ? `<div class="plan-days">${list}</div>`
          : emptyState('calendar', 'Ничего не запланировано', `На ближайшие ${days} ${plural(days, ['день', 'дня', 'дней'])} публикаций нет. Выберите канал слева, чтобы увидеть его свободные слоты, или поставьте пост в очередь.`, newPostButton('Написать пост'))}
        <div class="list-more"><button type="button" class="btn btn-secondary" data-action="plan-more">Показать ещё ${TIMELINE_DAYS} дней</button></div>`;
      const sel = $('#plan-folder', main);
      if (sel) {
        sel.addEventListener('change', () => {
          folder = sel.value;
          replaceHash(folder !== '' ? `#/plan?folder=${encodeURIComponent(folder)}` : '#/plan');
          main.innerHTML = loadingHtml(2);
          reload();
        });
      }
    }

    onAction(main, {
      'plan-more': (el) => busy(el, async () => {
        days = Math.min(days + TIMELINE_DAYS, 366);
        await load();
      }),
      'plan-move': async (el) => {
        const it = findItem(el);
        if (!it) return;
        const res = await movePostModal({ post: it.post, channelId: it.channel.id, ts: num(it.ts) });
        if (!res || res === 'cancel') return;
        toast(`Пост перенесён на ${fmtSlotWhen(res)}`, 'success');
        await reload();
      },
      'plan-publish': async (el) => {
        const it = findItem(el);
        if (!it) return;
        await busy(el, async () => {
          try {
            await publishPlannedNow(it.post, it.channel);
          } finally {
            await reload();
          }
        });
      },
      'plan-unqueue': (el) => busy(el, async () => {
        const it = findItem(el);
        if (!it) return;
        await api.post('api/plan/unqueue', { post_id: num(it.post.id), channel_id: num(it.channel.id) });
        toast('Пост убран из плана канала', 'success');
        await load();
      }),
    });

    await load();
  }

  // ======================================================================= 8f. Schedule editor

  let schedSeq = 0;

  /**
   * Weekly schedule editor (modal): 7 weekday rows with time chips, presets, «copy to all days»
   * and a text mode with the bot's syntax. `save(payload)` gets {slots: {"0": [...]}} or
   * {text} and returns the saved object; resolves with it, or null when cancelled.
   */
  async function openScheduleEditor({ title, subtitle = '', note = '', slots, save }) {
    const uid = ++schedSeq;
    let days = slots.map((list) => list.slice());
    let mode = 'visual';
    const body = document.createElement('div');
    body.className = 'sched';
    body.innerHTML = `
      ${subtitle ? `<p class="sched-sub">${esc(subtitle)}</p>` : ''}
      ${note ? `<p class="callout small">${icon('info')}<span>${esc(note)}</span></p>` : ''}
      <div class="segmented sched-modes" role="group" aria-label="Способ редактирования">
        <button type="button" class="seg" data-sched-mode="visual" aria-pressed="true">По дням</button>
        <button type="button" class="seg" data-sched-mode="text" aria-pressed="false">Текстом</button>
      </div>
      <div class="sched-visual">
        <div class="sched-presets" role="group" aria-label="Готовые варианты">
          <button type="button" class="btn btn-ghost btn-sm" data-preset="weekdays">Будни 09:00 и 18:00</button>
          <button type="button" class="btn btn-ghost btn-sm" data-preset="daily">Каждый день 10:00</button>
          <button type="button" class="btn btn-ghost btn-sm is-danger" data-preset="clear">Очистить</button>
        </div>
        <div class="sched-rows"></div>
      </div>
      <div class="sched-text" hidden>
        <label class="field"><span class="label">Расписание текстом — как в боте</span>
          <textarea class="input sched-textarea" rows="6" spellcheck="false" autocapitalize="off" placeholder="пн-пт 09:00, 18:00&#10;сб, вс 12:00"></textarea></label>
        <p class="field-error sched-text-error" role="status"></p>
        <p class="hint">По строке на правило: дни, затем время. Дни: пн вт ср чт пт сб вс, диапазоны (пн-пт), списки (сб, вс), «будни», «выходные», «ежедневно». Строка только со временем — каждый день.</p>
      </div>
      <p class="sched-total" role="status" aria-live="polite"></p>`;
    const rowsEl = $('.sched-rows', body);
    const totalEl = $('.sched-total', body);
    const textEl = $('.sched-textarea', body);
    const textErr = $('.sched-text-error', body);

    const announce = (msg = '') => {
      const n = slotsCount(days);
      totalEl.textContent = msg || (n ? `Публикаций в неделю: ${n}` : 'Расписание пустое — посты нельзя будет ставить в очередь');
    };

    function renderRows(focus = null) {
      rowsEl.innerHTML = days.map((list, d) => `
        <div class="sched-row" data-day="${d}">
          <span class="sched-day" id="sd-${uid}-${d}">${WEEKDAYS[d]}</span>
          <div class="sched-times" role="list" aria-labelledby="sd-${uid}-${d}">
            ${list.length ? list.map((t) => `<span class="time-chip" role="listitem">${esc(t)}<button type="button" data-remove="${esc(t)}" aria-label="Убрать ${esc(t)} для ${WEEKDAYS_GEN[d]}">${icon('x')}</button></span>`).join('') : '<span class="sched-none">нет публикаций</span>'}
          </div>
          <div class="sched-add">
            <input type="time" class="input input-sm" step="60" data-new-time aria-label="Новое время для ${WEEKDAYS_GEN[d]}">
            <button type="button" class="icon-btn sm" data-add aria-label="Добавить время для ${WEEKDAYS_GEN[d]}" title="Добавить">${icon('plus')}</button>
            <button type="button" class="icon-btn sm" data-copy aria-label="Скопировать время ${WEEKDAYS_GEN[d]} на все дни" title="Скопировать на все дни"${list.length ? '' : ' disabled'}>${icon('copy')}</button>
          </div>
        </div>`).join('');
      if (focus) {
        const row = $(`.sched-row[data-day="${focus.day}"]`, rowsEl);
        const el = row && ($(focus.sel, row) || $('[data-new-time]', row));
        if (el) el.focus();
      }
    }

    function addTime(day) {
      const row = $(`.sched-row[data-day="${day}"]`, rowsEl);
      const input = $('[data-new-time]', row);
      const t = cleanTime(input.value);
      if (!t) {
        announce('Укажите время, например 09:00');
        input.focus();
        return;
      }
      if (days[day].includes(t)) {
        announce(`${t} уже есть для ${WEEKDAYS_GEN[day]}`);
        input.focus();
        return;
      }
      days[day] = [...days[day], t].sort();
      renderRows({ day, sel: '[data-new-time]' });
      announce(`Добавлено ${t} для ${WEEKDAYS_GEN[day]}`);
    }

    function setMode(next) {
      if (next === mode) return;
      if (next === 'text') {
        textEl.value = slotsToText(days);
        textErr.textContent = '';
      } else {
        try {
          days = parseSlotsText(textEl.value);
        } catch (err) {
          textErr.textContent = `${err.message}. Исправьте текст или сохраните его как есть — бот проверит формат.`;
          textEl.focus();
          return;
        }
        renderRows();
      }
      mode = next;
      $('.sched-visual', body).hidden = mode !== 'visual';
      $('.sched-text', body).hidden = mode !== 'text';
      $$('[data-sched-mode]', body).forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.schedMode === mode)));
      if (mode === 'text') textEl.focus();
      announce();
    }

    body.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn || btn.disabled) return;
      const row = btn.closest('.sched-row');
      const day = row ? num(row.dataset.day) : -1;
      if (btn.dataset.schedMode) {
        setMode(btn.dataset.schedMode);
      } else if (btn.dataset.preset) {
        if (btn.dataset.preset === 'weekdays') days = ALL_DAYS.map((d) => (d < 5 ? ['09:00', '18:00'] : []));
        else if (btn.dataset.preset === 'daily') days = ALL_DAYS.map(() => ['10:00']);
        else days = ALL_DAYS.map(() => []);
        renderRows();
        announce();
      } else if (btn.dataset.remove !== undefined) {
        days[day] = days[day].filter((t) => t !== btn.dataset.remove);
        renderRows({ day, sel: '[data-remove]' });
        announce(`Убрано ${btn.dataset.remove} для ${WEEKDAYS_GEN[day]}`);
      } else if (btn.hasAttribute('data-add')) {
        addTime(day);
      } else if (btn.hasAttribute('data-copy')) {
        days = ALL_DAYS.map(() => days[day].slice());
        renderRows({ day, sel: '[data-copy]' });
        announce(`Время ${WEEKDAYS_GEN[day]} скопировано на все дни`);
      }
    });
    body.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.matches('[data-new-time]')) {
        e.preventDefault();
        addTime(num(e.target.closest('.sched-row').dataset.day));
      }
    });
    textEl.addEventListener('input', () => { textErr.textContent = ''; });

    renderRows();
    announce();
    const res = await openModal({
      title,
      body,
      wide: true,
      actions: [
        { label: 'Отмена', value: 'cancel' },
        { label: 'Сохранить', value: 'ok', kind: 'primary', submit: true, icon: 'check' },
      ],
      onSubmit: async () => {
        // A time typed in a row but not added with «+» would be lost silently — add it.
        $$('[data-new-time]', rowsEl).forEach((input) => {
          const t = cleanTime(input.value);
          const day = num(input.closest('.sched-row').dataset.day);
          if (mode === 'visual' && t && !days[day].includes(t)) days[day] = [...days[day], t].sort();
        });
        const payload = mode === 'text' ? { text: textEl.value } : { slots: slotsToApi(days) };
        const saved = await save(payload);
        return saved || true;
      },
    });
    return res && res !== 'cancel' ? res : null;
  }

  /** Schedule editor for one channel → the updated channel row (or null when cancelled). */
  async function editChannelSchedule(ch) {
    const res = await openScheduleEditor({
      title: 'Расписание публикаций',
      subtitle: `Канал «${ch.title || 'Без названия'}». Время — по часовому поясу бота${state.serverTz ? ` (${state.serverTz})` : ''}.`,
      slots: channelSlots(ch),
      save: async (payload) => {
        const data = await api.put(`api/channels/${num(ch.id)}/slots`, payload);
        const updated = unwrap(data, 'channel');
        return updated && typeof updated === 'object' ? updated : {};
      },
    });
    if (!res) return null;
    toast('Расписание сохранено', 'success');
    return res === true ? {} : res;
  }

  // ======================================================================= 8g. Channels page

  const COLLAPSED_KEY = 'pcb.foldersCollapsed';

  function readCollapsed() {
    try {
      const list = JSON.parse(storageGet(COLLAPSED_KEY) || '[]');
      return new Set(Array.isArray(list) ? list.map(String) : []);
    } catch {
      return new Set();
    }
  }

  async function pageChannels(root, r, ctx) {
    setPageTitle('Каналы');
    const [data] = await Promise.all([loadGrouped(), ensureServerTz()]);
    if (!ctx.alive()) return;
    const { groups, channels, folders } = data;
    const active = channels.filter((c) => c.status === 'active');
    const pending = channels.filter((c) => c.status === 'pending');
    const inactive = channels.filter((c) => c.status === 'left' || c.status === 'ignored');
    const byId = (id) => channels.find((c) => num(c.id) === num(id));
    const folderById = (id) => folders.find((f) => num(f.id) === num(id));
    const collapsed = readCollapsed();

    const folderSection = (g, idx) => {
      const f = g.folder;
      const fid = f ? num(f.id) : 0;
      const list = g.channels.filter((c) => c.status === 'active');
      if (!f && !list.length) return '';
      const name = f ? f.name : 'Без папки';
      const open = !collapsed.has(String(fid));
      const tools = f ? `<div class="folder-tools">
          <button type="button" class="btn btn-ghost btn-sm" data-action="folder-slots" data-folder="${fid}">${icon('calendar')}<span>Расписание</span></button>
          <button type="button" class="icon-btn sm" data-action="folder-up" data-folder="${fid}" aria-label="Поднять папку «${esc(name)}»"${idx === 0 ? ' disabled' : ''}>${icon('up')}</button>
          <button type="button" class="icon-btn sm" data-action="folder-down" data-folder="${fid}" aria-label="Опустить папку «${esc(name)}»"${idx === folders.length - 1 ? ' disabled' : ''}>${icon('down')}</button>
          <button type="button" class="icon-btn sm" data-action="folder-rename" data-folder="${fid}" aria-label="Переименовать папку «${esc(name)}»" title="Переименовать">${icon('edit')}</button>
          <button type="button" class="icon-btn sm is-danger" data-action="folder-delete" data-folder="${fid}" aria-label="Удалить папку «${esc(name)}»" title="Удалить папку">${icon('trash')}</button>
        </div>` : '';
      return `<section class="folder${open ? '' : ' is-collapsed'}" data-folder-section="${fid}" aria-labelledby="folder-title-${fid}">
        <div class="folder-head">
          <button type="button" class="folder-toggle" data-action="folder-toggle" data-folder="${fid}" aria-expanded="${open}" aria-controls="folder-body-${fid}">
            <span class="folder-chevron">${icon('down')}</span>${icon('folder')}
            <span class="folder-name" id="folder-title-${fid}">${esc(name)}</span><span class="count">${list.length}</span>
          </button>
          ${tools}
        </div>
        <div class="folder-body" id="folder-body-${fid}"${open ? '' : ' hidden'}>
          ${list.length
            ? `<div class="grid-cards">${list.map((c) => channelCardHtml(c, folders)).join('')}</div>`
            : '<p class="hint folder-empty">В папке пока нет каналов — выберите её в карточке канала.</p>'}
        </div>
      </section>`;
    };

    let connected;
    if (!active.length && !folders.length) {
      connected = emptyState('channels', 'Нет подключённых каналов', 'Добавьте бота в администраторы канала (право «Публикация сообщений»), затем подтвердите канал здесь или в боте.');
    } else if (!folders.length) {
      connected = `<div class="grid-cards">${active.map((c) => channelCardHtml(c, folders)).join('')}</div>`;
    } else {
      let idx = 0;
      connected = `<div class="folders">${groups.map((g) => folderSection(g, g.folder ? idx++ : -1)).join('')}</div>`;
    }

    root.innerHTML = `
      ${pageHead('Каналы', {
        subtitle: 'Каналы, в которые бот может публиковать посты',
        actions: `<button type="button" class="btn btn-secondary" data-action="folder-new">${icon('folderPlus')}<span>Новая папка</span></button>`,
      })}
      <section class="card add-channel">
        <form id="channel-add" class="add-form" novalidate>
          <label class="field grow">
            <span class="label">Добавить канал</span>
            <input class="input" name="ref" placeholder="@username, ссылка t.me/… или ID -100…" autocomplete="off" autocapitalize="off" spellcheck="false" required>
          </label>
          <button class="btn btn-primary" type="submit">${icon('plus')}<span>Добавить</span></button>
        </form>
        <p class="hint">${icon('info')} Сначала добавьте бота администратором канала с правом публикации. Бот сам заметит это и предложит подключить канал.</p>
      </section>
      ${pending.length ? pendingBannerHtml(pending) : ''}
      <section class="section">
        <div class="section-head"><h2>Подключённые <span class="count">${active.length}</span></h2>
          ${folders.length || active.length < 2 ? '' : '<span class="muted small">Папки помогают не теряться, когда каналов много</span>'}</div>
        ${connected}
      </section>
      ${inactive.length ? `<section class="section">
        <details class="inactive"><summary><h2>Неактивные <span class="count">${inactive.length}</span></h2></summary>
          <div class="grid-cards">${inactive.map((c) => channelCardHtml(c, folders)).join('')}</div>
        </details>
      </section>` : ''}`;

    /** Re-renders the page and puts keyboard focus back on the same control (first match wins). */
    const rerender = async (...focusSels) => {
      if (!ctx.alive()) return;
      await route();
      const el = focusSels.map((sel) => document.querySelector(sel)).find(Boolean);
      if (el) el.focus();
    };

    const createFolder = () => promptModal({
      title: 'Новая папка',
      label: 'Название папки',
      placeholder: 'Например, «Новости»',
      confirmLabel: 'Создать',
      hint: 'До 40 символов. Каналы в папку добавляются в их карточках.',
      save: async (name) => {
        const res = await api.post('api/folders', { name });
        const folder = unwrap(res, 'folder');
        return folder && typeof folder === 'object' ? folder : { name };
      },
    });

    onSubmit(root, {
      'channel-add': async (form) => {
        const input = form.elements.namedItem('ref');
        const ref = input.value.trim();
        if (!ref) throw new ValidationError('Укажите @username, ссылку или ID канала', '#channel-add [name="ref"]');
        const res = await api.post('api/channels', { ref });
        const ch = unwrap(res, 'channel');
        toast(ch && ch.title ? `Канал «${ch.title}» подключён` : 'Канал подключён', 'success');
        rerender();
      },
    });

    onAction(root, {
      ...pendingHandlers(() => rerender()),
      refresh: (el) => busy(el, async () => {
        const res = await api.post(`api/channels/${num(el.dataset.id)}/refresh`);
        const ch = unwrap(res, 'channel');
        if (ch && ch.status === 'left') toast('Бот больше не администратор этого канала', 'error');
        else toast('Данные канала обновлены', 'success');
        rerender();
      }),
      remove: async (el) => {
        const ch = byId(el.dataset.id) || {};
        const ok = await confirmModal({
          title: 'Удалить канал из панели?',
          message: `«${ch.title || 'Канал'}» пропадёт из списка. Бот останется администратором в Telegram — при необходимости уберите его в настройках канала. Позже канал можно подключить снова.`,
          confirmLabel: 'Удалить',
          danger: true,
        });
        if (!ok) return;
        await busy(el, async () => {
          await api.del(`api/channels/${num(el.dataset.id)}`);
          toast('Канал удалён из панели', 'success');
          rerender();
        });
      },
      'ch-slots': async (el) => {
        const ch = byId(el.dataset.id);
        if (!ch) return;
        const updated = await editChannelSchedule(ch);
        if (updated) rerender(`[data-action="ch-slots"][data-id="${num(ch.id)}"]`);
      },
      'folder-new': async () => {
        const folder = await createFolder();
        if (!folder || folder === 'cancel') return;
        toast(`Папка «${folder.name || ''}» создана. Добавьте в неё каналы — в их карточках.`, 'success');
        rerender();
      },
      'folder-toggle': (el) => {
        const fid = String(num(el.dataset.folder));
        const section = el.closest('.folder');
        const open = el.getAttribute('aria-expanded') !== 'true';
        el.setAttribute('aria-expanded', String(open));
        section.classList.toggle('is-collapsed', !open);
        $(`#folder-body-${fid}`, root).hidden = !open;
        const set = readCollapsed();
        if (open) set.delete(fid);
        else set.add(fid);
        storageSet(COLLAPSED_KEY, set.size ? JSON.stringify(Array.from(set)) : null);
      },
      'folder-rename': async (el) => {
        const f = folderById(el.dataset.folder);
        if (!f) return;
        const res = await promptModal({
          title: 'Переименовать папку',
          label: 'Название папки',
          value: f.name || '',
          save: async (name) => {
            await api.put(`api/folders/${num(f.id)}`, { name });
            return { name };
          },
        });
        if (!res || res === 'cancel') return;
        toast('Папка переименована', 'success');
        rerender(`[data-action="folder-rename"][data-folder="${num(f.id)}"]`);
      },
      'folder-up': (el) => moveFolder(el, -1),
      'folder-down': (el) => moveFolder(el, 1),
      'folder-delete': async (el) => {
        const f = folderById(el.dataset.folder);
        if (!f) return;
        const ok = await confirmModal({
          title: 'Удалить папку?',
          message: `Папка «${f.name}» будет удалена. Каналы из неё не удаляются — они переместятся в «Без папки».`,
          confirmLabel: 'Удалить папку',
          danger: true,
        });
        if (!ok) return;
        await busy(el, async () => {
          await api.del(`api/folders/${num(f.id)}`);
          toast('Папка удалена', 'success');
          rerender();
        });
      },
      'folder-slots': async (el) => {
        const f = folderById(el.dataset.folder);
        if (!f) return;
        const members = channels.filter((c) => num(c.folder_id) === num(f.id));
        if (!members.length) {
          toast(`В папке «${f.name}» пока нет каналов — добавьте их в карточках каналов, затем задайте расписание`, 'info');
          return;
        }
        // Start from the common schedule when every channel of the folder has the same one.
        const texts = new Set(members.map((c) => slotsToText(channelSlots(c))));
        const start = members.length && texts.size === 1 ? channelSlots(members[0]) : normSlots(null);
        const res = await openScheduleEditor({
          title: 'Расписание для всей папки',
          subtitle: `Папка «${f.name}»: ${members.length} ${plural(members.length, ['канал', 'канала', 'каналов'])}.`,
          note: texts.size > 1 ? 'Сейчас у каналов папки разные расписания — сохранённое заменит их все.' : 'Расписание будет установлено всем каналам папки.',
          slots: start,
          save: (payload) => api.put(`api/folders/${num(f.id)}/slots`, payload),
        });
        if (!res) return;
        const n = [res.changed, res.count, res.updated, res.channels].map(Number).find((x) => Number.isFinite(x));
        const count = n ?? members.length;
        toast(`Расписание применено к ${count} ${plural(count, ['каналу', 'каналам', 'каналам'])}`, 'success');
        rerender(`[data-action="folder-slots"][data-folder="${num(f.id)}"]`);
      },
    });

    async function moveFolder(el, direction) {
      const fid = num(el.dataset.folder);
      await busy(el, async () => {
        await api.put(`api/folders/${fid}`, { direction });
        await rerender(
          `[data-action="${direction < 0 ? 'folder-up' : 'folder-down'}"][data-folder="${fid}"]:not([disabled])`,
          `[data-action="${direction < 0 ? 'folder-down' : 'folder-up'}"][data-folder="${fid}"]:not([disabled])`,
          `[data-action="folder-toggle"][data-folder="${fid}"]`,
        );
      });
    }

    root.addEventListener('change', async (e) => {
      const sel = e.target.closest('[data-folder-select]');
      if (!sel) return;
      const chId = num(sel.dataset.folderSelect);
      const previous = sel.dataset.current || '';
      let value = sel.value;
      let name = '';
      try {
        if (value === 'new') {
          const folder = await createFolder();
          if (!folder || folder === 'cancel' || folder.id === undefined) {
            sel.value = previous;
            if (folder && folder !== 'cancel') rerender();
            return;
          }
          value = String(num(folder.id));
          name = folder.name || '';
        }
        sel.disabled = true;
        await api.put(`api/channels/${chId}/folder`, { folder_id: value === '' ? null : num(value) });
        const f = folderById(value);
        name = name || (f ? f.name : '');
        toast(value === '' ? 'Канал перемещён в «Без папки»' : `Канал перемещён в папку «${name || 'новая папка'}»`, 'success');
        rerender(`[data-folder-select="${chId}"]`);
      } catch (err) {
        sel.disabled = false;
        sel.value = previous;
        handleError(err);
      }
    });
  }

  function channelCardHtml(ch, folders = []) {
    const id = num(ch.id);
    const link = channelLink(ch);
    const handle = ch.username
      ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">@${esc(ch.username)}</a>`
      : (link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">приватный канал</a>` : '<span>приватный канал</span>');
    const rights = [['can_post', 'Публикация'], ['can_edit', 'Редактирование'], ['can_delete', 'Удаление']]
      .map(([key, label]) => `<span class="right ${ch[key] ? 'on' : 'off'}" title="${ch[key] ? 'Есть право' : 'Нет права'}: ${label}">${icon(ch[key] ? 'check' : 'x')}${label}</span>`)
      .join('');
    let warning = '';
    if (ch.status === 'active' && !ch.can_post) warning = 'У бота нет права публикации — выдайте его в настройках администраторов канала.';
    else if (ch.status === 'left') warning = 'Бот больше не администратор канала. Верните права и нажмите «Проверить снова».';
    const title = ch.title || 'Без названия';
    let actions;
    let plan = '';
    if (ch.status === 'active') {
      const fid = ch.folder_id === null || ch.folder_id === undefined ? '' : String(num(ch.folder_id));
      const summary = channelSlotsSummary(ch);
      plan = `<div class="ch-plan">
        <label class="ch-folder">${icon('folder')}<span class="sr-only">Папка канала «${esc(title)}»</span>
          <select class="input input-sm" data-folder-select="${id}" data-current="${esc(fid)}">
            <option value=""${fid ? '' : ' selected'}>Без папки</option>
            ${folders.map((f) => `<option value="${num(f.id)}"${String(num(f.id)) === fid ? ' selected' : ''}>${esc(f.name)}</option>`).join('')}
            <option value="new">+ Новая папка…</option>
          </select></label>
        <p class="ch-slots${channelHasSlots(ch) ? '' : ' is-empty'}">${icon('calendar')}<span><span class="sr-only">Расписание: </span>${esc(summary)}</span></p>
      </div>`;
      actions = `<button type="button" class="btn btn-secondary btn-sm" data-action="ch-slots" data-id="${id}">${icon('calendar')}<span>Расписание</span></button>
        <a class="btn btn-secondary btn-sm" href="#/plan/${id}">${icon('queue')}<span>План</span></a>
        <a class="btn btn-secondary btn-sm" href="#/stats/${id}">${icon('chart')}<span>Статистика</span></a>
        <span class="card-actions-end">
          <button type="button" class="icon-btn sm" data-action="refresh" data-id="${id}" aria-label="Обновить данные канала «${esc(title)}»" title="Обновить данные из Telegram">${icon('refresh')}</button>
          <button type="button" class="icon-btn sm is-danger" data-action="remove" data-id="${id}" aria-label="Удалить канал «${esc(title)}» из панели" title="Удалить из панели">${icon('trash')}</button>
        </span>`;
    } else {
      actions = `<button type="button" class="btn btn-secondary btn-sm" data-action="approve" data-id="${id}">${icon('refresh')}<span>${ch.status === 'ignored' ? 'Подключить' : 'Проверить снова'}</span></button>
        <button type="button" class="btn btn-ghost btn-sm is-danger" data-action="remove" data-id="${id}">${icon('trash')}<span>Удалить</span></button>`;
    }
    return `<article class="card channel-card">
      <div class="ch-head">
        ${avatarHtml(id, ch.title)}
        <div class="ch-info"><h3 class="ch-title">${esc(title)}</h3><div class="ch-handle">${handle}</div></div>
        ${badge(CHANNEL_STATUS, ch.status)}
      </div>
      <div class="ch-stats">
        <div><span class="ch-num">${fmtNum(ch.members_count)}</span> <span class="muted small">${esc(plural(ch.members_count, MEMBERS_FORMS))}</span></div>
        <div class="muted small">${ch.added_at ? `подключён ${esc(fmtDate(ch.added_at))}` : `найден ${esc(fmtDate(ch.discovered_at))}`}</div>
      </div>
      <div class="rights" aria-label="Права бота">${rights}</div>
      ${plan}
      ${warning ? `<p class="callout is-warn small">${icon('alert')}<span>${esc(warning)}</span></p>` : ''}
      <div class="card-actions">${actions}</div>
    </article>`;
  }

  // ======================================================================= 8h. Stats page

  const PERIODS = [7, 30, 90];
  const CHART_H = 220;

  async function pageStats(root, r, ctx) {
    setPageTitle('Статистика');
    const [all] = await Promise.all([loadChannels(), ensureServerTz()]);
    if (!ctx.alive()) return;
    const channels = all.filter((c) => c.status === 'active' || c.status === 'left');
    if (!channels.length) {
      root.innerHTML = `${pageHead('Статистика')}${emptyState('chart', 'Нет каналов для статистики', 'Подключите канал — бот начнёт собирать данные о подписчиках, вступлениях и постах.', '<a class="btn btn-primary" href="#/channels">Перейти к каналам</a>')}`;
      return;
    }
    let channelId = num(r.segs[1]) || num(channels[0].id);
    if (!channels.some((c) => num(c.id) === channelId)) channelId = num(channels[0].id);
    let days = PERIODS.includes(num(r.query.get('days'))) ? num(r.query.get('days')) : 30;

    const options = channels.map((c) => `<option value="${num(c.id)}"${num(c.id) === channelId ? ' selected' : ''}>${esc(c.title || 'Без названия')}${c.status !== 'active' ? ' (нет доступа)' : ''}</option>`).join('');
    const periods = PERIODS.map((d) => `<button type="button" class="seg" data-days="${d}" aria-pressed="${d === days}">${d} дней</button>`).join('');
    root.innerHTML = `
      ${pageHead('Статистика', { subtitle: 'Подписчики, вступления, посты и реакции по дням' })}
      <div class="stats-controls">
        <label class="field stats-select"><span class="sr-only">Канал</span>
          <select class="input" id="st-channel" aria-label="Канал">${options}</select></label>
        <div class="segmented" role="group" aria-label="Период">${periods}</div>
      </div>
      <div id="st-body"></div>`;

    const body = $('#st-body', root);
    let loadSeq = 0;
    let lastSeries = null;

    async function load() {
      replaceHash(`#/stats/${channelId}${days !== 30 ? `?days=${days}` : ''}`);
      const seq = ++loadSeq;
      body.innerHTML = loadingHtml(3);
      try {
        const data = await api.get(`api/channels/${channelId}/stats?days=${days}`);
        if (seq !== loadSeq || !ctx.alive()) return;
        const stats = data && Array.isArray(data.series) ? data : unwrap(data, 'stats');
        lastSeries = Array.isArray(stats.series) ? stats.series : [];
        renderStats(body, stats, days);
        drawCharts(body, lastSeries);
      } catch (err) {
        if (seq !== loadSeq || !ctx.alive() || err.silent) return;
        body.innerHTML = errorStateHtml(err);
        const retry = $('[data-retry]', body);
        if (retry) retry.addEventListener('click', () => load());
      }
    }

    $('#st-channel', root).addEventListener('change', (e) => {
      channelId = num(e.target.value);
      load();
    });
    $('.segmented', root).addEventListener('click', (e) => {
      const btn = e.target.closest('[data-days]');
      if (!btn) return;
      days = num(btn.dataset.days);
      $$('.seg', root).forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
      load();
    });

    const onResize = debounce(() => {
      if (!ctx.alive()) {
        window.removeEventListener('resize', onResize);
        return;
      }
      if (lastSeries) drawCharts(body, lastSeries);
    }, 150);
    window.addEventListener('resize', onResize);

    await load();
  }

  function deltaHtml(value, suffix = '') {
    if (value === null || value === undefined) return '<span class="delta">нет данных</span>';
    const cls = value > 0 ? 'up' : value < 0 ? 'down' : 'flat';
    return `<span class="delta ${cls}">${esc(fmtSigned(value))}</span>${suffix ? ` ${esc(suffix)}` : ''}`;
  }

  function reactionsBreakdown(list) {
    if (!Array.isArray(list)) return '';
    return list.slice(0, 6).map((item) => {
      if (!item || typeof item !== 'object') return '';
      const type = item.type && typeof item.type === 'object' ? item.type : item;
      let emoji = type.emoji || item.emoji || '';
      if (!emoji) emoji = type.type === 'paid' ? '⭐' : '✨';
      const count = Number(item.total_count ?? item.count ?? 0);
      return `${emoji} ${fmtNum(count)}`;
    }).filter(Boolean).join('  ');
  }

  function topPostsHtml(top) {
    if (!Array.isArray(top) || !top.length) {
      return '<p class="hint card-pad">Здесь появятся посты, опубликованные через бота, с наибольшим числом реакций.</p>';
    }
    const rows = top.map((t) => {
      const href = safeHref(t.link);
      const text = esc(t.excerpt || 'Пост без текста');
      return `<tr>
        <td class="top-text">${href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${text}</a>` : text}
          ${t.post_id ? ` <a class="muted small" href="#/compose/${num(t.post_id)}">#${num(t.post_id)}</a>` : ''}</td>
        <td class="nowrap">${esc(fmtDateTime(t.sent_at))}</td>
        <td class="num"><b>${fmtNum(t.reactions_total)}</b><div class="muted small">${esc(reactionsBreakdown(t.reactions))}</div></td>
      </tr>`;
    }).join('');
    return `<div class="table-wrap"><table class="table">
      <thead><tr><th scope="col">Пост</th><th scope="col">Опубликован</th><th scope="col" class="num">Реакции</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  function renderStats(body, s, days) {
    const series = Array.isArray(s.series) ? s.series : [];
    const known = series.filter((d) => d.members !== null && d.members !== undefined);
    const growth = known.length >= 2 ? Number(known[known.length - 1].members) - Number(known[0].members) : null;
    const delta7 = s.delta ? s.delta['7d'] : null;
    const since = fmtDate(s.tracking_since); // ru-RU dates already end with "г."
    const tracking = s.tracking_since
      ? `Статистика подписчиков собирается с ${esc(since)}${since.endsWith('.') ? '' : '.'}`
      : 'Данных о подписчиках пока нет: бот делает снимок числа подписчиков раз в час.';

    body.innerHTML = `
      <section class="tiles" aria-label="Показатели за период">
        ${kpiHtml({ label: 'Подписчики', value: fmtNum(s.members_now), extra: `${deltaHtml(delta7)} за 7 дней` })}
        ${kpiHtml({ label: 'Прирост', value: growth === null ? '—' : `<span class="delta-big ${growth > 0 ? 'up' : growth < 0 ? 'down' : ''}">${esc(fmtSigned(growth))}</span>`, sub: `за ${days} дней` })}
        ${kpiHtml({ label: 'Вступили', value: fmtNum(s.joins), sub: `за ${days} дней` })}
        ${kpiHtml({ label: 'Отписались', value: fmtNum(s.leaves), sub: `за ${days} дней` })}
        ${kpiHtml({ label: 'Посты', value: `${fmtNum(s.posts)}<span class="tile-slash"> / ${fmtNum(s.posts_bot)}</span>`, sub: 'всего / через бота' })}
        ${kpiHtml({ label: 'Реакции', value: fmtNum(s.reactions), sub: 'на постах бота' })}
      </section>
      <p class="note">${icon('info')}<span>${tracking} Вступления и отписки учитываются, пока бот — администратор канала.</span></p>

      <div class="charts">
        <section class="card chart-card chart-wide">
          <div class="card-head"><h2>Подписчики</h2></div>
          <div class="chart" data-chart="members"></div>
        </section>
        <section class="card chart-card">
          <div class="card-head"><h2>Вступления и отписки</h2>
            <div class="legend"><span><i class="lg-join"></i>вступили</span><span><i class="lg-leave"></i>отписались</span></div></div>
          <div class="chart" data-chart="joins"></div>
        </section>
        <section class="card chart-card">
          <div class="card-head"><h2>Посты по дням</h2>
            <div class="legend"><span><i class="lg-bot"></i>через бота</span><span><i class="lg-other"></i>другие</span></div></div>
          <div class="chart" data-chart="posts"></div>
        </section>
      </div>

      <section class="card flush section">
        <div class="card-head"><h2>Лучшие посты по реакциям</h2></div>
        ${topPostsHtml(s.top_posts)}
      </section>
      <p class="note">${icon('eye')}<span>Количество просмотров через Telegram Bot API недоступно — смотрите его в самом Telegram, в статистике канала.</span></p>`;
  }

  function drawCharts(body, series) {
    $$('.chart[data-chart]', body).forEach((el) => {
      const width = Math.max(280, Math.floor(el.clientWidth || 600));
      switch (el.dataset.chart) {
        case 'members':
          el.innerHTML = membersChartSvg(series, width);
          break;
        case 'joins':
          el.innerHTML = barChartSvg(series, width, [
            { cls: 'bar-join', label: 'вступили', value: (d) => d.joins },
            { cls: 'bar-leave', label: 'отписались', value: (d) => d.leaves },
          ], { label: 'Вступления и отписки по дням', emptyText: 'За период не было вступлений и отписок' });
          break;
        case 'posts':
          el.innerHTML = barChartSvg(series, width, [
            { cls: 'bar-bot', label: 'через бота', value: (d) => d.posts_bot },
            { cls: 'bar-other', label: 'другие', value: (d) => Math.max(0, (Number(d.posts) || 0) - (Number(d.posts_bot) || 0)) },
          ], { stacked: true, label: 'Посты по дням', emptyText: 'За период не было постов' });
          break;
        default:
          break;
      }
    });
  }

  /** "Nice" axis for integer counts: steps of 1/2/5 × 10ⁿ. */
  function niceScale(lo, hi, count = 4) {
    let min = Number.isFinite(lo) ? lo : 0;
    let max = Number.isFinite(hi) ? hi : 1;
    if (max <= min) max = min + 1;
    const raw = (max - min) / count;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const f = raw / mag;
    const step = Math.max(1, (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag);
    min = Math.floor(min / step) * step;
    max = Math.ceil(max / step) * step;
    if (max === min) max = min + step;
    const ticks = [];
    for (let v = min; v <= max + step / 2; v += step) ticks.push(Math.round(v));
    return { min, max, ticks };
  }

  function chartFrame(width, scale) {
    const labelChars = Math.max(...scale.ticks.map((t) => fmtCompact(t).length));
    const pad = { l: Math.max(32, Math.round(labelChars * 6.6 + 14)), r: 14, t: 12, b: 28 };
    const pw = Math.max(20, width - pad.l - pad.r);
    const ph = CHART_H - pad.t - pad.b;
    const yAt = (v) => pad.t + ph * (1 - (v - scale.min) / (scale.max - scale.min));
    return { width, height: CHART_H, pad, pw, ph, base: pad.t + ph, yAt };
  }

  function gridSvg(f, scale) {
    return scale.ticks.map((t) => {
      const y = f.yAt(t).toFixed(1);
      return `<line class="grid" x1="${f.pad.l}" x2="${f.width - f.pad.r}" y1="${y}" y2="${y}"/><text x="${f.pad.l - 8}" y="${y}" dy="0.32em" text-anchor="end">${esc(fmtCompact(t))}</text>`;
    }).join('');
  }

  function xLabelsSvg(f, series, xAt) {
    const n = series.length;
    const maxLabels = Math.max(2, Math.floor(f.pw / 54));
    const step = Math.max(1, Math.ceil(n / maxLabels));
    let out = '';
    for (let i = n - 1; i >= 0; i -= step) {
      const x = xAt(i);
      const anchor = x > f.width - f.pad.r - 18 ? 'end' : x < f.pad.l + 18 ? 'start' : 'middle';
      out += `<text x="${x.toFixed(1)}" y="${f.height - 8}" text-anchor="${anchor}">${esc(dayShort(series[i].date))}</text>`;
    }
    return out;
  }

  function svgWrap(width, inner, label) {
    return `<svg class="chart-svg" viewBox="0 0 ${width} ${CHART_H}" width="${width}" height="${CHART_H}" role="img" aria-label="${esc(label)}">${inner}</svg>`;
  }

  const chartEmpty = (text) => `<div class="chart-empty">${icon('chart')}<span>${esc(text)}</span></div>`;

  function membersChartSvg(series, width) {
    const vals = series.map((d) => (d.members === null || d.members === undefined ? null : Number(d.members)));
    const known = vals.filter((v) => v !== null);
    if (!known.length) return chartEmpty('Данных о подписчиках пока нет — снимки делаются раз в час');
    const lo = Math.min(...known);
    const hi = Math.max(...known);
    const spread = Math.max(2, Math.ceil((hi - lo) * 0.15));
    const scale = niceScale(Math.max(0, lo - spread), hi + spread);
    const f = chartFrame(width, scale);
    const n = vals.length;
    const xAt = (i) => (n === 1 ? f.pad.l + f.pw / 2 : f.pad.l + (f.pw * i) / (n - 1));
    const base = f.base.toFixed(1);

    // Days without data (before tracking started) break the line into segments.
    let lines = '';
    let areas = '';
    let seg = [];
    const flush = () => {
      if (seg.length) {
        const d = seg.map(([x, y], k) => `${k ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join(' ');
        areas += `<path class="area" d="${d} L${seg[seg.length - 1][0].toFixed(1)} ${base} L${seg[0][0].toFixed(1)} ${base} Z"/>`;
        lines += `<path class="line" d="${d}"/>`;
      }
      seg = [];
    };
    vals.forEach((v, i) => {
      if (v === null) flush();
      else seg.push([xAt(i), f.yAt(v)]);
    });
    flush();

    const slot = n > 1 ? f.pw / (n - 1) : f.pw;
    let dots = '';
    let hits = '';
    vals.forEach((v, i) => {
      const x = xAt(i).toFixed(1);
      // A point with no known neighbour is a zero-length line (not stroked): always give it a dot.
      const lone = v !== null && (i === 0 || vals[i - 1] === null) && (i === n - 1 || vals[i + 1] === null);
      if (v !== null && (n <= 31 || lone)) dots += `<circle class="dot" cx="${x}" cy="${f.yAt(v).toFixed(1)}" r="2.5"/>`;
      const tip = `${dayLong(series[i].date)}: ${v === null ? 'нет данных' : `${fmtNum(v)} ${plural(v, MEMBERS_FORMS)}`}`;
      const marker = v === null ? '' : `<line class="hover-line" x1="${x}" x2="${x}" y1="${f.pad.t}" y2="${base}"/><circle class="hover-dot" cx="${x}" cy="${f.yAt(v).toFixed(1)}" r="4.5"/>`;
      hits += `<g class="hit"><rect x="${(xAt(i) - slot / 2).toFixed(1)}" y="${f.pad.t}" width="${slot.toFixed(1)}" height="${f.ph}"/>${marker}<title>${esc(tip)}</title></g>`;
    });
    const defs = '<defs><linearGradient id="pcb-area-grad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="stop-a"/><stop offset="1" class="stop-b"/></linearGradient></defs>';
    return svgWrap(width, defs + gridSvg(f, scale) + areas + lines + dots + xLabelsSvg(f, series, xAt) + hits, 'График числа подписчиков по дням');
  }

  function barChartSvg(series, width, bars, { stacked = false, label = '', emptyText = '' } = {}) {
    const n = series.length;
    if (!n) return chartEmpty('Нет данных за период');
    const values = series.map((d) => bars.map((b) => Math.max(0, Number(b.value(d)) || 0)));
    const peaks = values.map((vs) => (stacked ? vs.reduce((a, b) => a + b, 0) : Math.max(...vs)));
    const peak = Math.max(0, ...peaks);
    const scale = niceScale(0, Math.max(1, peak));
    const f = chartFrame(width, scale);
    const slot = f.pw / n;
    const groupW = Math.max(2, slot * 0.72);
    const barW = stacked ? groupW : groupW / bars.length;
    const xAt = (i) => f.pad.l + slot * (i + 0.5);

    let rects = '';
    let hits = '';
    values.forEach((vs, i) => {
      const x0 = f.pad.l + slot * i + (slot - groupW) / 2;
      let top = f.base;
      vs.forEach((v, k) => {
        if (v <= 0) return;
        const h = Math.max(1.5, (v / (scale.max - scale.min)) * f.ph);
        const x = stacked ? x0 : x0 + k * barW;
        const y = stacked ? top - h : f.base - h;
        if (stacked) top -= h;
        const w = Math.max(1, stacked ? barW : barW - Math.min(2, barW * 0.15));
        rects += `<rect class="${bars[k].cls}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" rx="${Math.min(3, w / 3).toFixed(1)}"/>`;
      });
      const tip = `${dayLong(series[i].date)}: ${bars.map((b, k) => `${b.label} — ${fmtNum(vs[k])}`).join(', ')}`;
      hits += `<g class="hit"><rect x="${(f.pad.l + slot * i).toFixed(1)}" y="${f.pad.t}" width="${slot.toFixed(1)}" height="${f.ph}"/><title>${esc(tip)}</title></g>`;
    });
    const note = peak === 0 && emptyText
      ? `<text class="chart-note" x="${(f.pad.l + f.pw / 2).toFixed(1)}" y="${(f.pad.t + f.ph / 2).toFixed(1)}" text-anchor="middle">${esc(emptyText)}</text>`
      : '';
    return svgWrap(width, gridSvg(f, scale) + rects + note + xLabelsSvg(f, series, xAt) + hits, label);
  }

  // ======================================================================= 8i. Settings page

  async function pageSettings(root, r, ctx) {
    setPageTitle('Настройки');
    const [settings, overview, adminsData] = await Promise.all([
      api.get('api/settings'),
      api.get('api/overview').catch((err) => {
        if (err.silent) throw err;
        return null; // bot info is optional on this page
      }),
      api.get('api/admins'),
    ]);
    if (!ctx.alive()) return;
    if (overview) applyOverviewToShell(overview);
    state.serverTz = settings.timezone || state.serverTz;
    state.auth.password_set = Boolean(settings.password_set);
    state.auth.password_source = settings.password_source || null;
    if (settings.version) setVersion(settings.version);
    // A server without SPEC_V23 does not report the setting: no card then.
    const premium = typeof settings.premium_emoji === 'boolean' ? settings.premium_emoji : null;
    if (premium !== null) state.premiumEmoji = premium;

    const admins = Array.isArray(adminsData.admins) ? adminsData.admins : [];
    const botState = readBotState(settings, overview);
    const webUrl = settings.web_url || (overview && overview.web_url) || '';
    const theme = getTheme();
    const version = settings.version || APP_VERSION;

    root.innerHTML = `
      ${pageHead('Настройки')}
      <div class="settings-grid">
        ${botCardHtml(botState)}
        ${premium === null ? '' : premiumCardHtml(premium)}
        ${passwordCardHtml(Boolean(settings.password_set), settings.password_source)}
        <section class="card">
          <div class="card-head"><h2>${icon('users')}Администраторы</h2><span class="muted small">${admins.length}</span></div>
          <p class="hint">Только администраторы могут пользоваться ботом. Остальным бот отвечает «Это приватный бот».</p>
          <ul class="admin-list">${admins.map(adminItemHtml).join('')}</ul>
          <form id="admin-add" class="add-form" novalidate>
            <label class="field grow"><span class="label">Добавить по Telegram ID</span>
              <input class="input" name="user_id" inputmode="numeric" autocomplete="off" placeholder="например, 123456789"></label>
            <button class="btn btn-primary" type="submit">${icon('plus')}<span>Добавить</span></button>
          </form>
          <p class="hint">Свой ID можно узнать, например, у бота @userinfobot. Новому администратору нужно написать боту /start.</p>
        </section>
        <section class="card">
          <div class="card-head"><h2>${icon('info')}О панели</h2></div>
          <dl class="kv">
            <dt>Часовой пояс</dt><dd>${esc(settings.timezone || (overview && overview.timezone) || '—')}</dd>
            <dt>Адрес панели</dt><dd class="url-cell"><code>${esc(webUrl || '—')}</code>${webUrl ? `<button type="button" class="btn btn-ghost btn-sm" data-action="copy-url">${icon('copy')}<span>Копировать</span></button>` : ''}</dd>
            <dt>Версия</dt><dd>${version ? `v${esc(version)}` : '—'}</dd>
          </dl>
          <p class="hint">Часовой пояс и адрес панели задаются в файле .env (TIMEZONE, WEB_PUBLIC_URL, WEB_PREFIX).</p>
        </section>
        <section class="card">
          <div class="card-head"><h2>${icon('palette')}Оформление</h2></div>
          <div class="segmented" role="group" aria-label="Тема оформления">
            ${THEMES.map((v) => `<button type="button" class="seg" data-action="theme" data-theme="${v}" aria-pressed="${theme === v}">${THEME_LABELS[v]}</button>`).join('')}
          </div>
          <p class="hint">Тема общая для всех панелей на этом адресе. Её же переключает кнопка внизу меню.</p>
        </section>
      </div>`;

    const rerender = () => {
      if (ctx.alive()) route();
    };

    onSubmit(root, {
      'pw-set': async (form) => {
        const pw = form.elements.namedItem('new_password').value;
        const confirmPw = form.elements.namedItem('confirm_password').value;
        if (pw.length < 6) throw new ValidationError('Пароль должен быть не короче 6 символов', '#pw-set [name="new_password"]');
        if (pw !== confirmPw) throw new ValidationError('Пароли не совпадают', '#pw-set [name="confirm_password"]');
        await api.post('api/auth/password', { new: pw });
        await afterPasswordChange('Пароль установлен');
      },
      'pw-change': async (form) => {
        const current = form.elements.namedItem('current').value;
        const pw = form.elements.namedItem('new_password').value;
        const confirmPw = form.elements.namedItem('confirm_password').value;
        if (!current) throw new ValidationError('Введите текущий пароль', '#pw-change [name="current"]');
        if (pw.length < 6) throw new ValidationError('Новый пароль должен быть не короче 6 символов', '#pw-change [name="new_password"]');
        if (pw !== confirmPw) throw new ValidationError('Пароли не совпадают', '#pw-change [name="confirm_password"]');
        await api.post('api/auth/password', { current, new: pw });
        await afterPasswordChange('Пароль изменён');
      },
      'pw-remove': async (form) => {
        const current = form.elements.namedItem('current').value;
        if (!current) throw new ValidationError('Введите текущий пароль', '#pw-remove [name="current"]');
        const ok = await confirmModal({
          title: 'Удалить пароль?',
          message: 'Панель станет доступна без входа всем, кто может открыть её адрес.',
          confirmLabel: 'Удалить пароль',
          danger: true,
        });
        if (!ok) return;
        await api.del('api/auth/password', { current });
        await afterPasswordChange('Пароль удалён — панель открыта без входа');
      },
      'admin-add': async (form) => {
        const raw = form.elements.namedItem('user_id').value.trim();
        if (!/^\d{3,20}$/.test(raw)) throw new ValidationError('Укажите числовой Telegram ID', '#admin-add [name="user_id"]');
        await api.post('api/admins', { user_id: Number(raw) });
        toast('Администратор добавлен', 'success');
        rerender();
      },
      'bot-token': (form) => changeBot(form, botState, rerender),
    });

    onAction(root, {
      'toggle-token': (el) => setTokenVisible(el, el.getAttribute('aria-pressed') !== 'true'),
      'bot-revert': (el) => revertBot(el, botState, rerender),
      logout: (el) => busy(el, async () => {
        await api.post('api/auth/logout');
        state.auth.authenticated = false;
        showLogin('Вы вышли из панели.');
      }),
      'admin-remove': async (el) => {
        const admin = admins.find((a) => num(a.user_id) === num(el.dataset.id)) || {};
        const name = admin.full_name || (admin.username ? `@${admin.username}` : `ID ${num(el.dataset.id)}`);
        const ok = await confirmModal({
          title: 'Удалить администратора?',
          message: `${name} больше не сможет пользоваться ботом.`,
          confirmLabel: 'Удалить',
          danger: true,
        });
        if (!ok) return;
        await busy(el, async () => {
          await api.del(`api/admins/${num(el.dataset.id)}`);
          toast('Администратор удалён', 'success');
          rerender();
        });
      },
      'copy-url': async () => {
        const ok = await copyText(webUrl);
        toast(ok ? 'Адрес скопирован' : 'Не удалось скопировать — выделите адрес вручную', ok ? 'success' : 'error');
      },
      theme: (el) => setTheme(el.dataset.theme),
    });

    root.addEventListener('change', (e) => {
      const input = e.target.closest('[data-premium-emoji]');
      if (input) savePremiumEmoji(input);
    });

    // From the composer's warning (#/settings?card=premium-emoji): show the card.
    if (r.query.get('card') === 'premium-emoji') {
      replaceHash('#/settings');
      const card = $('#premium-emoji-card', root);
      if (card) {
        card.scrollIntoView({ block: 'center' });
        card.classList.add('is-target');
        setTimeout(() => card.classList.remove('is-target'), 2400);
        const toggle = $('[data-premium-emoji]', card);
        if (toggle) toggle.focus({ preventScroll: true });
      }
    }
  }

  // ---- settings: premium (custom) emoji, SPEC_V23

  function premiumBadgeHtml(on) {
    return on
      ? '<span class="badge b-success"><span class="dot" aria-hidden="true"></span>Доступны</span>'
      : '<span class="badge b-neutral"><span class="dot" aria-hidden="true"></span>Недоступны</span>';
  }

  function premiumCardHtml(on) {
    return `<section class="card" id="premium-emoji-card" aria-labelledby="premium-emoji-title">
      <div class="card-head"><h2 id="premium-emoji-title">${icon('sparkles')}Премиум-эмодзи</h2><span data-premium-badge>${premiumBadgeHtml(on)}</span></div>
      <p class="card-text">Telegram показывает премиум-эмодзи в каналах, только если у бота есть дополнительный юзернейм, купленный на <a href="https://fragment.com" target="_blank" rel="noopener noreferrer">Fragment</a> (за TON) и привязанный к боту. Без него в опубликованных постах они заменяются обычными эмодзи.</p>
      <label class="switch">
        <input type="checkbox" data-premium-emoji aria-describedby="premium-emoji-hint"${on ? ' checked' : ''}>
        <span class="switch-text"><span class="switch-label">У бота есть юзернейм с Fragment — премиум-эмодзи доступны</span></span>
      </label>
      <p class="hint" id="premium-emoji-hint">Выключено — редактор предупреждает, что премиум-эмодзи в посте станут обычными, и бот отправляет их обычными. Включено — публикуются как есть. То же самое — в боте: ⚙️ Настройки → ✨ Премиум-эмодзи.</p>
    </section>`;
  }

  /** The checkbox saves at once (PUT api/settings/premium-emoji); a failed save puts it back. */
  async function savePremiumEmoji(input) {
    const enabled = input.checked;
    const label = input.closest('.switch');
    const hadFocus = document.activeElement === input;
    input.disabled = true;
    if (label) label.classList.add('is-busy');
    try {
      const res = await api.put('api/settings/premium-emoji', { enabled });
      const on = res && typeof res.premium_emoji === 'boolean' ? res.premium_emoji : enabled;
      state.premiumEmoji = on;
      input.checked = on;
      const card = input.closest('#premium-emoji-card');
      const badgeBox = card && $('[data-premium-badge]', card);
      if (badgeBox) badgeBox.innerHTML = premiumBadgeHtml(on);
      toast(on
        ? 'Премиум-эмодзи включены — посты публикуются как есть'
        : 'Премиум-эмодзи выключены — бот отправит их обычными эмодзи', 'success');
    } catch (err) {
      input.checked = !enabled;
      handleError(err);
    } finally {
      input.disabled = false;
      if (label) label.classList.remove('is-busy');
      // Disabling the focused checkbox blurred it: give the keyboard focus back.
      if (hadFocus && input.isConnected && (!document.activeElement || document.activeElement === document.body)) input.focus();
    }
  }

  function passwordCardHtml(isSet, source = null) {
    if (source === 'env') {
      // WEB_PASSWORD from .env: the panel cannot set, change or remove it.
      return `<section class="card">
        <div class="card-head"><h2>${icon('lock')}Пароль панели</h2><span class="badge b-success"><span class="dot" aria-hidden="true"></span>Установлен</span></div>
        <p class="callout">${icon('info')}<span><b>Пароль задан в .env (WEB_PASSWORD).</b> Изменить или убрать его можно только там — после смены пароля и перезапуска бота нужно будет войти заново.</span></p>
        <div class="card-foot"><button type="button" class="btn btn-secondary" data-action="logout">${icon('logout')}<span>Выйти</span></button></div>
      </section>`;
    }
    if (!isSet) {
      return `<section class="card">
        <div class="card-head"><h2>${icon('lock')}Пароль панели</h2><span class="badge b-warning"><span class="dot" aria-hidden="true"></span>Не установлен</span></div>
        <p class="callout is-warn">${icon('alert')}<span>Сейчас панель открыта: управлять ботом может любой, кто откроет этот адрес. Установите пароль, особенно если панель доступна из интернета.</span></p>
        <form id="pw-set" novalidate>
          <label class="field"><span class="label">Новый пароль</span>
            <input class="input" type="password" name="new_password" minlength="6" autocomplete="new-password"></label>
          <label class="field"><span class="label">Повторите пароль</span>
            <input class="input" type="password" name="confirm_password" autocomplete="new-password"></label>
          <p class="hint">Не короче 6 символов.</p>
          <button class="btn btn-primary" type="submit">${icon('lock')}<span>Установить пароль</span></button>
        </form>
      </section>`;
    }
    return `<section class="card">
      <div class="card-head"><h2>${icon('lock')}Пароль панели</h2><span class="badge b-success"><span class="dot" aria-hidden="true"></span>Установлен</span></div>
      <p class="hint">Вход действует 30 дней. Забыли пароль — сбросьте его в боте: ⚙️ Настройки → «Сбросить пароль веб-панели».</p>
      <form id="pw-change" novalidate>
        <input type="text" name="username" autocomplete="username" value="admin" hidden>
        <label class="field"><span class="label">Текущий пароль</span>
          <input class="input" type="password" name="current" autocomplete="current-password"></label>
        <label class="field"><span class="label">Новый пароль</span>
          <input class="input" type="password" name="new_password" minlength="6" autocomplete="new-password"></label>
        <label class="field"><span class="label">Повторите новый пароль</span>
          <input class="input" type="password" name="confirm_password" autocomplete="new-password"></label>
        <button class="btn btn-primary" type="submit"><span>Сменить пароль</span></button>
      </form>
      <details class="sub-details">
        <summary>Убрать пароль</summary>
        <form id="pw-remove" novalidate>
          <label class="field"><span class="label">Текущий пароль</span>
            <input class="input" type="password" name="current" autocomplete="current-password"></label>
          <button class="btn btn-danger" type="submit">${icon('trash')}<span>Удалить пароль</span></button>
        </form>
      </details>
      <div class="card-foot"><button type="button" class="btn btn-secondary" data-action="logout">${icon('logout')}<span>Выйти</span></button></div>
    </section>`;
  }

  function adminItemHtml(a) {
    const id = num(a.user_id);
    const name = a.full_name || (a.username ? `@${a.username}` : 'Пользователь');
    return `<li class="admin-item">
      ${avatarHtml(id, name, 'sm')}
      <div class="admin-info">
        <div class="admin-name">${esc(name)}${a.is_owner ? ' <span class="badge b-accent">Владелец</span>' : ''}</div>
        <div class="muted small">${a.username ? `@${esc(a.username)} · ` : ''}ID ${id}</div>
      </div>
      ${a.is_owner ? '' : `<button type="button" class="icon-btn is-danger" data-action="admin-remove" data-id="${id}" aria-label="Удалить администратора ${esc(name)}">${icon('trash')}</button>`}
    </li>`;
  }

  async function afterPasswordChange(message) {
    const st = await api.get('api/auth/status');
    state.auth = st;
    if (st.password_set && !st.authenticated) {
      showLogin(`${message}. Войдите с новым паролем.`);
      return;
    }
    toast(message, 'success');
    route();
  }

  // ======================================================================= 8j. Settings: the bot (token)
  //
  // The publishing bot = the token from .env (BOT_TOKEN) or a token set here (stored by the server,
  // it wins over .env). Changing it restarts the bot in-process: the page shows a blocking screen,
  // waits on api/health for the new bot and reloads. The token is never shown back (only a masked
  // tail) and is not kept: the field is cleared as soon as the request is sent.

  const BOT_TOKEN_RE = /^\d{5,}:[A-Za-z0-9_-]{30,}$/;
  const BOT_SWITCH_NOTE_KEY = 'pcb.bot-switch-note'; // sessionStorage: toast after the reload
  const RESTART_POLL_MS = 1000;
  const RESTART_WAIT_MS = 60000;
  const RESTART_SAME_BOT_MS = 10000;
  const HEALTH_PROBE_TIMEOUT_MS = 6000;

  const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
  const sameHandle = (a, b) => Boolean(a && b) && String(a).toLowerCase() === String(b).toLowerCase();

  function sessionSet(key, value) {
    try {
      sessionStorage.setItem(key, value);
    } catch {
      /* storage blocked: the note after the reload is simply skipped */
    }
  }

  function sessionTake(key) {
    try {
      const value = sessionStorage.getItem(key);
      sessionStorage.removeItem(key);
      return value;
    } catch {
      return null;
    }
  }

  /** The running bot and its token source from api/settings (V2.2), api/overview as a fallback. */
  function readBotState(settings, overview) {
    const pick = (o) => (o && o.bot && typeof o.bot === 'object' ? o.bot : null);
    const raw = pick(settings) || pick(overview) || {};
    const fallback = pick(overview) || {};
    const username = String(raw.username || fallback.username || '').replace(/^@/, '');
    const source = settings && ['env', 'panel'].includes(settings.bot_token_source) ? settings.bot_token_source : null;
    return {
      id: num(raw.id || fallback.id) || 0,
      username,
      name: String(raw.name || fallback.name || ''),
      source, // null: a server without the V2.2 API — no change form
      masked: settings && typeof settings.bot_token_masked === 'string' ? settings.bot_token_masked : '',
      rejected: Boolean(settings && settings.panel_token_rejected),
    };
  }

  const BOT_SOURCE_HINTS = {
    panel: 'Токен задан в панели. Токен из .env (BOT_TOKEN) остаётся запасным: бот вернётся к нему, если Telegram не примет токен из панели.',
    env: 'Токен задан в файле .env (BOT_TOKEN). Новый токен сохранится в панели, а .env останется запасным.',
  };

  function botCardHtml(b) {
    const handle = b.username ? `@${b.username}` : '';
    const title = b.name || handle || 'Бот';
    // A rejected panel token: the bot runs with the .env token whatever the stored source says.
    const running = b.rejected ? 'env' : b.source;
    const sourceBadge = running === 'panel'
      ? '<span class="badge b-accent"><span class="dot" aria-hidden="true"></span>задан в панели</span>'
      : running === 'env' ? '<span class="badge b-neutral"><span class="dot" aria-hidden="true"></span>из .env</span>' : '';
    const sourceHint = b.rejected ? '' : BOT_SOURCE_HINTS[b.source] || ''; // rejected: the callout explains
    const canRevert = b.source === 'panel' || (b.source && b.rejected);
    const revertLabel = b.rejected ? 'Удалить токен из панели' : 'Вернуть бота из .env';
    return `<section class="card span-all bot-card">
      <div class="card-head"><h2>${icon('bot')}Бот</h2></div>
      ${b.rejected ? `<p class="callout is-warn" role="alert">${icon('alert')}<span><b>Токен из панели не принят Telegram — бот работает с токеном из .env.</b> Вставьте новый токен или удалите токен из панели.</span></p>` : ''}
      <div class="bot-layout${b.source ? '' : ' is-single'}">
        <div class="bot-info">
          <div class="bot-ident">
            ${avatarHtml(b.id, title, 'lg')}
            <div class="bot-ident-text">
              <div class="bot-ident-name">${esc(title)}</div>
              <div class="muted small">${handle
                ? `<a href="https://t.me/${esc(encodeURIComponent(b.username))}" target="_blank" rel="noopener noreferrer">${esc(handle)}</a>`
                : 'Нет данных от Telegram'}${b.id ? ` · ID ${b.id}` : ''}</div>
            </div>
          </div>
          ${b.source ? `<dl class="kv">
            <dt>Токен</dt><dd class="token-cell"><code>${esc(b.masked || '—')}</code>${sourceBadge}</dd>
          </dl>
          ${sourceHint ? `<p class="hint">${esc(sourceHint)}</p>` : ''}` : ''}
          ${canRevert ? `<div class="bot-revert"><button type="button" class="btn btn-secondary" data-action="bot-revert">${icon(b.rejected ? 'trash' : 'back')}<span>${esc(revertLabel)}</span></button></div>` : ''}
        </div>
        ${b.source ? botFormHtml() : ''}
      </div>
    </section>`;
  }

  function botFormHtml() {
    return `<form id="bot-token" class="bot-form" novalidate autocomplete="off">
      <h3 class="sub-title">Сменить бота</h3>
      <div class="field">
        <label class="label" for="bot-token-input">Токен нового бота</label>
        <div class="input-wrap">
          <input class="input input-secret" type="password" id="bot-token-input" name="token" autocomplete="off"
            autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true" data-bwignore="true"
            placeholder="123456789:AA…" aria-describedby="bot-token-hint">
          <button type="button" class="icon-btn sm input-toggle" data-action="toggle-token" aria-controls="bot-token-input"
            aria-pressed="false" aria-label="Показать токен" title="Показать токен">${icon('eye')}</button>
        </div>
        <p class="hint" id="bot-token-hint">Токен выдаёт <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">@BotFather</a>: /newbot — создать нового бота, /token — токен существующего.</p>
      </div>
      <button class="btn btn-primary" type="submit">${icon('refresh')}<span>Сменить бота</span></button>
      <div class="bot-steps">
        <p class="bot-steps-title">После смены бота</p>
        <ul>
          <li>Добавьте нового бота администратором с правом публикации во все каналы — бот сам проверит каналы и пришлёт, где доступа нет.</li>
          <li>Администраторам нужно нажать /start в новом боте, иначе он не сможет присылать отчёты и превью.</li>
          <li>Медиа, присланные через старого бота, загрузите заново. Файлы, загруженные через панель, отправятся сами.</li>
        </ul>
      </div>
    </form>`;
  }

  /** Show/hide toggle of the token field (the field returns to «hidden» after every submit). */
  function setTokenVisible(btn, visible) {
    const input = btn && document.getElementById(btn.getAttribute('aria-controls') || '');
    if (!input) return;
    input.type = visible ? 'text' : 'password';
    const label = visible ? 'Скрыть токен' : 'Показать токен';
    btn.setAttribute('aria-pressed', String(visible));
    btn.setAttribute('aria-label', label);
    btn.title = label;
    btn.innerHTML = icon(visible ? 'eyeOff' : 'eye');
  }

  /** «Сменить бота»: check the format, confirm, send the token (the server asks Telegram), restart. */
  async function changeBot(form, current, rerender) {
    const input = form.elements.namedItem('token');
    const toggle = $('[data-action="toggle-token"]', form);
    const token = input.value.replace(/\s+/g, ''); // a pasted token often carries a line break
    if (!token) throw new ValidationError('Вставьте токен нового бота', '#bot-token-input');
    if (!BOT_TOKEN_RE.test(token)) throw new ValidationError('Это не похоже на токен бота (пример: 123456789:AA…)', '#bot-token-input');
    // The bot id is the part before the colon.
    if (current.id && token.split(':')[0] === String(current.id)) {
      throw new ValidationError('Это тот же бот, что работает сейчас', '#bot-token-input');
    }
    const ok = await confirmModal({
      title: 'Сменить бота?',
      html: `<p>Сменить бота на новый? Бот перезапустится, публикации по расписанию продолжатся после перезапуска.</p>
        <p class="muted small">Сначала токен проверит Telegram. После перезапуска добавьте нового бота администратором во все каналы и нажмите в нём /start.</p>`,
      confirmLabel: 'Сменить бота',
      confirmIcon: 'refresh',
      danger: true,
    });
    if (!ok) return;
    // From here on the token lives only in the request body.
    input.value = '';
    if (toggle) setTokenVisible(toggle, false);
    input.disabled = true;
    let res;
    try {
      res = await api.post('api/settings/bot-token', { token });
    } catch (err) {
      if (err instanceof ApiError) err.focus = '#bot-token-input';
      throw err;
    } finally {
      input.disabled = false;
    }
    const bot = res && res.bot && typeof res.bot === 'object' ? res.bot : {};
    const username = String(bot.username || '').replace(/^@/, '');
    if (res && res.restarting === false) {
      toast('Токен сохранён', 'success');
      rerender();
      return;
    }
    showRestartScreen({
      expect: username ? `@${username}` : null,
      previous: current.username ? `@${current.username}` : null,
      bot: { id: num(bot.id), name: String(bot.name || ''), username },
    });
  }

  /** «Вернуть бота из .env» / «Удалить токен из панели»: remove the panel token and restart. */
  async function revertBot(el, current, rerender) {
    const rejected = current.rejected;
    const ok = await confirmModal({
      title: rejected ? 'Удалить токен из панели?' : 'Вернуть бота из .env?',
      html: rejected
        ? '<p>Telegram не принял токен из панели — бот уже работает с токеном из .env и продолжит работать без перезапуска. Токен из панели будет удалён.</p>'
        : `<p>Токен из панели будет удалён, бот перезапустится с токеном из .env (BOT_TOKEN). Публикации по расписанию продолжатся после перезапуска.</p>
          <p class="muted small">Если в .env другой бот, проверьте, что он администратор во всех каналах.</p>`,
      confirmLabel: rejected ? 'Удалить токен' : 'Вернуть бота из .env',
      confirmIcon: rejected ? 'trash' : 'back',
      danger: true,
    });
    if (!ok) return;
    let res;
    try {
      res = await busy(el, () => api.del('api/settings/bot-token'));
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) rerender(); // the token is already gone
      throw err;
    }
    if (res && res.restarting === false) {
      toast('Токен из панели удалён', 'success');
      rerender();
      return;
    }
    // The server checked the .env token first and names its bot: wait for exactly that one.
    const envBot = res && res.bot && typeof res.bot === 'object' ? res.bot : {};
    const envName = String(envBot.username || '').replace(/^@/, '');
    showRestartScreen({
      expect: envName ? `@${envName}` : null,
      previous: current.username ? `@${current.username}` : null,
      bot: envName ? { id: num(envBot.id), name: String(envBot.name || ''), username: envName } : null,
      keepsBot: rejected,
      revert: true,
    });
  }

  /** api/health answer, or null while the server is down / restarting. */
  async function probeHealth() {
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), HEALTH_PROBE_TIMEOUT_MS) : 0;
    try {
      const res = await fetch('api/health', {
        cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: ctrl ? ctrl.signal : undefined,
      });
      if (!res.ok) return null;
      const data = await res.json().catch(() => null);
      return data && typeof data === 'object' ? data : null;
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Polls api/health every second for up to a minute. Done when the bot is `expect` ("@username");
   * without `expect` (back to .env: the bot is not known in advance) — when the server went down and
   * answers again, or answers with a bot other than `previous`. `keepsBot`: the bot is expected to stay
   * the same, so after RESTART_SAME_BOT_MS of unchanged answers the (missed) restart counts as done.
   * A bot that is neither `expect` nor `previous` means the restart is over but Telegram rejected the
   * new token at start, so the server fell back to the .env bot: resolves at once with `fallback`.
   * Resolves {ok, bot, fallback} where `bot` is the last bot health reported.
   */
  async function waitForBot({ expect = null, previous = null, keepsBot = false, onTick = () => {} }) {
    const started = Date.now();
    let sawDown = false;
    let sameSince = 0;
    let last = null;
    for (;;) {
      const tickAt = Date.now();
      if (tickAt - started >= RESTART_WAIT_MS) return { ok: false, bot: last };
      onTick(Math.floor((tickAt - started) / 1000));
      const data = await probeHealth();
      const bot = data && typeof data.bot === 'string' && data.bot ? data.bot : null;
      if (!data) sawDown = true;
      if (bot) {
        last = bot;
        if (expect ? sameHandle(bot, expect) : sawDown || (previous && !sameHandle(bot, previous))) return { ok: true, bot };
        if (expect && previous && !sameHandle(bot, previous)) return { ok: false, bot, fallback: true };
        if (!expect && !sawDown && (keepsBot || !previous)) {
          sameSince = sameSince || Date.now();
          if (Date.now() - sameSince >= RESTART_SAME_BOT_MS) return { ok: true, bot };
        }
      }
      await sleep(Math.max(0, RESTART_POLL_MS - (Date.now() - tickAt)));
    }
  }

  /** Blocking «Бот перезапускается…» screen; reloads the page once the bot answers. */
  function showRestartScreen({ expect = null, previous = null, bot = null, keepsBot = false, revert = false }) {
    closeDrawer();
    const box = document.createElement('div');
    box.className = 'restart-screen';
    const botLine = bot && (bot.name || bot.username)
      ? `<div class="restart-bot">${avatarHtml(bot.id, bot.name || bot.username, 'sm')}<span class="restart-bot-text">
          <b>${esc(bot.name || `@${bot.username}`)}</b>${bot.username && bot.name ? `<span class="muted small">@${esc(bot.username)}</span>` : ''}</span></div>`
      : '';
    box.innerHTML = `<div class="card restart-card" role="dialog" aria-modal="true" aria-labelledby="restart-title" aria-describedby="restart-text" tabindex="-1">
        <div class="restart-icon" data-restart-icon><span class="spinner restart-spinner" aria-hidden="true"></span></div>
        <h2 class="restart-title" id="restart-title">Бот перезапускается…</h2>
        ${botLine}
        <p class="restart-text" id="restart-text" aria-live="polite"></p>
        <p class="restart-progress muted small" data-restart-progress></p>
        <div class="restart-actions" data-restart-actions hidden>
          <button type="button" class="btn btn-secondary" data-restart="wait">${icon('clock')}<span>Подождать ещё</span></button>
          <button type="button" class="btn btn-primary" data-restart="reload">${icon('refresh')}<span>Обновить страницу</span></button>
        </div>
      </div>`;
    document.body.appendChild(box);
    $('#app').inert = true;
    document.body.classList.add('is-locked');
    const card = $('.restart-card', box);
    const iconEl = $('[data-restart-icon]', box);
    const titleEl = $('#restart-title', box);
    const textEl = $('#restart-text', box);
    const progressEl = $('[data-restart-progress]', box);
    const actionsEl = $('[data-restart-actions]', box);
    card.focus();

    const waitText = expect
      ? `Панель обновится сама, как только ${expect} ответит. Обычно это занимает несколько секунд.`
      : 'Панель обновится сама, как только бот снова ответит. Обычно это занимает несколько секунд.';
    const total = Math.round(RESTART_WAIT_MS / 1000);

    const run = async () => {
      box.classList.remove('is-failed');
      iconEl.innerHTML = '<span class="spinner restart-spinner" aria-hidden="true"></span>';
      titleEl.textContent = 'Бот перезапускается…';
      textEl.textContent = waitText;
      actionsEl.hidden = true;
      card.focus();
      const result = await waitForBot({
        expect,
        previous,
        keepsBot,
        onTick: (secs) => { progressEl.textContent = `Ждём ответа бота: ${secs} с из ${total}`; },
      });
      if (result.ok || result.fallback) {
        progressEl.textContent = '';
        textEl.textContent = 'Бот запущен — обновляем страницу…';
        let note;
        if (result.fallback) {
          note = { tone: 'error', text: `Telegram не принял новый токен при запуске — бот вернулся к токену из .env и публикует ${result.bot}.` };
        } else if (revert || !expect) {
          note = { tone: 'success', text: `Бот перезапущен с токеном из .env: публикует ${result.bot}.` };
        } else {
          note = { tone: 'success', text: `Бот сменён: теперь публикует ${result.bot}. Добавьте его администратором во все каналы и нажмите в нём /start.` };
        }
        sessionSet(BOT_SWITCH_NOTE_KEY, JSON.stringify(note));
        location.reload();
        return;
      }
      box.classList.add('is-failed');
      iconEl.innerHTML = icon('alert');
      titleEl.textContent = 'Бот не ответил за минуту';
      progressEl.textContent = '';
      let reason;
      if (expect && result.bot && sameHandle(result.bot, previous)) {
        reason = revert
          ? `Сейчас отвечает прежний бот ${result.bot} — перезапуск ещё не закончился (бот дожидается текущих публикаций).`
          : `Сейчас отвечает прежний бот ${result.bot}: перезапуск ещё не закончился (бот дожидается текущих публикаций) или Telegram не принял новый токен при запуске.`;
      } else if (expect) {
        reason = `${revert ? 'Бот из .env' : 'Новый бот'} ${expect} пока не ответил.`;
      } else {
        reason = 'Бот пока не ответил после перезапуска.';
      }
      textEl.textContent = `${reason} Проверьте логи бота на сервере — там видна причина (например, нет связи с Telegram).`;
      actionsEl.hidden = false;
      const reload = $('[data-restart="reload"]', box);
      if (reload) reload.focus();
    };

    box.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-restart]');
      if (!btn) return;
      if (btn.dataset.restart === 'reload') location.reload();
      else run();
    });
    run();
  }

  // ======================================================================= 9. Shell & boot

  /** Content of <meta name="…"> (the server fills pcb-hub and pcb-version into index.html). */
  function metaContent(name) {
    const el = document.querySelector(`meta[name="${name}"]`);
    return el ? (el.getAttribute('content') || '').trim() : '';
  }

  let APP_VERSION = metaContent('pcb-version');

  function setVersion(value) {
    if (!APP_VERSION && value) APP_VERSION = String(value);
    const el = $('#app-version');
    if (el) el.textContent = APP_VERSION ? `v${APP_VERSION}` : '';
  }

  // ---- theme: localStorage "panel-theme" = light | dark | system, shared by all panels of the
  // origin; index.html applies it before the first paint (and migrates the old "pcb.theme").

  const THEME_KEY = 'panel-theme';
  const THEMES = ['system', 'light', 'dark'];
  const THEME_LABELS = { system: 'Как в системе', light: 'Светлая', dark: 'Тёмная' };
  const THEME_ICONS = { system: 'monitor', light: 'sun', dark: 'moon' };
  let currentTheme = 'system';

  function readTheme() {
    const t = storageGet(THEME_KEY);
    return THEMES.includes(t) ? t : 'system';
  }

  function applyTheme(value) {
    const rootEl = document.documentElement;
    if (value === 'light' || value === 'dark') rootEl.dataset.theme = value;
    else delete rootEl.dataset.theme;
  }

  const getTheme = () => currentTheme;

  function setTheme(value) {
    currentTheme = THEMES.includes(value) ? value : 'system';
    storageSet(THEME_KEY, currentTheme);
    applyTheme(currentTheme);
    paintThemeControls();
  }

  /** Sidebar toggle icon + the segmented control on the settings page. */
  function paintThemeControls() {
    const btn = $('#theme-toggle');
    if (btn) {
      btn.innerHTML = icon(THEME_ICONS[currentTheme]);
      const label = `Тема: ${THEME_LABELS[currentTheme].toLowerCase()}. Нажмите, чтобы сменить`;
      btn.title = label;
      btn.setAttribute('aria-label', label);
    }
    $$('[data-action="theme"]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.theme === currentTheme)));
  }

  // ---- off-canvas sidebar below 960 px

  const drawerQuery = window.matchMedia('(max-width: 959.98px)');

  function openDrawer() {
    if (!drawerQuery.matches) return;
    document.body.classList.add('nav-open');
    $('#backdrop').hidden = false;
    $('.main').inert = true;
    const burger = $('#burger');
    burger.setAttribute('aria-expanded', 'true');
    burger.setAttribute('aria-label', 'Закрыть меню');
    const target = $('#sidebar .nav-item[aria-current="page"]') || $('#sidebar .nav-item');
    if (target) target.focus({ preventScroll: true });
  }

  function closeDrawer({ focusBurger = false } = {}) {
    if (!document.body.classList.contains('nav-open')) return;
    document.body.classList.remove('nav-open');
    $('#backdrop').hidden = true;
    $('.main').inert = false;
    const burger = $('#burger');
    burger.setAttribute('aria-expanded', 'false');
    burger.setAttribute('aria-label', 'Открыть меню');
    if (focusBurger) burger.focus();
  }

  // ---- global status pill: api/health every 60 s (the endpoint needs no login)

  const HEALTH_EVERY_MS = 60000;
  let healthTimer = 0;
  let healthTip = '';

  async function refreshHealth() {
    clearTimeout(healthTimer);
    healthTimer = setTimeout(refreshHealth, HEALTH_EVERY_MS);
    let data = null;
    try {
      const res = await fetch('api/health', { cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'application/json' } });
      data = await res.json().catch(() => null);
      // A non-OK answer keeps its body when it is the bot's own report (503 «База данных
      // недоступна»): the bot is up and names the problem. Anything else = no answer from the bot.
      if (!res.ok && !(data && (data.status === 'degraded' || data.status === 'error'))) data = null;
    } catch {
      data = null;
    }
    renderHealth(data);
  }

  function renderHealth(d) {
    const pill = $('#status-pill');
    if (!pill) return;
    let tone;
    let label;
    const lines = [];
    if (d && d.status === 'ok') {
      tone = 'success';
      label = 'Работает';
      lines.push(d.bot ? `Бот ${d.bot} работает` : 'Бот работает');
    } else if (d && d.status === 'degraded') {
      tone = 'warning';
      label = 'Есть проблемы';
      const problems = Array.isArray(d.problems) ? d.problems.filter((x) => typeof x === 'string' && x) : [];
      lines.push(...(problems.length ? problems : ['Бот работает с ошибками — подробности в логах']));
    } else {
      tone = 'danger';
      label = 'Ошибка';
      const problems = d && Array.isArray(d.problems) ? d.problems.filter((x) => typeof x === 'string' && x) : [];
      lines.push(...(problems.length ? problems : ['Нет ответа от бота. Проверьте, что он запущен.']));
      if (d && d.version) setVersion(d.version);
    }
    if (d && (d.status === 'ok' || d.status === 'degraded')) {
      if (Number.isFinite(Number(d.scheduled))) lines.push(`Публикаций в контент-плане: ${fmtNum(d.scheduled)}`);
      if (Number(d.failed_24h) > 0) lines.push(`Не опубликовано за сутки: ${fmtNum(d.failed_24h)}`);
      if (d.pending_channels !== undefined) updatePendingBadge(num(d.pending_channels));
      if (d.version) setVersion(d.version);
    }
    healthTip = lines.join('\n');
    pill.className = `status-pill badge b-${tone}`;
    pill.innerHTML = `<span class="dot" aria-hidden="true"></span><span class="status-label">${esc(label)}</span>`;
    pill.title = healthTip;
    pill.setAttribute('aria-label', `Состояние бота: ${label}. ${lines.join('. ')}`);
  }

  /** Hub link, version, theme toggle, drawer and status pill. */
  function initShell() {
    const hub = metaContent('pcb-hub');
    const link = $('#hub-link');
    if (link && hub && /^(\/|\.{1,2}\/|https?:\/\/)/i.test(hub)) {
      link.href = hub;
      link.hidden = false;
    }
    setVersion('');

    currentTheme = readTheme();
    applyTheme(currentTheme);
    paintThemeControls();
    $('#theme-toggle').addEventListener('click', () => {
      setTheme(THEMES[(THEMES.indexOf(currentTheme) + 1) % THEMES.length]);
    });
    // Another panel of the same origin changed the shared theme.
    window.addEventListener('storage', (e) => {
      if (e.key !== THEME_KEY) return;
      currentTheme = readTheme();
      applyTheme(currentTheme);
      paintThemeControls();
    });

    $('#burger').addEventListener('click', () => {
      if (document.body.classList.contains('nav-open')) closeDrawer();
      else openDrawer();
    });
    $('#backdrop').addEventListener('click', () => closeDrawer({ focusBurger: true }));
    $('#sidebar').addEventListener('click', (e) => {
      if (e.target.closest('a[href]')) closeDrawer();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && document.body.classList.contains('nav-open') && !document.querySelector('dialog[open]')) {
        closeDrawer({ focusBurger: true });
      }
    });
    const onQuery = () => {
      if (!drawerQuery.matches) closeDrawer();
    };
    if (drawerQuery.addEventListener) drawerQuery.addEventListener('change', onQuery);
    else drawerQuery.addListener(onQuery);

    // Tooltips do not work on touch screens: a tap on the pill re-checks and shows the details.
    $('#status-pill').addEventListener('click', async () => {
      await refreshHealth();
      if (healthTip) toast(healthTip.split('\n').join(' · '), $('#status-pill').classList.contains('b-success') ? 'info' : 'error');
    });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') refreshHealth();
    });
    refreshHealth();
  }

  function applyOverviewToShell(ov) {
    if (ov && ov.stats && ov.stats.channels_pending !== undefined) updatePendingBadge(num(ov.stats.channels_pending));
    if (ov && ov.timezone) state.serverTz = ov.timezone;
    if (ov && ov.bot) state.botUsername = ov.bot.username || '';
  }

  /** Background refresh of the sidebar (pending channels badge, the bot's timezone). */
  function refreshShell() {
    api.get('api/overview').then(applyOverviewToShell).catch(() => { /* not critical */ });
  }

  function fillStaticIcons() {
    $$('[data-icon]').forEach((el) => {
      el.innerHTML = icon(el.dataset.icon);
    });
  }

  function bindGlobalEvents() {
    window.addEventListener('hashchange', onHashChange);
    // The skip link must not change the hash (the router would treat "#view" as a page).
    const skip = $('.skip-link');
    if (skip) {
      skip.addEventListener('click', (e) => {
        e.preventDefault();
        $('#view').focus();
      });
    }
    // Media that cannot be shown (deleted file, Telegram file over 20 MB…): a video falls back to
    // an image, an image to a placeholder. `error` does not bubble — listen in the capture phase.
    document.addEventListener('error', onMediaError, true);
    window.addEventListener('beforeunload', (e) => {
      if (state.composer && state.composer.dirty) {
        e.preventDefault();
        e.returnValue = '';
      }
    });
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.code === 'KeyS' && state.composer && !state.loginShown) {
        e.preventDefault();
        if (state.composer.shortcut) state.composer.shortcut();
      }
    });
    // A file dropped outside the drop zone must not navigate away from the panel.
    const isFileDrag = (e) => Array.from((e.dataTransfer && e.dataTransfer.types) || []).includes('Files');
    window.addEventListener('dragover', (e) => {
      if (isFileDrag(e)) e.preventDefault();
    });
    window.addEventListener('drop', (e) => {
      if (isFileDrag(e)) e.preventDefault();
    });
    $('#login-form').addEventListener('submit', (e) => {
      onLoginSubmit(e).catch(handleError);
    });
  }

  async function boot() {
    // Hash links resolve against <base>: keep the page URL equal to it (no stray query string).
    if (location.search) history.replaceState(null, '', `${location.pathname}${location.hash}`);
    fillStaticIcons();
    initShell();
    bindGlobalEvents();
    try {
      const st = await api.get('api/auth/status', { allow401: true });
      state.auth = {
        password_set: Boolean(st.password_set),
        authenticated: Boolean(st.authenticated),
        password_source: st.password_source || null,
      };
    } catch (err) {
      $('#view').innerHTML = `<div class="page">${errorStateHtml(err)}</div>`;
      const retry = $('[data-retry]');
      if (retry) retry.addEventListener('click', () => location.reload());
      return;
    }
    // The page was reloaded after the bot was changed (see showRestartScreen).
    const botNote = sessionTake(BOT_SWITCH_NOTE_KEY);
    if (botNote) {
      let note = { tone: 'success', text: botNote };
      try {
        const parsed = JSON.parse(botNote);
        if (parsed && typeof parsed.text === 'string') note = parsed;
      } catch {
        /* a plain text note */
      }
      toast(note.text, note.tone === 'error' ? 'error' : 'success', 10000);
    }
    if (state.auth.password_set && !state.auth.authenticated) {
      showLogin();
      return;
    }
    refreshShell();
    route();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
