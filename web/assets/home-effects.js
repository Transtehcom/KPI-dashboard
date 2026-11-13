/* Assemble the existing two-colour logo from particles on hover or keyboard focus. */
(() => {
  "use strict";
  if (!window.Path2D || !window.matchMedia) return;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let active = null;

  function stop() {
    if (!active) return;
    cancelAnimationFrame(active.frame);
    active.brand.classList.remove("is-assembling");
    active.canvas.remove();
    active = null;
  }

  function assemble(brand) {
    stop();
    if (reduced.matches || document.hidden) return;
    const svg = brand.querySelector(".brand-logo"), wrap = brand.querySelector(".brand-logo-wrap");
    if (!svg || !wrap) return;
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const sample = document.createElement("canvas");
    sample.width = Math.ceil(rect.width); sample.height = Math.ceil(rect.height);
    const sampleContext = sample.getContext("2d", {willReadFrequently: true});
    if (!sampleContext) return;
    const points = [];
    [...svg.querySelectorAll("path")].forEach((path, index) => {
      sampleContext.clearRect(0, 0, sample.width, sample.height);
      sampleContext.save();
      sampleContext.scale(rect.width / 1306, rect.height / 875);
      sampleContext.fillStyle = path.getAttribute("fill");
      sampleContext.fill(new Path2D(path.getAttribute("d")));
      sampleContext.restore();
      const pixels = sampleContext.getImageData(0, 0, sample.width, sample.height).data;
      for (let y = 0; y < sample.height; y++) {
        for (let x = 0; x < sample.width; x++) {
          if (pixels[(y * sample.width + x) * 4 + 3] < 100) continue;
          const direction = index === 0 ? 1 : -1;
          points.push({x: x + .5, y: y + .5, color: path.getAttribute("fill"),
            fromX: x - direction * (14 + Math.random() * 14), fromY: y + (Math.random() - .5) * 38,
            delay: (index === 0 ? x / rect.width : 1 - x / rect.width) * 430});
        }
      }
    });
    if (!points.length) return;
    const canvas = document.createElement("canvas"), padding = 26;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.className = "brand-dust";
    canvas.setAttribute("aria-hidden", "true");
    canvas.width = Math.ceil((rect.width + padding * 2) * ratio);
    canvas.height = Math.ceil((rect.height + padding * 2) * ratio);
    Object.assign(canvas.style, {left: `-${padding}px`, top: `-${padding}px`,
      width: `${rect.width + padding * 2}px`, height: `${rect.height + padding * 2}px`});
    const context = canvas.getContext("2d");
    if (!context) return;
    context.scale(ratio, ratio);
    wrap.appendChild(canvas);
    brand.classList.add("is-assembling");
    const animation = {brand, canvas, frame: 0, started: performance.now()};
    active = animation;
    function draw(time) {
      if (active !== animation) return;
      if (!brand.isConnected || document.hidden || reduced.matches || time - animation.started >= 1100) { stop(); return; }
      context.clearRect(0, 0, canvas.width / ratio, canvas.height / ratio);
      for (const point of points) {
        const progress = Math.max(0, Math.min(1, (time - animation.started - point.delay) / 620));
        const eased = 1 - (1 - progress) ** 3;
        context.globalAlpha = Math.min(1, progress * 5);
        context.fillStyle = point.color;
        context.fillRect(padding + point.fromX + (point.x - point.fromX) * eased,
          padding + point.fromY + (point.y - point.fromY) * eased, 1, 1);
      }
      animation.frame = requestAnimationFrame(draw);
    }
    animation.frame = requestAnimationFrame(draw);
  }

  document.addEventListener("pointerover", event => {
    const brand = event.target.closest?.(".brand-home");
    if (brand && !brand.contains(event.relatedTarget)) assemble(brand);
  });
  document.addEventListener("focusin", event => {
    const brand = event.target.closest?.(".brand-home");
    if (brand) assemble(brand);
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });
  reduced.addEventListener("change", () => { if (reduced.matches) stop(); });
})();
