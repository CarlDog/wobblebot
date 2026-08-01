// Theme init — runs BEFORE the stylesheet loads so the page paints
// with the right theme on first frame. Prevents the flash-of-wrong-
// theme on dark-mode browsers loading the light defaults.
//
// Extracted from an inline <script> in base.html (v1.1 CSP fix) —
// script-src is 'self' only, so every template script lives here
// under /static/ instead of inline. Placed before the stylesheet
// <link> in base.html; an external, non-deferred <script src> still
// blocks parsing (and therefore runs before the stylesheet) the same
// way the inline version did.
(function () {
  try {
    var stored = localStorage.getItem('wobblebot_theme');
    if (stored === 'light' || stored === 'dark') {
      document.documentElement.setAttribute('data-theme', stored);
    }
  } catch (e) { /* localStorage unavailable; fall back to auto */ }
})();
