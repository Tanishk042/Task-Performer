/* Shared behaviour for the simulated company apps: toast rendering plus the
   flaky-render fault, which populates deferred tables from a JSON island. */
(function () {
  "use strict";

  function renderToast(message, kind) {
    var host = document.getElementById("toast-host");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "toast" + (kind ? " toast--" + kind : "");
    el.setAttribute("role", "status");
    el.textContent = message;
    host.appendChild(el);
    window.setTimeout(function () {
      el.style.transition = "opacity .3s";
      el.style.opacity = "0";
      window.setTimeout(function () { el.remove(); }, 320);
    }, 6000);
  }

  window.renderToast = renderToast;

  var boot = function () {
    var pending = window.__TOASTS__ || [];
    pending.forEach(function (t) { renderToast(t.message, t.kind); });
    window.__TOASTS__ = [];

    // Flaky-render fault: <script type="application/json" id="deferred-rows">
    // holds the real <tbody> HTML; we swap it in after a delay. With the fault
    // armed the delay is long enough that a snapshot can race the render.
    var island = document.getElementById("deferred-rows");
    if (!island) return;
    var target = document.getElementById(island.getAttribute("data-target"));
    var delay = parseInt(island.getAttribute("data-delay") || "1400", 10);
    window.setTimeout(function () {
      target.innerHTML = island.textContent;
      island.remove();
      document.body.setAttribute("data-deferred-ready", "1");
    }, delay);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();