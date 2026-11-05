/* Decorative TTK outline field. Canvas layers never intercept interface input. */
(() => {
  "use strict";
  if (!window.Path2D || !window.matchMedia) return;
  const root = document.getElementById("root");
  if (!root) return;
  const createLayer = (name) => {
    const canvas = document.createElement("canvas");
    canvas.className = `brand-field brand-field--${name}`;
    canvas.setAttribute("aria-hidden", "true");
    document.body.appendChild(canvas);
    return { canvas, ctx: canvas.getContext("2d") };
  };
  const back = createLayer("back"), front = createLayer("front");
  if (!back.ctx || !front.ctx) {
    back.canvas.remove(); front.canvas.remove(); return;
  }
  // The exact paths from the approved original mark; fills are intentionally absent.
  const blue = new Path2D("M1118,464 L1006,623 L1114,776 L1129,794 L1153,818 L1183,840 L1221,858 L1266,870 L1305,874 L1305,690 L1289,688 L1278,683 L1269,676 L1255,660Z M0,0 L1,24 L7,57 L18,90 L33,120 L53,149 L82,179 L99,193 L124,209 L149,221 L178,231 L226,238 L325,240 L707,239 L727,242 L740,250 L756,266 L893,460 L912,436 L1004,304 L1004,301 L909,166 L880,130 L857,108 L835,92 L812,80 L773,65 L727,57 L244,57 L222,53 L206,44 L191,27 L185,13 L184,0Z");
  const teal = new Path2D("M1304,57 L1285,57 L1256,61 L1232,67 L1210,75 L1189,85 L1166,99 L1142,118 L1126,134 L1109,155 L1008,298 L995,320 L894,461 L847,531 L769,641 L739,678 L731,684 L713,691 L659,692 L639,686 L626,679 L614,667 L605,646 L603,632 L603,240 L420,241 L420,641 L422,666 L429,701 L438,726 L456,760 L483,796 L508,819 L552,848 L577,859 L599,866 L648,873 L726,873 L769,865 L797,855 L816,846 L842,830 L868,809 L892,783 L1255,266 L1267,253 L1278,245 L1291,240 L1304,239Z");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const colorScheme = matchMedia("(prefers-color-scheme: dark)");
  const pointer = { x: -1000, y: -1000, visible: false };
  const cursor = { strength: 0 };
  const hoverRadius = 22, trailRadius = 16, trailDuration = 1100;
  const waveDuration = 1250, waveRadius = 155;
  // Cache the dense neutral grid; only the small active area needs individual redraws.
  const pattern = document.createElement("canvas"), patternCtx = pattern.getContext("2d");
  let width = 0, height = 0, ratio = 1, marks = [], surfaces = [], waves = [], trail = [];
  let frame = 0, activeUntil = 0, lastTime = 0, surfacesDirty = true, patternDirty = true, dark = false;

  function readTheme() {
    const theme = document.documentElement.dataset.theme;
    dark = theme === "dark" || (theme !== "light" && colorScheme.matches);
    patternDirty = true;
  }
  function resize() {
    width = window.innerWidth; height = window.innerHeight;
    ratio = Math.min(window.devicePixelRatio || 1, 1.5, Math.sqrt(4000000 / Math.max(1, width * height)));
    for (const { canvas, ctx } of [back, front]) {
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    }
    pattern.width = Math.round(width * ratio); pattern.height = Math.round(height * ratio);
    patternCtx.setTransform(ratio, 0, 0, ratio, 0, 0);
    patternDirty = true;
    marks = [];
    const gapX = 32, gapY = 25;
    for (let row = -1; row * gapY < height + gapY; row++) {
      for (let col = -1; col * gapX < width + gapX; col++) {
        marks.push({ x: col * gapX + (row % 2 ? gapX / 2 : 0) + 16, y: row * gapY + 13,
                     size: 18 + ((row + col + 12) % 3) });
      }
    }
    surfacesDirty = true; wake();
  }
  function readSurfaces() {
    // Only large card surfaces get the quieter upper layer; controls and popovers stay clear.
    surfaces = [...root.querySelectorAll(".panel")].map(el => el.getBoundingClientRect())
      .filter(r => r.width > 100 && r.height > 45 && r.bottom > 0 && r.top < height);
    surfacesDirty = false;
  }
  function wake(duration = 900) {
    activeUntil = performance.now() + duration;
    if (!frame && !document.hidden) frame = requestAnimationFrame(draw);
  }
  function mark(ctx, item, offsetX, offsetY, scale, alpha, glow, color = 1) {
    ctx.save();
    ctx.translate(item.x + offsetX, item.y + offsetY);
    const size = item.size * scale;
    ctx.scale(size / 1306, size / 1306);
    ctx.translate(-653, -437.5);
    ctx.globalAlpha = alpha;
    ctx.lineWidth = 0.78 * 1306 / size;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.shadowBlur = glow;
    const gray = dark ? [145, 150, 164] : [139, 145, 156];
    const blend = rgb => `rgb(${rgb.map((v, i) => Math.round(gray[i] + (v - gray[i]) * color)).join(",")})`;
    ctx.strokeStyle = blend([58, 65, 133]); ctx.shadowColor = ctx.strokeStyle; ctx.stroke(blue);
    ctx.strokeStyle = blend([75, 187, 184]); ctx.shadowColor = ctx.strokeStyle; ctx.stroke(teal);
    ctx.restore();
  }
  function draw(now) {
    frame = 0;
    if (document.hidden) return;
    const elapsed = Math.min(50, now - (lastTime || now)); lastTime = now;
    const approach = reduced.matches ? 1 : 1 - Math.exp(-elapsed / 75);
    cursor.strength += ((pointer.visible ? 1 : 0) - cursor.strength) * approach;
    if (surfacesDirty) readSurfaces();
    if (patternDirty) {
      patternCtx.clearRect(0, 0, width, height);
      for (const item of marks) mark(patternCtx, item, 0, 0, 1, 1, 0, 0);
      patternDirty = false;
    }
    waves = reduced.matches ? [] : waves.filter(w => now - w.at < waveDuration);
    trail = reduced.matches ? [] : trail.filter(p => now - p.at < trailDuration);
    for (const { ctx } of [back, front]) ctx.clearRect(0, 0, width, height);
    const modal = !!root.querySelector(".drawer");
    front.ctx.save();
    front.ctx.beginPath();
    if (!modal) surfaces.forEach(r => front.ctx.rect(r.left + 2, r.top + 2, r.width - 4, r.height - 4));
    front.ctx.clip();
    const backIdle = dark ? 0.19 : 0.16, frontIdle = dark ? 0.055 : 0.045;
    for (const [layer, alpha] of [[back, backIdle], [front, frontIdle]]) {
      layer.ctx.save(); layer.ctx.globalAlpha = alpha;
      layer.ctx.drawImage(pattern, 0, 0, width, height); layer.ctx.restore();
    }

    for (const item of marks) {
      const distance = Math.hypot(item.x - pointer.x, item.y - pointer.y);
      let hover = Math.pow(Math.max(0, 1 - distance / hoverRadius), 0.55) * cursor.strength;
      // Distance to the actual mouse path produces a narrow continuous snake, even on fast movement.
      for (let i = 1; i < trail.length; i++) {
        const a = trail[i - 1], b = trail[i];
        if (item.x < Math.min(a.x, b.x) - trailRadius || item.x > Math.max(a.x, b.x) + trailRadius ||
            item.y < Math.min(a.y, b.y) - trailRadius || item.y > Math.max(a.y, b.y) + trailRadius) continue;
        const dx = b.x - a.x, dy = b.y - a.y;
        const t = Math.max(0, Math.min(1, ((item.x - a.x) * dx + (item.y - a.y) * dy) / (dx * dx + dy * dy || 1)));
        const age = Math.max(0, now - (a.at + (b.at - a.at) * t));
        const freshness = Math.pow(Math.max(0, 1 - age / trailDuration), 1.4);
        const radius = trailRadius * (0.55 + 0.45 * freshness);
        const pathDistance = Math.hypot(item.x - (a.x + dx * t), item.y - (a.y + dy * t));
        const energy = Math.pow(Math.max(0, 1 - pathDistance / radius), 0.55) * freshness;
        hover = Math.max(hover, energy);
      }
      let offsetX = 0, offsetY = 0, waveLight = 0;
      for (const wave of waves) {
        const progress = (now - wave.at) / waveDuration, radius = progress * waveRadius;
        const dx = item.x - wave.x, dy = item.y - wave.y, dist = Math.hypot(dx, dy);
        if (dist > waveRadius || Math.abs(dist - radius) > 35) continue;
        const band = (dist - radius) / 13;
        const envelope = Math.exp(-band * band / 2) * Math.pow(1 - progress, 1.25);
        const displacement = Math.sin(band * 1.7) * envelope * 3;
        offsetX += dx / (dist || 1) * displacement;
        offsetY += dy / (dist || 1) * displacement;
        waveLight += envelope;
      }
      const light = Math.min(1, hover * 0.8 + waveLight * 1.8);
      // Replace neutral outlines locally with smoothly blended brand colors.
      if (light < 0.008) continue;
      const color = Math.min(1, light * 1.7);
      const scale = reduced.matches ? 1 : 1 + hover * 0.04 + Math.min(waveLight, 1) * 0.04;
      const markHeight = item.size * 875 / 1306;
      for (const { ctx } of [back, front]) {
        ctx.clearRect(item.x - item.size / 2 - 4, item.y - markHeight / 2 - 4, item.size + 8, markHeight + 8);
      }
      mark(back.ctx, item, offsetX, offsetY, scale, backIdle + light * (dark ? 0.61 : 0.56), light * 3, color);
      // Low opacity on cards keeps labels, tables and charts readable during the effect.
      mark(front.ctx, item, offsetX * 0.5, offsetY * 0.5, scale, frontIdle + light * (dark ? 0.135 : 0.105), 0, color);
    }
    for (const wave of waves) {
      const progress = (now - wave.at) / waveDuration, radius = progress * waveRadius;
      for (const [layer, alpha] of [[back, 0.18], [front, 0.035]]) {
        const ctx = layer.ctx;
        ctx.save();
        ctx.beginPath(); ctx.arc(wave.x, wave.y, radius, 0, Math.PI * 2);
        ctx.strokeStyle = "#4BBBB8"; ctx.lineWidth = 1.2;
        ctx.globalAlpha = Math.pow(1 - progress, 1.25) * alpha; ctx.stroke();
        ctx.restore();
      }
    }
    front.ctx.restore();
    if (!reduced.matches && (now < activeUntil || waves.length || trail.length)) frame = requestAnimationFrame(draw);
  }

  window.addEventListener("pointermove", e => {
    if (e.pointerType === "touch") return;
    const now = performance.now();
    if (!reduced.matches) {
      const last = trail[trail.length - 1];
      if (!last || now - last.at > 120 || Math.hypot(e.clientX - last.x, e.clientY - last.y) >= 4) {
        // A fresh entry starts a new path rather than connecting across the browser boundary.
        if (!pointer.visible) trail = [];
        trail.push({ x: e.clientX, y: e.clientY, at: now });
        if (trail.length > 160) trail.shift();
      }
    }
    pointer.x = e.clientX; pointer.y = e.clientY; pointer.visible = true; wake(trailDuration + 80);
  }, { passive: true });
  document.documentElement.addEventListener("pointerleave", () => { pointer.visible = false; wake(); }, { passive: true });
  window.addEventListener("blur", () => { pointer.visible = false; wake(); });
  window.addEventListener("pointerdown", e => {
    if (e.button !== 0 || reduced.matches) return;
    waves.push({ x: e.clientX, y: e.clientY, at: performance.now() });
    if (waves.length > 5) waves.shift();
    wake(waveDuration + 80);
  }, { passive: true });
  window.addEventListener("resize", resize, { passive: true });
  window.addEventListener("scroll", () => { surfacesDirty = true; wake(150); }, { passive: true, capture: true });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { cancelAnimationFrame(frame); frame = 0; lastTime = 0; trail = []; }
    else { surfacesDirty = true; wake(); }
  });
  new MutationObserver(() => { surfacesDirty = true; wake(150); }).observe(root, { childList: true, subtree: true });
  new MutationObserver(() => { readTheme(); wake(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  colorScheme.addEventListener("change", () => { readTheme(); wake(); });
  reduced.addEventListener("change", () => { waves = []; trail = []; wake(); });
  readTheme(); resize();
})();
