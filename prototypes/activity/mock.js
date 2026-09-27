let NOW = 18 * 60 + 10;
let DAY_KICKER = "Воскресенье";
let DAY_TITLE = "27 сентября";
let NOW_LABEL = "18:10";
const STORAGE_KEY = "activity-mock-category-colors";

const CATEGORIES = [
  { id: "games", name: "Компьютерные игры", color: "#6d4aff" },
  { id: "work", name: "Работа", color: "#1a73e8" },
  { id: "study", name: "Учёба", color: "#0d904f" },
  { id: "browser", name: "Браузер", color: "#e8710a" },
];

const BLOCKS = [
  {
    category: "work",
    start: "08:30",
    end: "09:40",
    items: [
      { name: "Почта", start: "08:30", end: "09:10" },
      { name: "Редактор", start: "09:10", end: "09:40" },
    ],
  },
  {
    category: "games",
    start: "10:00",
    end: "16:00",
    items: [
      { name: "Dota 2", start: "10:00", end: "15:00" },
      { name: "Counter-Strike 2", start: "15:00", end: "16:00" },
    ],
  },
  {
    category: "browser",
    start: "16:20",
    end: "17:10",
    items: [
      { name: "Вастрик.Клуб", start: "16:20", end: "16:50" },
      { name: "Документация", start: "16:50", end: "17:10" },
    ],
  },
  {
    category: "study",
    start: "18:00",
    end: "19:30",
    items: [
      { name: "Конспект", start: "18:00", end: "18:50" },
      { name: "Задачи", start: "18:50", end: "19:30" },
    ],
  },
];

const LOG = [
  { time: "08:32", category: "work", app: "Почта", title: "Входящие — 4 письма", device: "MacBook" },
  { time: "08:41", category: "work", app: "Почта", title: "Черновик: отчёт", device: "MacBook" },
  { time: "09:12", category: "work", app: "Редактор", title: "activity.py", device: "MacBook" },
  { time: "09:28", category: "work", app: "Редактор", title: "client.py", device: "MacBook" },
  { time: "10:02", category: "games", app: "Dota 2", title: "Главное меню", device: "Windows" },
  { time: "11:15", category: "games", app: "Dota 2", title: "Матч", device: "Windows" },
  { time: "13:40", category: "games", app: "Dota 2", title: "Матч", device: "Windows" },
  { time: "14:55", category: "games", app: "Dota 2", title: "Экран итогов", device: "Windows" },
  { time: "15:01", category: "games", app: "Counter-Strike 2", title: "Поиск матча", device: "Windows" },
  { time: "15:36", category: "games", app: "Counter-Strike 2", title: "Матч", device: "Windows" },
  { time: "16:22", category: "browser", app: "Safari", title: "Вастрик.Клуб — тред", device: "MacBook" },
  { time: "16:51", category: "browser", app: "Safari", title: "Документация", device: "MacBook" },
  { time: "18:06", category: "study", app: "Заметки", title: "Конспект, лекция 4", device: "MacBook" },
  { time: "18:52", category: "study", app: "Заметки", title: "Список задач", device: "MacBook" },
];

function esc(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

function mins(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

function hexToRgb(hex) {
  const raw = hex.replace("#", "");
  const full = raw.length === 3 ? raw.split("").map((c) => c + c).join("") : raw;
  const n = parseInt(full, 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

function mixWhite(hex, amount) {
  const { r, g, b } = hexToRgb(hex);
  const ch = (v) => Math.round(v + (255 - v) * amount).toString(16).padStart(2, "0");
  return `#${ch(r)}${ch(g)}${ch(b)}`;
}

function rgba(hex, alpha) {
  const { r, g, b } = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function stripe(index) {
  return index % 2 === 0 ? 0.55 : 0.26;
}

let paletteCache = null;

function colors() {
  if (paletteCache) return { ...paletteCache };
  const defaults = Object.fromEntries(CATEGORIES.map((item) => [item.id, item.color]));
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    for (const item of CATEGORIES) {
      if (typeof saved[item.id] === "string" && /^#[0-9a-fA-F]{6}$/.test(saved[item.id])) {
        defaults[item.id] = saved[item.id].toLowerCase();
      }
    }
  } catch {
    /* keep defaults */
  }
  paletteCache = defaults;
  return { ...defaults };
}

function saveColors(next) {
  paletteCache = { ...next };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(paletteCache));
  } catch {
    /* the open page still keeps the new colors */
  }
}

function categoryById(id) {
  return CATEGORIES.find((item) => item.id === id);
}

function shell(active) {
  const tabs = [
    ["#day", "day", "День"],
    ["#log", "log", "Подробный лог"],
    ["#settings", "settings", "Настройки"],
  ];
  const links = tabs
    .map(
      ([href, id, label]) =>
        `<a href="${href}" class="${id === active ? "active" : ""}">${label}</a>`,
    )
    .join("");
  return `
    <header class="top">
      <div class="top-left"><a class="back" href="day.html">←</a><h1>Активность</h1></div>
    </header>
    <div class="wrap">
      <nav class="subnav">${links}</nav>
      <div id="view"></div>
    </div>`;
}

const HOUR = 72;

function yOf(minute) {
  return (minute / 60) * HOUR;
}

function article(base, alpha, title, timeLabel, top, height, compact) {
  return `
    <article class="block${compact ? " compact" : ""}" style="top:${top}px;height:${height}px;background:${rgba(base, alpha)};border-left-color:${base};color:#fff">
      <div class="block-title">${esc(title)}</div>
      <div class="block-time">${esc(timeLabel)}</div>
    </article>`;
}

function renderDay() {
  document.body.className = "day-page";
  document.body.innerHTML = shell("day");
  const palette = colors();
  const hours = [];
  for (let hour = 0; hour < 24; hour += 1) {
    const nearNow = Math.abs(hour * 60 - NOW) < 18;
    const label = String(hour).padStart(2, "0") + ":00";
    hours.push(
      `<div class="hour-label${nearNow ? " hidden" : ""}" style="top:${yOf(hour * 60)}px">${label}</div>`,
    );
  }
  const cats = [];
  const subs = [];
  for (const block of BLOCKS) {
    const base = palette[block.category];
    const top = yOf(mins(block.start));
    const height = yOf(mins(block.end)) - top;
    cats.push(
      article(base, 0.4, categoryById(block.category).name, `${block.start}–${block.end}`, top + 1, Math.max(height - 2, 4), height < 42),
    );
    block.items.forEach((item, index) => {
      const itemTop = yOf(mins(item.start));
      const itemHeight = yOf(mins(item.end)) - itemTop;
      subs.push(
        article(base, stripe(index), item.name, `${item.start}–${item.end}`, itemTop + 1, Math.max(itemHeight - 2, 4), itemHeight < 42),
      );
    });
  }
  document.getElementById("view").innerHTML = `
    <section class="cal-card">
      <div class="cal-toolbar">
        <div>
          <div class="cal-kicker">${esc(DAY_KICKER)}</div>
          <h2>${esc(DAY_TITLE)}</h2>
        </div>
        <div class="tz">GMT+5</div>
      </div>
      <div class="cal-scroll" id="cal-scroll">
        <div class="cal-body">
          <div class="hours">
            ${hours.join("")}
            ${NOW_LABEL ? `<div class="now-label" style="top:${yOf(NOW)}px">${esc(NOW_LABEL)}</div>` : ""}
          </div>
          <div class="col">${cats.join("")}</div>
          <div class="col">${subs.join("")}</div>
          ${NOW_LABEL ? `<div class="now" style="top:${yOf(NOW)}px"></div>` : ""}
        </div>
      </div>
    </section>`;
  document.getElementById("cal-scroll").scrollTop = yOf(8 * 60);
}

function renderLog() {
  document.body.className = "";
  document.body.innerHTML = shell("log");
  const palette = colors();
  const groups = new Map();
  for (const row of LOG) {
    const hour = row.time.slice(0, 2) + ":00";
    if (!groups.has(hour)) groups.set(hour, []);
    groups.get(hour).push(row);
  }
  const html = [...groups.entries()]
    .map(([hour, rows]) => {
      const items = rows
        .map((row) => {
          const base = palette[row.category];
          return `
            <li class="log-row">
              <time>${esc(row.time)}</time>
              <span class="log-dot" style="background:${base}"></span>
              <div>
                <div class="log-app" style="color:${mixWhite(base, 0.45)}">${esc(row.app)}</div>
                <div class="log-title">${esc(row.title)}</div>
              </div>
              <div class="log-device">${esc(row.device)}</div>
            </li>`;
        })
        .join("");
      return `<section class="log-hour"><h3>${hour}</h3><ul>${items}</ul></section>`;
    })
    .join("");
  document.getElementById("view").innerHTML = `
    <section class="panel">
      <h2>Подробный лог</h2>
      <p class="hint">Сырые отметки за 27 сентября. На день это не выносится.</p>
      ${html}
    </section>`;
}

function swatches(base, names) {
  const chips = [
    `<span class="chip" style="background:${rgba(base, 0.4)};border-left-color:${base};color:#fff">категория</span>`,
  ];
  names.forEach((name, index) => {
    chips.push(
      `<span class="chip" style="background:${rgba(base, stripe(index))};border-left-color:${base};color:#fff">${esc(name)}</span>`,
    );
  });
  return chips.join("");
}

function renderSettings() {
  document.body.className = "";
  document.body.innerHTML = shell("settings");
  const palette = colors();
  const rows = CATEGORIES.map((item) => {
    const names = BLOCKS.filter((block) => block.category === item.id).flatMap((block) =>
      block.items.map((entry) => entry.name),
    );
    return `
      <label class="color-row">
        <input type="color" value="${palette[item.id]}" data-id="${item.id}" aria-label="Цвет: ${esc(item.name)}">
        <span class="color-name">${esc(item.name)}</span>
        <span class="swatches" data-swatches="${item.id}">${swatches(palette[item.id], names)}</span>
      </label>`;
  }).join("");
  document.getElementById("view").innerHTML = `
    <section class="panel">
      <h2>Цвета категорий</h2>
      <div class="color-list">${rows}</div>
      <button class="btn" type="button" id="reset-colors">Сбросить цвета</button>
    </section>`;
  document.getElementById("view").addEventListener("input", (event) => {
    const input = event.target.closest("input[type=color]");
    if (!input) return;
    const next = colors();
    next[input.dataset.id] = input.value.toLowerCase();
    saveColors(next);
    const names = BLOCKS.filter((block) => block.category === input.dataset.id).flatMap((block) =>
      block.items.map((entry) => entry.name),
    );
    const box = document.querySelector(`[data-swatches="${input.dataset.id}"]`);
    box.innerHTML = swatches(next[input.dataset.id], names);
  });
  document.getElementById("reset-colors").addEventListener("click", () => {
    paletteCache = null;
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      paletteCache = Object.fromEntries(CATEGORIES.map((item) => [item.id, item.color]));
    }
    renderSettings();
  });
}

function useAggregatedDay() {
  const day = window.ACTIVITY_DAY;
  if (!day) return;
  CATEGORIES.splice(0, CATEGORIES.length, ...day.categories);
  BLOCKS.splice(0, BLOCKS.length, ...day.blocks);
  DAY_KICKER = day.weekday || DAY_KICKER;
  DAY_TITLE = day.day_label || DAY_TITLE;
  if (typeof day.now_minutes === "number") {
    NOW = day.now_minutes;
    NOW_LABEL = day.now_label;
  } else {
    NOW_LABEL = null;
  }
  paletteCache = null;
}

function route() {
  useAggregatedDay();
  const page = (location.hash || "#day").slice(1);
  if (page === "log") renderLog();
  else if (page === "settings") renderSettings();
  else renderDay();
}

window.addEventListener("hashchange", route);
window.ActivityMock = { route, renderDay, renderLog, renderSettings };
