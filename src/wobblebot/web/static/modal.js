// Modal close behavior (P3 modal layer). CSP is script-src 'self' with
// no unsafe-inline, so this lives here instead of inline handlers.
// The modal container (#modal) shows via CSS whenever it has content;
// "closing" is simply emptying it.
(function () {
  "use strict";

  function closeModal() {
    var modal = document.getElementById("modal");
    if (!modal || modal.innerHTML === "") {
      return;
    }
    modal.innerHTML = "";
    // Closing may interrupt the in-card row-watch before its terminal
    // state (whose own load-refresh would otherwise be the only status
    // update) — so every close refreshes the status card. Guarded: the
    // modal layer can host pages without a status card.
    var status = document.getElementById("status-wrap");
    if (status && window.htmx) {
      window.htmx.ajax("GET", "/status/card", {
        target: "#status-wrap",
        swap: "outerHTML",
      });
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element)) {
      return;
    }
    // Explicit close affordances (X, Cancel, Close buttons).
    if (target.closest("[data-modal-close]")) {
      closeModal();
      return;
    }
    // Clicking the dimmed backdrop (the container itself, outside the
    // card) also closes — standard modal affordance.
    if (target.id === "modal") {
      closeModal();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeModal();
    }
  });
})();
