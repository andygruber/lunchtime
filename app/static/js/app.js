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
      if (e.touches.length !== 1) return;
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
});
