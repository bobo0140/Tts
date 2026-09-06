/* Свързва интерфейса с Python. Всяка контрола праща стойността си
   към engine-а, а състоянието се дърпа периодично. */

let api = {};
let S = {};                 // текущи настройки
let ready = false;

const PROFILES = ["Само TTS", "Само Live AI", "Пълно"];
const OUTPUTS  = ["TTS гласове", "Само Live AI", "И двете"];

const PAGES = {
  dash:    { icon: "📊", label: "Табло" },
  home:    { icon: "🏠", label: "Начало" },
  filters: { icon: "🛡", label: "Филтри" },
  voice:   { icon: "🔊", label: "Глас" },
  ai:      { icon: "🤖", label: "AI" },
  test:    { icon: "🧪", label: "Тест" },
};

const PAGES_BY_PROFILE = {
  "Само TTS":     ["dash", "home", "filters", "voice", "test"],
  "Само Live AI": ["dash", "home", "filters", "ai", "test"],
  "Пълно":        ["dash", "home", "filters", "voice", "ai", "test"],
};

let page = "home";

/* ---------- малки помощници за изграждане ---------- */
const el = (h) => { const d = document.createElement("div"); d.innerHTML = h.trim(); return d.firstChild; };

function card(title, sub, inner) {
  return `<div class="card"><h2>${title}</h2>${sub ? `<p class="sub">${sub}</p>` : ""}${inner}</div>`;
}
function row(label, inner) {
  return `<div class="row">${label ? `<label>${label}</label>` : ""}<div class="grow">${inner}</div></div>`;
}
function text(key, ph = "", pw = false) {
  return `<input type="${pw ? "password" : "text"}" data-k="${key}" placeholder="${ph}">`;
}
function num(key) { return `<input type="text" class="sm" data-k="${key}">`; }
function check(key, label) {
  return `<label class="check"><input type="checkbox" data-k="${key}"><span>${label}</span></label>`;
}
function select(key, opts) {
  return `<select data-k="${key}">${opts.map(o => `<option>${o}</option>`).join("")}</select>`;
}
function slider(key, min, max, step, fmt) {
  return `<div class="row" style="margin:0"><input type="range" data-k="${key}" data-fmt="${fmt}"
    min="${min}" max="${max}" step="${step}" class="grow"><span class="val" data-for="${key}"></span></div>`;
}
function hint(t) { return `<p class="hint">${t}</p>`; }
function btn(label, call, cls = "") { return `<button class="${cls}" onclick="${call}">${label}</button>`; }

/* ---------- страници ---------- */
function pageHome() {
  return card("Връзка с TikTok", "Директно, или през TikFinity ако директната се къса.",
      row("Начин на свързване", select("connection_mode", ["Директно (TikTok)", "TikFinity (Advanced)"]))
    + row("TikTok потребител", text("username_entry", "напр. someusername (без @)"))
    + row("Euler Stream ключ", text("api_key_entry", "по избор"))
    + row("TikFinity адрес", text("tikfinity_url_entry"))
  )
  + card("Лог", "Коментари, филтри, AI отговори и грешки.",
      `<div class="logwrap" id="log"></div>
       <div class="toolbar">
         ${btn("Изчисти", "api.clear_log()")}
         ${btn("📋 Копирай", "copyLog()")}
         ${btn("💾 Запази", "api.save_log()")}
         ${btn("🔄 Нулирай статистиката", "api.reset_stats()")}
       </div>`
  );
}

function pageFilters() {
  return card("Съдържание на коментарите", "",
      row("", check("filter_var", "Забранени думи"))
    + row("Списък", text("filter_entry", "дума1, дума2, дума3"))
    + check("strip_mentions_var", "Пропускай @споменавания")
    + check("shlyokavitsa_var", "Конвертирай шльокавица (Zdravei → Здравей)")
  )
  + card("Спам и злоупотреби", "",
      check("spam_filter_var", "Анти-спам защита")
    + check("heart_me_filter_var", "Чети само от Heart Me донори + абонати")
  )
  + card("Дължина", "",
      row("Макс. символи", num("max_chars_entry"))
    + check("skip_instead_of_truncate_var", "Пропускай изцяло по-дългите")
    + row("Макс. дължина на име", num("max_name_len_entry"))
    + hint("Имена по-дълги от това, или само от символи и цифри, не се обявяват.")
  );
}

function pageVoice() {
  const voices = (S._voices || []);
  return card("Избор на глас", "Dimitar работи офлайн. Borislav и Kalina минават през интернет.",
      row("Глас", select("voice_engine_menu", voices))
    + check("voice_shuffle_var", "Разбъркай гласовете")
    + row("В разбъркването", (S._voice_keys || []).map(k =>
        `<label class="check" style="display:inline-flex;margin-right:16px">
           <input type="checkbox" data-shuffle="${k}"><span>${S._voice_short[k]}</span></label>`).join(""))
  )
  + card("Как звучи", "По-ниска скорост = по-бърз говор.",
      row("Скорост", slider("speed_slider", 0.6, 1.4, 0.01, "x2"))
    + row("Изразителност", slider("expressiveness_slider", 0.3, 1.3, 0.01, "x2"))
    + row("Сила на звука", slider("volume_slider", 0.05, 3, 0.01, "mult"))
    + row("Ефект", select("voice_effect_menu", ["Няма","Дълбок глас","Чипмънк","Ехо","Робот","Реверберация"]))
    + `<div class="toolbar">${btn("🔊 Пробвай гласа","api.preview_voice()","primary")}
        ${btn("↺ Върни гласовите настройки","api.reset_voice_settings()")}</div>`
  )
  + card("Гласови обявявания", "Какво друго да казва освен коментарите.",
      check("announce_follow_var", "Нови последователи")
    + check("announce_share_var", "Споделяния (веднъж на човек)")
    + check("announce_gift_var", "Подаръци (без Heart Me)")
    + check("announce_viewers_var", "Брой зрители")
    + row("На всеки (сек)", num("viewer_interval_entry"))
  );
}

function pageAI() {
  return card("Gemini ключ", "Един ключ обслужва и текстовия AI, и Live AI. Взима се безплатно.",
      `<div class="row"><label>API ключ</label><div class="grow">${text("gemini_api_key_entry","",true)}</div>
       ${btn("Вземи ключ ↗","api.open_key_page()")}</div>`
    + row("Твоето име", text("streamer_name_entry", "AI-то ще те заговаря по име"))
  )
  + card("Характер на AI-то", "Важи и за текстовия коментатор, и за Live AI.",
      row("Готов характер", select("personality_menu", S._personalities || []))
    + row("Колко да се шегува", slider("humor_slider", 0, 100, 1, "pct"))
    + `<label style="font-size:12px;color:var(--muted)">Опиши характера със свои думи:</label>
       <textarea data-k="custom_prompt_entry" placeholder="Ти си шегаджия, играя Fortnite, закачай ме за лошите игри…"></textarea>`
  )
  + card("AI коментатор (текст)", "Реагира кратко на коментарите.",
      check("ai_enabled_var", "Активирай AI коментатор")
    + check("ai_speak_var", "Изговаряй отговорите на глас")
    + row("Модел", select("gemini_model_entry", S._text_models || []))
    + row("Реагирай на", select("ai_frequency_menu", ["Всеки коментар","На всеки N-ти"]))
    + row("N =", num("ai_every_n_entry"))
    + row("Мин. дължина", num("ai_min_chars_entry"))
    + hint("Късите коментари не се пращат — само хабят лимит. flash-lite издържа най-много заявки.")
  )
  + card("Live AI (говор в реално време)", "AI-то говори със собствен глас и може да те слуша.",
      `<div class="toolbar" style="margin:0 0 14px">
         ${btn("Свържи Live AI","api.start_live_ai()","primary")}
         ${btn("Спри","api.stop_live_ai()")}</div>`
    + row("Live модел", select("live_model_entry", S._live_models || []))
    + row("Глас на AI-то", select("live_voice_menu", ["Puck","Charon","Kore","Fenrir","Aoede"]))
    + row("Сила на звука", slider("live_volume_slider", 0.05, 2.5, 0.01, "mult"))
    + check("live_autoreconnect_var", "Пресвързвай се автоматично")
    + `<p class="sub" style="margin:14px 0 4px">Какво да подава на AI-то:</p>`
    + check("live_feed_follow_var", "Нови последователи")
    + check("live_feed_share_var", "Споделяния")
    + check("live_feed_gift_var", "Подаръци")
    + check("live_feed_comment_var", "Коментари")
    + row("На всеки N-ти", num("live_every_n_entry"))
    + check("live_feed_viewers_var", "Брой зрители")
    + row("Макс. изчакване (сек)", num("live_batch_seconds_entry"))
    + hint("При единично събитие реагира за ~1 секунда. Стойността е таван при наплив.")
  )
  + card("Микрофон", "За да те слуша Live AI-то.",
      check("live_mic_var", "Пусни микрофона")
    + check("half_duplex_var", "Избягвай ехо (спира микрофона докато AI-то говори)")
    + `<div class="row"><label>Устройство</label><div class="grow">${select("mic_device_menu", S._mic_devices || ["(по подразбиране)"])}</div>${btn("Опресни","api.refresh_mic_devices()")}</div>`
    + `<div class="row"><label>Изход за звука</label><div class="grow">${select("output_device_menu", S._out_devices || ["(по подразбиране)"])}</div>${btn("Опресни","api.refresh_output_devices()")}</div>`
    + row("Клавиш микрофон", num("hotkey_entry") + " " + `<span style="display:inline-block;width:200px">${select("hotkey_mode_menu",["Задръж за говорене","Вкл./изкл. с натискане"])}</span>`)
    + row("Клавиш заглушаване", num("mute_hotkey_entry"))
    + `<div class="toolbar">${btn("Активирай клавишите","api.toggle_hotkey()")}</div>`
    + hint("Клавишите работят и когато прозорецът не е на фокус. Примери: f8, ctrl+shift+m.")
  );
}

function pageTest() {
  const b = (t, m) => btn(t, `api.${m}()`);
  return card("Тествай без истински лайв", "Минава през същите филтри и гласове.",
      row("Име за тест", text("test_name_entry"))
    + row("Коментар за тест", text("test_comment_entry"))
    + `<div class="grid" style="margin-top:14px">
        ${b("💬 Коментар","test_comment")} ${b("➕ Нов последовател","test_follow")}
        ${b("🔁 Споделяне","test_share")} ${b("🎁 Подарък","test_gift")}
        ${b("❤️ Heart Me","test_heart_me")} ${b("👁 Брой зрители","test_viewers")}
        ${b("⚠️ Спам","test_spam")} ${b("🔂 Серия подаръци","test_gift_streak")}
      </div>`
  )
  + card("Диагностика", "Debug показва суровите заявки към Gemini.",
      check("debug_var", "Debug режим")
    + `<div class="toolbar">
        ${btn("🔬 Пълна проверка на API","api.test_api_full()","primary")}
        ${b("🔌 Връзка с Gemini","test_gemini_connection")}
        ${b("🤖 AI коментатор","test_ai_commentator")}
        ${b("🎙 Тест микрофон","test_microphone")}
        ${b("🔈 Тест на звука","test_audio_output")}
      </div>`
  )
  + card("Симулатор на наплив", "Проверява групирането при много събития наведнъж.",
      row("Брой събития", num("burst_count_entry"))
    + `<div class="grid">
        ${b("👥 Последователи","test_burst_follows")} ${b("🔁 Споделяния","test_burst_shares")}
        ${b("🌹 Подаръци","test_burst_gifts")} ${b("💬 Коментари","test_burst_comments")}
      </div>`
  )
  + card("Настройки", "",
      `<div class="toolbar">
        ${btn("⚡ Оптимални настройки","api.apply_optimal_settings()","primary")}
        ${btn("💾 Запази сега","api.save_settings()")}
        ${btn("🧹 Изчисти тест паметта","api.test_reset()")}
        ${btn("↺ Върни фабричните","api.restore_defaults()","danger")}
      </div>`
  );
}

const BUILDERS = { dash: pageDash, home: pageHome, filters: pageFilters, voice: pageVoice, ai: pageAI, test: pageTest };

/* ---------- Табло ---------- */
function pageDash() {
  return `<div id="dash-grid" class="dash">
    <div class="card"><h2>Зареждане…</h2></div></div>`;
}

const ST_ICON = { ok: "🟢", warn: "🟡", err: "🔴", idle: "⚪" };

function mod(title, m, rows) {
  return `<div class="card mod">
    <h2>${ST_ICON[m.state] || "⚪"} ${title}</h2>
    <p class="sub">${esc(m.note || "")}</p>
    <div class="kv">${rows.filter(Boolean).map(([k, v, cls]) =>
      `<div><span>${k}</span><b class="${cls || ""}">${v}</b></div>`).join("")}</div>
  </div>`;
}

function renderDash(M) {
  const box = document.getElementById("dash-grid");
  if (!box) return;
  const a = M.api, t = M.tiktok, v = M.voice, l = M.live, f = M.filters;

  const reasons = Object.entries(a.reasons || {});
  const reasonTxt = reasons.length
    ? reasons.map(([k, n]) => `${k}: ${n}`).join(", ") : "няма";

  box.innerHTML =
    mod("Gemini API", a, [
      ["Заявки общо", `${a.total}`],
      ["Успешни / Откази", `<span class="g">${a.ok}</span> / <span class="${a.failed ? "r" : ""}">${a.failed}</span>`],
      ["Последна минута", `${a.min_ok} ок, ${a.min_fail} откази`],
      ["Последен час", `${a.hour_ok} ок, ${a.hour_fail} откази`],
      ["Средно време", a.avg_ms ? `${a.avg_ms} ms` : "—"],
      ["Най-бавна", a.slowest_ms ? `${a.slowest_ms} ms` : "—"],
      ["Причини за отказ", reasonTxt],
      a.backoff_left ? ["Отдръпване", `${a.backoff_left} сек`, "r"] : null,
      ["Достъпни модели", a.available.length ? `${a.available.length}` : "непроверени"],
      a.last_error ? ["Последна грешка", `${esc(a.last_error)} (преди ${a.last_error_ago} сек)`, "r"] : null,
    ]) +
    mod("TikTok", t, [
      ["Източник", esc(t.source)],
      ["Зрители", t.viewers],
      ["Последователи", t.follows],
      ["Споделяния", t.shares],
      ["Подаръци", t.gifts],
      ["Коментари", t.comments],
    ]) +
    mod("Глас", v, [
      ["Глас", esc(v.voice) + (v.shuffle ? " (разбъркване)" : "")],
      ["Ефект", esc(v.effect)],
      ["Опашка", v.queue],
      ["Режим", esc(v.mode)],
      ["Заглушено", v.muted ? "<span class='r'>да</span>" : "не"],
    ]) +
    mod("Live AI", l, [
      ["Модел", esc(l.model)],
      ["Ниво на setup", l.setup_level],
      ["Чакащи събития", l.buffered],
      ["Микрофон", l.mic ? "<span class='g'>включен</span>" : "изключен"],
      ["Изпратен звук", l.mic_chunks ? `${(l.mic_chunks * 0.1).toFixed(1)} сек` : "—"],
      ["В момента говори", l.speaking ? "AI-то" : "никой"],
    ]) +
    mod("Филтри", f, [
      ["Анти-спам", f.spam ? "вкл." : "изкл."],
      ["Забранени думи", f.words ? "вкл." : "изкл."],
      ["@споменавания", f.mentions ? "махат се" : "четат се"],
      ["Шльокавица", f.shlyokavitsa ? "превежда се" : "не"],
      ["Само Heart Me", f.heart_me ? "вкл." : "изкл."],
      ["Макс. символи", esc(String(f.max_chars))],
    ]);
}

/* ---------- рендиране ---------- */
function renderNav() {
  const allowed = PAGES_BY_PROFILE[S.profile] || PAGES_BY_PROFILE["Пълно"];
  if (!allowed.includes(page)) page = allowed[0];
  document.getElementById("nav").innerHTML = allowed.map(k =>
    `<button class="${k === page ? "on" : ""}" onclick="go('${k}')">
       <span class="ico">${PAGES[k].icon}</span>${PAGES[k].label}</button>`).join("");
}

function renderPage() {
  document.getElementById("main").innerHTML = `<div class="page on">${BUILDERS[page]()}</div>`;
  bind();
  applyValues();
  if (page === "home") renderLog();
}

function go(k) { page = k; renderNav(); renderPage(); }

function renderSegs() {
  document.getElementById("seg-profile").innerHTML = PROFILES.map(p =>
    `<button class="${S.profile === p ? "on" : ""}" onclick="setProfile('${p}')">${p}</button>`).join("");
  document.getElementById("seg-output").innerHTML = OUTPUTS.map(o =>
    `<button class="${S.output_mode === o ? "on" : ""}" onclick="setKey('output_mode','${o}')">${o}</button>`).join("");
}

function setProfile(p) { S.profile = p; api.set_profile(p); renderSegs(); renderNav(); renderPage(); }
function setKey(k, v) { S[k] = v; api.set_setting(k, v); renderSegs(); }

/* ---------- свързване на контролите ---------- */
function bind() {
  document.querySelectorAll("[data-k]").forEach(n => {
    const k = n.dataset.k;
    const ev = (n.type === "checkbox" || n.tagName === "SELECT" || n.type === "range") ? "change" : "input";
    n.addEventListener(ev, () => {
      const v = n.type === "checkbox" ? n.checked : (n.type === "range" ? parseFloat(n.value) : n.value);
      S[k] = v;
      api.set_setting(k, v);
      if (n.type === "range") showVal(n);
    });
    if (n.type === "range") n.addEventListener("input", () => showVal(n));
  });
  document.querySelectorAll("[data-shuffle]").forEach(n => {
    n.addEventListener("change", () => api.set_shuffle(n.dataset.shuffle, n.checked));
  });
}

function showVal(n) {
  const out = document.querySelector(`[data-for="${n.dataset.k}"]`);
  const v = parseFloat(n.value);
  if (out) out.textContent = n.dataset.fmt === "pct" ? Math.round(v) + "%"
                          : n.dataset.fmt === "mult" ? v.toFixed(2) + "x" : v.toFixed(2);
  const pct = (v - n.min) / (n.max - n.min) * 100;
  n.style.setProperty("--p", pct + "%");
}

function applyValues() {
  document.querySelectorAll("[data-k]").forEach(n => {
    const v = S[n.dataset.k];
    if (v === undefined) return;
    if (n.type === "checkbox") n.checked = !!v;
    else n.value = v;
    if (n.type === "range") showVal(n);
  });
  document.querySelectorAll("[data-shuffle]").forEach(n => {
    n.checked = !!(S._shuffle || {})[n.dataset.shuffle];
  });
}

/* ---------- лог и състояние ---------- */
let logCache = [];
function renderLog() {
  const box = document.getElementById("log");
  if (!box) return;
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.innerHTML = logCache.map(l => `<div class="${logClass(l)}">${esc(l)}</div>`).join("");
  if (near) box.scrollTop = box.scrollHeight;
}
function logClass(l) {
  if (/грешка|ГРЕШКА|СПРЯНО|✗/.test(l)) return "l-err";
  if (/✓|Свързан|работи/.test(l)) return "l-ok";
  if (/Внимание|Съвет|ЗАГЛУШ/.test(l)) return "l-warn";
  if (/\[AI|Live AI|Чух те/.test(l)) return "l-ai";
  if (/DEBUG/.test(l)) return "l-dbg";
  if (/\[Система|\[Настройки|\[Тест|\[Профил/.test(l)) return "l-sys";
  return "";
}
const esc = s => s.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function copyLog() {
  navigator.clipboard.writeText(logCache.join("\n")).catch(() => {});
  api.log_copied();
}

async function poll() {
  if (!ready) return;
  try {
    const st = await api.get_state();
    document.getElementById("st1").textContent = st.status.replace(/^[●○◌]\s*/, "");
    document.getElementById("st2").textContent = st.live_status.replace(/^[●○◌]\s*/, "");
    document.getElementById("d1").className = "dot " + st.status_kind;
    document.getElementById("d2").className = "dot " + st.live_status_kind;
    document.getElementById("s-view").textContent = st.viewers;
    document.getElementById("s-fol").textContent  = st.follows;
    document.getElementById("s-sh").textContent   = st.shares;
    document.getElementById("s-gift").textContent = st.gifts;
    document.getElementById("s-com").textContent  = st.comments;

    document.getElementById("btn-start").disabled = st.running;
    document.getElementById("btn-stop").disabled  = !st.running;
    const mb = document.getElementById("btn-mute");
    mb.textContent = st.muted ? "🔇 Заглушено" : "🔊 Заглуши";
    mb.className = st.muted ? "danger" : "ghost";

    if (page === "dash") {
      try { renderDash(await api.get_metrics()); } catch (e) {}
    }

    if (st.log_len !== logCache.length) {
      logCache = await api.get_log();
      renderLog();
    }
  } catch (e) { /* прозорецът се затваря */ }
}

async function boot() {
  if (ready) return;
  api = window.pywebview.api;
  try {
    S = await api.get_settings();
    logCache = await api.get_log();
  } catch (e) {
    document.getElementById("main").innerHTML =
      `<div class="card"><h2>Няма връзка с ядрото</h2>
       <p class="sub">${esc(String(e))}</p></div>`;
    return;
  }
  ready = true;
  renderSegs(); renderNav(); renderPage();
  setInterval(poll, 500);
  try { if (await api.needs_setup()) wizShow(); } catch (e) {}
}

// Събитието може да е минало, преди скриптът да се зареди — затова и двете.
window.addEventListener("pywebviewready", boot);
(function wait(n) {
  if (window.pywebview && window.pywebview.api) return boot();
  if (n > 100) return;
  setTimeout(() => wait(n + 1), 80);
})(0);

// Всяка грешка в интерфейса да се вижда, вместо бутонът да мълчи
window.addEventListener("error", e => {
  if (ready && api.js_error) api.js_error(String(e.message) + " @" + e.lineno);
});

/* ======================= Съветник при стартиране ======================= */
let wiz = { step: 0, mode: null, source: null };

const WIZ_LAST = 4;

function wizShow() { document.getElementById("setup").classList.add("on"); wizRender(); }
function wizHide() { document.getElementById("setup").classList.remove("on"); }

function wizPick(field, value) { wiz[field] = value; wizRender(); }

function wizRender() {
  const T = document.getElementById("w-title");
  const U = document.getElementById("w-sub");
  const B = document.getElementById("w-body");
  const next = document.getElementById("w-next");
  const back = document.getElementById("w-back");
  const skip = document.getElementById("w-skip");

  back.style.visibility = wiz.step === 0 ? "hidden" : "visible";
  skip.style.display = wiz.step === WIZ_LAST - 1 ? "" : "none";
  next.style.display = "";
  next.textContent = "Продължи";
  next.disabled = false;

  /* --- 0: какво ще ползваме --- */
  if (wiz.step === 0) {
    T.textContent = "Какво ще ползваш?";
    U.textContent = "Може да смениш по всяко време след това.";
    B.innerHTML = `
      <button class="pick ${wiz.mode === "Само TTS" ? "on" : ""}" onclick="wizPick('mode','Само TTS')">
        <span class="pi">🔊</span><span><b>Само TTS гласове</b>
        <span>Български гласове четат коментарите. Работи офлайн, без ключове, най-бързо.</span></span></button>
      <button class="pick ${wiz.mode === "Само Live AI" ? "on" : ""}" onclick="wizPick('mode','Само Live AI')">
        <span class="pi">🤖</span><span><b>Само Live AI</b>
        <span>AI-то говори със свой глас, коментира чата и може да те слуша. Нужен е безплатен Gemini ключ.</span></span></button>
      <button class="pick ${wiz.mode === "Пълно" ? "on" : ""}" onclick="wizPick('mode','Пълно')">
        <span class="pi">✨</span><span><b>И двете</b>
        <span>Гласовете четат коментарите, а AI-то коментира отгоре.</span></span></button>`;
    next.disabled = !wiz.mode;
    return;
  }

  /* --- 1: откъде идват коментарите --- */
  if (wiz.step === 1) {
    T.textContent = "Откъде да чета чата?";
    U.textContent = "TikFinity е по-стабилно, ако директната връзка се къса.";
    B.innerHTML = `
      <button class="pick ${wiz.source === "tikfinity" ? "on" : ""}" onclick="wizPick('source','tikfinity')">
        <span class="pi">🔌</span><span><b>През TikFinity<span class="tag">препоръчано</span></b>
        <span>Изисква TikFinity да е пуснат и свързан към стрийма. По-стабилно, без грешки от TikTok.</span></span></button>
      <button class="pick ${wiz.source === "direct" ? "on" : ""}" onclick="wizPick('source','direct')">
        <span class="pi">📡</span><span><b>Директно към TikTok</b>
        <span>Без допълнителни програми, но понякога дава грешки при натоварване.</span></span></button>
      <div style="margin-top:18px">${
        wiz.source === "direct"
          ? row("TikTok потребител", text("username_entry", "напр. someusername (без @)"))
          : wiz.source === "tikfinity"
          ? row("TikFinity адрес", text("tikfinity_url_entry"))
          : ""}</div>`;
    bind(); applyValues();
    next.disabled = !wiz.source;
    return;
  }

  /* --- 2: ключ (само ако трябва) --- */
  if (wiz.step === 2) {
    if (wiz.mode === "Само TTS") { wiz.step = 3; return wizRender(); }
    T.textContent = "Gemini ключ";
    U.textContent = "Безплатен е и се взима за минута — без карта.";
    B.innerHTML =
      row("API ключ", text("gemini_api_key_entry", "постави ключа тук", true))
      + row("Твоето име", text("streamer_name_entry", "AI-то ще те заговаря по име (по избор)"))
      + `<div class="toolbar">${btn("Вземи ключ ↗", "api.open_key_page()")}</div>`
      + hint("Ако ключът не се поставя с Ctrl+V, натисни с десен бутон в полето. Внимавай да не хванеш интервал.");
    bind(); applyValues();
    return;
  }

  /* --- 3: проверката --- */
  if (wiz.step === 3) {
    T.textContent = "Проверявам всичко";
    U.textContent = "Секунда — тествам връзките, преди да започнеш.";
    next.textContent = "Изчакай…";
    next.disabled = true;
    B.innerHTML = `<ul class="steps" id="w-steps"><li><span class="ic"><i class="spin"></i></span>
      <span class="nm">Започвам…</span></li></ul>`;
    startChecks();
    return;
  }

  /* --- 4: резултат --- */
  if (wiz.step === 4) {
    const ok = wiz.result && wiz.result.ok;
    T.textContent = ok ? "Всичко е наред" : "Има какво да се оправи";
    U.textContent = ok ? "Готов си за стрийм." : "Виж кое не мина по-долу.";
    B.innerHTML =
      `<div class="banner ${ok ? "ok" : "err"}">${ok
        ? "✓ Всички проверки минаха. AI-то току-що ти го каза и на глас."
        : "✗ Част от проверките не минаха. Може да продължиш и да ги оправиш после, или да се върнеш назад."}</div>`
      + stepsHtml(wiz.result ? wiz.result.steps : []);
    next.textContent = ok ? "Започваме" : "Продължи въпреки това";
    return;
  }
}

function stepsHtml(steps) {
  const ic = { ok: "✅", err: "❌", warn: "⚠️", run: '<i class="spin"></i>' };
  return `<ul class="steps">` + steps.map(s =>
    `<li><span class="ic">${ic[s.status] || ""}</span>
     <span class="nm">${esc(s.name)}${s.detail ? `<small>${esc(s.detail)}</small>` : ""}</span></li>`
  ).join("") + `</ul>`;
}

async function startChecks() {
  await api.run_wizard(wiz.mode);
  const tick = setInterval(async () => {
    const st = await api.wizard_state();
    const box = document.getElementById("w-steps");
    if (box) box.outerHTML = stepsHtml(st.steps).replace("<ul", '<ul id="w-steps"');
    if (st.done) {
      clearInterval(tick);
      wiz.result = st;
      wiz.step = 4;
      wizRender();
    }
  }, 400);
}

function wizNext() {
  if (wiz.step === WIZ_LAST) return wizFinish();
  wiz.step++;
  if (wiz.step === 2 && wiz.mode === "Само TTS") wiz.step = 3;
  wizRender();
}

function wizBack() {
  wiz.step = Math.max(0, wiz.step - 1);
  if (wiz.step === 2 && wiz.mode === "Само TTS") wiz.step = 1;
  wizRender();
}

function wizSkip() { wiz.step = 4; wiz.result = { ok: true, steps: [] }; wizRender(); }

async function wizFinish() {
  if (wiz.source) {
    const cm = wiz.source === "tikfinity" ? "TikFinity (Advanced)" : "Директно (TikTok)";
    S.connection_mode = cm;
    await api.set_setting("connection_mode", cm);
  }
  await api.set_profile(wiz.mode);
  await api.finish_setup();
  S = await api.get_settings();
  wizHide();
  renderSegs(); renderNav(); renderPage();
}
