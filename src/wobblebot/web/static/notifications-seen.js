// Visiting the notifications page = "operator has seen the latest
// notifications." Stamp localStorage so the bell-badge clears on the
// next poll. The bell's own JS (nav.js) reads this value.
//
// Extracted from an inline <script> in notifications.html (v1.1 CSP
// fix) — script-src is 'self' only.
(function () {
  localStorage.setItem('wobblebot_last_seen_notifications', new Date().toISOString());
  // Trigger bell update immediately so the dot clears without waiting
  // for the next 30s poll cycle.
  if (window.updateBellBadge) window.updateBellBadge();
})();
