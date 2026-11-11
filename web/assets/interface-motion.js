/* Short interface transitions. Data, navigation and disclosure contents stay in their original DOM. */
(() => {
  "use strict";
  const root = document.getElementById("root");
  if (!root || typeof Element.prototype.animate !== "function" || typeof matchMedia !== "function") return;

  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const entrySelector = ".main > .head, .main > .hero, .main > .panel, .main > .stats > .stat, .main > .cards > .card";
  const barSelector = ".track .fill, .funnel .fb";
  const valueSelector = ".stat .v, .card-score .p, .hero .big";
  const quietSelector = "#cJobsList, #gptMessages, #gptDocuments, #gptUploadStatus";
  const seen = new WeakSet(), values = new WeakMap();
  const pending = new Map(), active = new Set(), disclosures = new Map();
  const scopes = new Set(), changedValues = new Set();
  let frame = 0, printing = false, previousTab = null;
  const disabled = () => reduced.matches || document.hidden || printing;

  function play(node, keyframes, options, finish = () => {}) {
    if (disabled() || !node.isConnected) { finish(); return null; }
    const animation = node.animate(keyframes, {duration: 220, easing: "cubic-bezier(.2,.7,.2,1)", ...options});
    active.add(animation);
    const settle = () => { active.delete(animation); finish(); };
    animation.onfinish = settle;
    animation.oncancel = settle;
    return animation;
  }

  function reveal(node, index) {
    const kind = pending.get(node);
    if (!kind) return;
    pending.delete(node);
    observer?.unobserve(node);
    if (kind === "bar") {
      play(node, [{transform: "scaleX(.25)", transformOrigin: "left"},
                  {transform: "scaleX(1)", transformOrigin: "left"}], {duration: 340, delay: 40});
    } else {
      play(node, [{opacity: .35, transform: "translateY(6px)"}, {opacity: 1, transform: "translateY(0)"}],
        {delay: Math.min(index, 5) * 18});
    }
  }

  const observer = typeof IntersectionObserver === "function" ? new IntersectionObserver((entries) => {
    entries.filter(entry => entry.isIntersecting).forEach((entry, index) => reveal(entry.target, index));
  }, {threshold: .05}) : null;

  function watch(node, kind) {
    if (seen.has(node) || disabled()) return;
    seen.add(node);
    pending.set(node, kind);
    if (observer) observer.observe(node);
    else reveal(node, 0);
  }

  function collect(scope, selector) {
    return [...(scope.matches(selector) ? [scope] : []), ...scope.querySelectorAll(selector)];
  }

  function updateTab() {
    const tabs = root.querySelector(".tabs");
    const selected = tabs?.querySelector(":scope > a.on, :scope > .tab-menu.on > summary");
    if (!selected) { previousTab = null; return; }
    const rect = selected.getBoundingClientRect(), container = tabs.getBoundingClientRect();
    if (!rect.width) return;
    let indicator = tabs.querySelector(".motion-tab-indicator");
    if (!indicator) {
      indicator = document.createElement("span");
      indicator.className = "motion-tab-indicator";
      indicator.setAttribute("aria-hidden", "true");
      tabs.appendChild(indicator);
      tabs.dataset.motionTabs = "";
    }
    indicator.style.left = `${rect.left - container.left}px`;
    const bottom = rect.top + rect.height;
    indicator.style.top = `${bottom - container.top - 3}px`;
    indicator.style.width = `${rect.width}px`;
    if (previousTab && (previousTab.left !== rect.left || previousTab.width !== rect.width || previousTab.bottom !== bottom)) {
      play(indicator, [{transform: `translate(${previousTab.left - rect.left}px,${previousTab.bottom - bottom}px)`, width: `${previousTab.width}px`},
                       {transform: "translate(0,0)", width: `${rect.width}px`}], {duration: 210});
    }
    previousTab = {left: rect.left, width: rect.width, bottom};
  }

  function flush() {
    frame = 0;
    if (document.hidden) return;
    updateTab();
    for (const node of pending.keys()) {
      if (!node.isConnected) { observer?.unobserve(node); pending.delete(node); }
    }
    for (const scope of scopes) {
      if (!scope.isConnected) continue;
      collect(scope, entrySelector).forEach(node => watch(node, "entry"));
      collect(scope, barSelector).forEach(node => watch(node, "bar"));
      collect(scope, valueSelector).forEach(node => {
        if (!values.has(node)) values.set(node, node.textContent);
      });
      collect(scope, ".empty").forEach(node => {
        const loading = node.textContent.trim().startsWith("Загрузка");
        node.classList.toggle("motion-loading", loading);
        if (loading) node.setAttribute("aria-busy", "true");
        else node.removeAttribute("aria-busy");
      });
    }
    scopes.clear();
    for (const node of changedValues) {
      const text = node.textContent;
      if (values.has(node) && values.get(node) !== text) {
        play(node, [{opacity: .55, transform: "translateY(2px)"}, {opacity: 1, transform: "translateY(0)"}], {duration: 180});
      }
      values.set(node, text);
    }
    changedValues.clear();
  }

  function schedule() {
    if (!frame && !document.hidden) frame = requestAnimationFrame(flush);
  }

  new MutationObserver((records) => {
    if (document.hidden) return;
    let relevant = false;
    for (const record of records) {
      const target = record.type === "characterData" ? record.target.parentElement : record.target;
      if (!target || target.closest(quietSelector)) continue;
      relevant = true;
      const value = target.closest(valueSelector);
      if (value) changedValues.add(value);
      const loading = target.closest(".empty");
      if (loading) scopes.add(loading);
      for (const node of record.addedNodes || []) {
        if (node.nodeType === 1) scopes.add(node);
      }
    }
    if (relevant) schedule();
  }).observe(root, {childList: true, subtree: true, characterData: true});

  document.addEventListener("click", (event) => {
    if (disabled() || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const summary = event.target.closest?.("summary");
    const details = summary?.closest("details");
    if (!details || !root.contains(details) || summary.parentElement !== details ||
        !details.matches("details.more, details.kpi-audit") || details.hasAttribute("data-motion-native") ||
        event.target.closest("a,button,input,select,textarea,[contenteditable=true]")) return;
    event.preventDefault();
    const old = disclosures.get(details), opening = old ? !old.opening : !details.open;
    const start = details.getBoundingClientRect().height;
    const overflow = old ? old.overflow : details.style.overflow;
    disclosures.delete(details);
    old?.animation?.cancel();
    details.open = true;
    const style = getComputedStyle(details);
    const extra = ["paddingTop", "paddingBottom", "borderTopWidth", "borderBottomWidth"]
      .reduce((sum, key) => sum + (parseFloat(style[key]) || 0), 0);
    const end = opening ? details.scrollHeight + (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0)
                        : summary.getBoundingClientRect().height + extra;
    const record = {opening, overflow, animation: null};
    disclosures.set(details, record);
    details.style.overflow = "hidden";
    record.animation = play(details, [{height: `${start}px`}, {height: `${end}px`}], {duration: 180}, () => {
      if (disclosures.get(details) !== record) return;
      disclosures.delete(details);
      details.style.overflow = overflow;
      if (details.isConnected) details.open = opening;
    });
  });

  function stop() {
    for (const animation of [...active]) animation.cancel();
    for (const node of pending.keys()) observer?.unobserve(node);
    pending.clear();
    cancelAnimationFrame(frame);
    frame = 0;
    scopes.clear();
    changedValues.clear();
  }

  reduced.addEventListener("change", () => {
    stop();
    scopes.add(root);
    schedule();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else { previousTab = null; scopes.add(root); schedule(); }
  });
  window.addEventListener("beforeprint", () => { printing = true; stop(); });
  window.addEventListener("afterprint", () => { printing = false; previousTab = null; scopes.add(root); schedule(); });
  window.addEventListener("resize", () => { previousTab = null; schedule(); }, {passive: true});
  document.fonts?.ready.then(schedule);
  document.fonts?.addEventListener("loadingdone", schedule);
  scopes.add(root);
  schedule();
})();
