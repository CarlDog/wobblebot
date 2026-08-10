// Modal close behavior (P3 modal layer). CSP is script-src 'self' with
// no unsafe-inline, so this lives here instead of inline handlers.
// The modal container (#modal) shows via CSS whenever it has content;
// "closing" is simply emptying it.
(function () {
  "use strict";

  function closeModal() {
    var modal = document.getElementById("modal");
    if (modal) {
      modal.innerHTML = "";
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
