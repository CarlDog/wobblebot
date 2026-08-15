// Nav-icon alert dots. Two independent pollers driving the same
// visual treatment (.has-alert on the nav-icon -> red dot via CSS).
//
// Bell: shows the dot when the server reports unread notifications.
// Server-side since P3 slice 19 — the old localStorage "last seen"
// comparison was per-browser, so clearing the dot on the desktop left
// the phone dotted, and merely opening /notifications counted as
// reading everything. The dot now clears when the operator actually
// acknowledges a row (or Mark all read).
//
// Health: hits /health/overall.json on the same cadence; shows the
// dot when overall status is not green (yellow or red). The page
// itself shows the granular traffic-light.
//
// Extracted from an inline <script> in layout.html (v1.1 CSP fix) —
// script-src is 'self' only. Loaded after the nav/toast-stack markup
// in the document, same as the original inline placement, so the
// element lookups below still find their targets.
window.updateBellBadge = async function () {
  try {
    const r = await fetch('/notifications/latest-timestamp');
    if (!r.ok) return;
    const { unread } = await r.json();
    const bell = document.getElementById('nav-bell');
    if (!bell) return;
    if (unread > 0) {
      bell.classList.add('has-alert');
    } else {
      bell.classList.remove('has-alert');
    }
  } catch (e) {
    // Silent — bell badge isn't critical UX.
  }
};
window.updateBellBadge();
setInterval(window.updateBellBadge, 30000);

window.updateHealthBadge = async function () {
  try {
    const r = await fetch('/health/overall.json');
    if (!r.ok) return;
    const { overall } = await r.json();
    const health = document.getElementById('nav-health');
    if (!health) return;
    // Tiered dot since 2026-05-23: yellow=degraded, red=error,
    // no dot when green. The status-card traffic light was
    // removed in the same change — this is now the only health
    // indicator in the chrome.
    health.classList.remove('has-alert-yellow', 'has-alert-red');
    if (overall === 'yellow') {
      health.classList.add('has-alert-yellow');
    } else if (overall === 'red') {
      health.classList.add('has-alert-red');
    }
  } catch (e) {
    // Silent — health dot isn't critical UX; the /health page is
    // the source of truth.
  }
};
window.updateHealthBadge();
setInterval(window.updateHealthBadge, 30000);

// Fill toasts — poll recent fills, show a bottom-right popup for each
// fill newer than the localStorage watermark (so returning to the page
// replays fills since you were last active), then advance the mark. The
// very first load (no watermark) just sets the mark without a flood.
(function () {
  function showFillToast(f) {
    const stack = document.getElementById('toast-stack');
    if (!stack) return;
    const el = document.createElement('div');
    el.className = 'toast toast-' + (f.side === 'buy' ? 'buy' : 'sell');
    // textContent (not innerHTML) — XSS-safe even though the data is ours.
    // price arrives pre-formatted with its $ (house fmt_usd) — do not
    // prepend another one here.
    el.textContent = f.side.toUpperCase() + ' ' + f.symbol + '  ' + f.amount + ' @ ' + f.price;
    stack.appendChild(el);
    requestAnimationFrame(function () { el.classList.add('toast-show'); });
    setTimeout(function () {
      el.classList.remove('toast-show');
      setTimeout(function () { el.remove(); }, 300);
    }, 6000);
  }
  window.pollFills = async function () {
    try {
      const r = await fetch('/status/recent-fills.json');
      if (!r.ok) return;
      const data = await r.json();
      const fills = data.fills || [];
      if (!fills.length) return;
      const lastSeen = localStorage.getItem('wobblebot_last_seen_fill') || '';
      const fresh = [];
      for (const f of fills) {            // newest-first from the server
        if (f.id === lastSeen) break;
        fresh.push(f);
      }
      localStorage.setItem('wobblebot_last_seen_fill', fills[0].id);
      if (!lastSeen) return;              // first ever load — no flood
      fresh.slice(0, 5).reverse().forEach(showFillToast);  // cap + oldest-first
    } catch (e) { /* silent — toasts aren't critical UX */ }
  };
  window.pollFills();
  setInterval(window.pollFills, 15000);
})();

// User menu dropdown — toggle on trigger click; close on outside
// click and on Escape. Keeps the trigger's aria-expanded in sync.
(function () {
  const trigger = document.getElementById('user-menu-trigger');
  const dropdown = document.getElementById('user-menu-dropdown');
  if (!trigger || !dropdown) return;
  function setOpen(open) {
    dropdown.hidden = !open;
    trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  trigger.addEventListener('click', function (e) {
    e.stopPropagation();
    setOpen(dropdown.hidden);
  });
  document.addEventListener('click', function (e) {
    if (!dropdown.hidden && !trigger.contains(e.target) && !dropdown.contains(e.target)) {
      setOpen(false);
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !dropdown.hidden) {
      setOpen(false);
      trigger.focus();
    }
  });
})();

// Theme switcher — three buttons (Light/Dark/Auto) inside the user
// menu dropdown. Stores choice in localStorage; the early-init
// script (theme-init.js) applies it BEFORE the stylesheet loads to
// prevent flash-of-wrong-theme. "Auto" removes the explicit
// override so the CSS media query (prefers-color-scheme) wins.
// Click-through cycler: each click advances light -> dark -> auto -> light.
(function () {
  const root = document.documentElement;
  const cycler = document.getElementById('theme-cycler');
  const order = ['light', 'dark', 'auto'];
  function applyTheme(theme) {
    if (theme === 'auto') {
      root.removeAttribute('data-theme');
    } else {
      root.setAttribute('data-theme', theme);
    }
    if (cycler) {
      // Drives the .theme-cycler-icon-{light,dark,auto} CSS
      // visibility selectors so only the current mode's icon
      // shows next to the "Theme" label.
      cycler.dataset.currentTheme = theme;
    }
  }
  if (cycler) {
    cycler.addEventListener('click', function () {
      const current = cycler.dataset.currentTheme || 'auto';
      const next = order[(order.indexOf(current) + 1) % order.length];
      try { localStorage.setItem('wobblebot_theme', next); } catch (e) {}
      applyTheme(next);
    });
  }
  // Initial: reflect stored preference (or "auto" if none).
  var stored = 'auto';
  try { stored = localStorage.getItem('wobblebot_theme') || 'auto'; } catch (e) {}
  applyTheme(stored);
})();
