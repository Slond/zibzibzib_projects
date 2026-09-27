function mins(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

function span(start, end) {
  return Math.max(0, mins(end) - mins(start));
}

function fmt(total) {
  const h = Math.floor(total / 60);
  const m = total % 60;
  if (h === 0) return `${m} мин`;
  if (m === 0) return `${h} ч`;
  return `${h} ч ${m} мин`;
}

function esc(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

function percents(rows, total) {
  const raw = rows.map((row) => (total ? (row.minutes / total) * 100 : 0));
  const rounded = raw.map((value) => Math.round(value));
  const drift = 100 - rounded.reduce((sum, value) => sum + value, 0);
  if (rows.length && drift !== 0) {
    let index = 0;
    raw.forEach((value, i) => {
      if (value > raw[index]) index = i;
    });
    rounded[index] += drift;
  }
  return rounded.map((value, i) => (value === 0 && rows[i].minutes > 0 ? "<1%" : `${value}%`));
}

function groups(day) {
  const byId = new Map(day.categories.map((item) => [item.id, { ...item, minutes: 0, intervals: [] }]));
  for (const block of day.blocks) {
    const group = byId.get(block.category);
    if (!group) continue;
    for (const item of block.items) {
      const minutes = span(item.start, item.end);
      group.minutes += minutes;
      group.intervals.push({ name: item.name, start: item.start, end: item.end, minutes });
    }
  }
  return [...byId.values()].filter((group) => group.minutes > 0).sort((a, b) => b.minutes - a.minutes);
}

function render() {
  const day = window.ACTIVITY_DAY;
  const rows = groups(day);
  const total = rows.reduce((sum, row) => sum + row.minutes, 0);
  const shares = percents(rows, total);
  let cursor = 0;
  const slices = rows.map((row, index) => {
    const start = cursor;
    cursor += total ? (row.minutes / total) * 100 : 0;
    return `${row.color} ${start}% ${cursor}%`;
  });
  const list = rows
    .map((row, index) => {
      const intervals = row.intervals
        .map(
          (item) =>
            `<li><span>${esc(item.name)}</span><time>${esc(item.start)}–${esc(item.end)} · ${esc(fmt(item.minutes))}</time></li>`,
        )
        .join("");
      return `
        <button class="spend-row" type="button" aria-expanded="false">
          <div class="spend-head">
            <span class="spend-dot" style="background:${row.color}"></span>
            <span class="spend-name">${esc(row.name)}</span>
            <span class="spend-time">${esc(fmt(row.minutes))}</span>
            <span class="spend-pct">${esc(shares[index])}</span>
            <span class="spend-chevron">›</span>
          </div>
          <div class="spend-track"><div class="spend-fill" style="width:${total ? (row.minutes / total) * 100 : 0}%;background:${row.color}"></div></div>
          <ul class="intervals">${intervals}</ul>
        </button>`;
    })
    .join("");
  document.body.innerHTML = `
    <header class="top">
      <div class="top-left"><h1>Активность</h1></div>
    </header>
    <div class="wrap">
      <section class="panel spend">
        <div class="cal-toolbar">
          <div>
            <div class="cal-kicker">${esc(day.weekday)}</div>
            <h2>${esc(day.day_label)}</h2>
          </div>
          <div class="tz">GMT+5</div>
        </div>
        <div style="padding: 0 18px 18px">
          <div class="spend-total">
            <strong>${esc(fmt(total))}</strong>
            <span>за экраном</span>
          </div>
          <div class="donut-wrap">
            <div class="donut" style="background:conic-gradient(${slices.join(",")})"></div>
          </div>
          <div class="spend-list">${list}</div>
        </div>
      </section>
    </div>`;
  document.querySelector(".spend-list").addEventListener("click", (event) => {
    const row = event.target.closest(".spend-row");
    if (!row) return;
    const open = row.classList.toggle("open");
    row.setAttribute("aria-expanded", open ? "true" : "false");
  });
}

render();
