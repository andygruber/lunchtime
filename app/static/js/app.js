// Progressive enhancement only — every action also works without JavaScript.
document.documentElement.classList.add("js");

document.addEventListener("DOMContentLoaded", () => {
  // date picker: navigate on change
  document.querySelectorAll("[data-autosubmit]").forEach((input) => {
    input.addEventListener("change", () => input.form.submit());
  });
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // previous/next day: swipe on touch devices, arrow keys on desktop
  const swipe = document.querySelector("[data-swipe]");
  if (swipe) {
    const go = (dir) => {
      const target = swipe.dataset[dir];
      if (target) window.location.href = target;
    };
    let x0 = null, y0 = null;
    document.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1 || e.target.closest("[data-hscroll]")) return;
      x0 = e.touches[0].clientX;
      y0 = e.touches[0].clientY;
    }, { passive: true });
    document.addEventListener("touchend", (e) => {
      if (x0 === null) return;
      const dx = e.changedTouches[0].clientX - x0;
      const dy = e.changedTouches[0].clientY - y0;
      x0 = y0 = null;
      if (Math.abs(dx) > 70 && Math.abs(dy) < 50) go(dx > 0 ? "prev" : "next");
    }, { passive: true });
    document.addEventListener("keydown", (e) => {
      if (e.altKey || e.ctrlKey || e.metaKey || /input|textarea|select/i.test(e.target.tagName)) return;
      if (e.key === "ArrowLeft") go("prev");
      if (e.key === "ArrowRight") go("next");
    });
  }

  // restaurant search: filter the list while typing
  document.querySelectorAll("input[data-filter]").forEach((input) => {
    const list = document.querySelector(input.dataset.filter);
    const empty = list && list.parentElement.querySelector("[data-empty]");
    if (!list) return;
    const apply = () => {
      const terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      let visible = 0;
      list.querySelectorAll("[data-search]").forEach((row) => {
        const show = terms.every((t) => row.dataset.search.includes(t));
        row.hidden = !show;
        if (show) visible++;
      });
      if (empty) empty.hidden = visible > 0;
    };
    input.addEventListener("input", apply);
    apply();
  });

  // narrow screens scroll the toolbar sideways: keep the active settings in view
  document.querySelectorAll("[data-hscroll]").forEach((bar) => {
    const pressed = bar.querySelectorAll("[aria-pressed='true']");
    if (pressed.length && bar.scrollWidth > bar.clientWidth) {
      bar.scrollLeft = Math.max(0, pressed[pressed.length - 1].offsetLeft + pressed[pressed.length - 1].offsetWidth - bar.clientWidth + 16);
    }
  });

  // color scheme: switch at once and store it in the background (the form reloads the page without JS)
  document.querySelectorAll("form[data-theme-switch]").forEach((form) => {
    const button = form.querySelector("button");
    const meta = document.querySelector('meta[name="color-scheme"]');
    const order = ["auto", "light", "dark"];
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const theme = button.value;
      if (theme === "auto") delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = theme;
      if (meta) meta.content = theme === "auto" ? "light dark" : theme;
      button.dataset.current = theme;
      button.value = order[(order.indexOf(theme) + 1) % order.length];
      button.title = button.dataset["label" + theme[0].toUpperCase() + theme.slice(1)];
      button.setAttribute("aria-label", button.title);
      send(form, { name: "theme", value: theme });
    });
  });

  // favourite star: toggle in place instead of reloading (and losing the scroll position)
  document.querySelectorAll("form[data-fav-toggle]").forEach((form) => {
    const button = form.querySelector("button");
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const on = button.getAttribute("aria-pressed") !== "true";
      button.setAttribute("aria-pressed", String(on));
      button.setAttribute("aria-label", on ? button.dataset.labelOn : button.dataset.labelOff);
      button.title = button.getAttribute("aria-label").split(":")[0];
      const card = form.closest("[data-fav]");
      if (card) card.dataset.fav = on ? "1" : "0";
      send(form);
    });
  });

  // "nearby": sort by distance; the position stays in the browser
  document.querySelectorAll("[data-sortable][data-sort='distance']").forEach((list) => {
    const status = document.querySelector("[data-geo-status]");
    const show = (text) => {
      if (!status) return;
      status.textContent = text || "";
      status.hidden = !text;
    };
    show(status && status.dataset.locating);
    currentPosition().then((here) => {
      show("");
      const lang = document.documentElement.lang || undefined;
      const items = [...list.children].map((el, index) => {
        let km = Infinity;
        if (el.dataset.lat) {
          km = distanceKm(here, { lat: +el.dataset.lat, lon: +el.dataset.lon });
          const label = el.querySelector("[data-dist]");
          if (label) {
            const value = km.toLocaleString(lang, { maximumFractionDigits: km < 10 ? 1 : 0 });
            label.textContent = km < 1 ? Math.max(100, Math.round(km * 10) * 100) + " m" : value + " km";
            label.title = list.dataset.distanceLabel.replace("{km}", value);
            label.hidden = false;
          }
        }
        const group = list.dataset.favFirst === "1" && el.dataset.fav !== "1" ? 1 : 0;
        return { el, key: [group, km, index] };
      });
      items.sort((a, b) => a.key[0] - b.key[0] || a.key[1] - b.key[1] || a.key[2] - b.key[2]);
      items.forEach(({ el }) => list.appendChild(el));
    }, () => show(status && status.dataset.failed));
  });
});

function send(form, submitter) {
  const body = new URLSearchParams(new FormData(form));
  if (submitter) body.set(submitter.name, submitter.value);
  return fetch(form.action, { method: "POST", body, headers: { "x-requested-with": "fetch" } })
    .then((r) => { if (!r.ok) throw new Error(r.status); })
    .catch(() => form.submit());
}

const POSITION_KEY = "lunchtime.position";

function currentPosition() {
  try {
    const cached = JSON.parse(sessionStorage.getItem(POSITION_KEY));
    if (cached && Date.now() - cached.time < 30 * 60 * 1000) return Promise.resolve(cached);
  } catch (e) { /* storage blocked: ask the browser again */ }
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("unsupported"));
    navigator.geolocation.getCurrentPosition((p) => {
      // rounded to about 100 m: enough to rank restaurants
      const here = { lat: +p.coords.latitude.toFixed(3), lon: +p.coords.longitude.toFixed(3), time: Date.now() };
      try { sessionStorage.setItem(POSITION_KEY, JSON.stringify(here)); } catch (e) { /* not cached */ }
      resolve(here);
    }, reject, { maximumAge: 10 * 60 * 1000, timeout: 15000 });
  });
}

function distanceKm(a, b) {
  const rad = (d) => (d * Math.PI) / 180;
  const h = Math.sin(rad(b.lat - a.lat) / 2) ** 2
    + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(rad(b.lon - a.lon) / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(h));
}
