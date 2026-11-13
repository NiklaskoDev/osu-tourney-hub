// Small behaviours the server-rendered pages need. No inline scripts anywhere (CSP).
(function () {
  "use strict";

  var AVAILABILITY_STATES = ["yes", "maybe", "no"];
  var AVAILABILITY_LABELS = { yes: "Yes", maybe: "Maybe", no: "No" };
  var COPIED_RESET_MS = 2000;

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  // <time data-local datetime="2026-11-12T20:00:00Z"> shows the viewer's local time.
  function renderLocalTimes(root) {
    var formatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
    root.querySelectorAll("time[data-local]").forEach(function (el) {
      var moment = new Date(el.getAttribute("datetime"));
      if (isNaN(moment)) return;
      el.title = el.getAttribute("datetime") + " (UTC)";
      el.textContent = formatter.format(moment);
    });
  }

  // <time data-countdown datetime="..."> counts down to the instant, then reads "Started".
  function formatCountdown(ms) {
    var total = Math.floor(ms / 1000);
    var days = Math.floor(total / 86400);
    var clock = pad(Math.floor((total % 86400) / 3600)) + ":" + pad(Math.floor((total % 3600) / 60)) + ":" + pad(total % 60);
    return days > 0 ? days + "d " + clock : clock;
  }

  function tickCountdowns() {
    document.querySelectorAll("time[data-countdown]").forEach(function (el) {
      var remaining = new Date(el.getAttribute("datetime")) - Date.now();
      if (isNaN(remaining)) return;
      el.textContent = remaining > 0 ? formatCountdown(remaining) : "Started";
    });
  }

  // Fill timezone fields that are still empty with the browser's IANA zone.
  function detectTimezone(root) {
    var zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!zone) return;
    root.querySelectorAll("[data-detect-timezone]").forEach(function (el) {
      if (!el.value) el.value = zone;
    });
  }

  // A dialog marked data-autoopen holds a form that came back with an error: show it again.
  function openMarkedDialogs(root) {
    root.querySelectorAll("dialog[data-autoopen]").forEach(function (dialog) {
      if (!dialog.open) dialog.showModal();
    });
  }

  function renderAll(root) {
    renderLocalTimes(root);
    detectTimezone(root);
    tickCountdowns();
  }

  function copyText(button) {
    var text = button.getAttribute("data-copy");
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(text).then(function () {
      var label = button.querySelector("[data-copy-label]");
      var original = label.textContent;
      label.textContent = button.getAttribute("data-copied-label") || "Copied";
      button.classList.add("is-done");
      window.setTimeout(function () {
        label.textContent = original;
        button.classList.remove("is-done");
      }, COPIED_RESET_MS);
    });
  }

  function cycleAvailability(cell) {
    var current = AVAILABILITY_STATES.indexOf(cell.getAttribute("data-state"));
    var next = AVAILABILITY_STATES[(current + 1) % AVAILABILITY_STATES.length];
    cell.setAttribute("data-state", next);
    cell.querySelector("[data-avail-label]").textContent = AVAILABILITY_LABELS[next];
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element)) return;

    var opener = target.closest("[data-dialog-open]");
    if (opener) {
      var dialog = document.getElementById(opener.getAttribute("data-dialog-open"));
      if (dialog) dialog.showModal();
      return;
    }
    var copyButton = target.closest("[data-copy]");
    if (copyButton) {
      copyText(copyButton);
      return;
    }
    var cell = target.closest("[data-cycle-state]");
    if (cell) cycleAvailability(cell);
  });

  // <select data-nav-select> holds same-site paths; choosing one goes there.
  document.addEventListener("change", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLSelectElement) || !target.hasAttribute("data-nav-select")) return;
    if (target.value.charAt(0) === "/" && target.value.charAt(1) !== "/") {
      window.location.assign(target.value);
    }
  });

  // <form data-disable-on-submit> turns its buttons off once sent, so a double click cannot repeat a
  // slow or paid request. Coming back through the history restores the page, so turn them on again.
  function setSubmitButtons(form, disabled) {
    form.querySelectorAll("button[type=submit]").forEach(function (button) {
      button.disabled = disabled;
    });
  }
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-disable-on-submit")) return;
    // Disable after the browser has read the form, so the request still goes out.
    window.setTimeout(function () {
      setSubmitButtons(form, true);
    }, 0);
  });
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    document.querySelectorAll("form[data-disable-on-submit]").forEach(function (form) {
      setSubmitButtons(form, false);
    });
  });

  document.addEventListener("DOMContentLoaded", function () {
    renderAll(document);
    openMarkedDialogs(document);
    window.setInterval(tickCountdowns, 1000);
  });
  // The live page shows a note while its event stream is down; the browser reconnects by itself.
  function showLiveConnection(visible) {
    var note = document.querySelector("[data-live-connection]");
    if (note) note.hidden = !visible;
  }
  document.addEventListener("htmx:sseError", function () {
    showLiveConnection(true);
  });
  document.addEventListener("htmx:sseOpen", function () {
    showLiveConnection(false);
  });
  document.addEventListener("htmx:sseClose", function () {
    showLiveConnection(false);
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    renderAll(event.target);
  });
})();
