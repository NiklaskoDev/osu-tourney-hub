// The availability grid: pick a slot or a range in your own column, save it with one request.
// Loaded only on the availability page, after htmx. No inline scripts anywhere (CSP).
(function () {
  "use strict";

  var STATES = ["yes", "maybe", "no"];
  var SLOTS_PER_DAY = 48;
  var CELL = "button[data-slot]";
  var MESSAGES = {
    403: "You cannot change this row, or your session expired. Reload the page and try again.",
    409: "This tournament is archived, or this person is not on the roster. Nothing was saved.",
    422: "That range cannot be saved. Nothing was changed."
  };

  // The server only knows a timezone for people who saved one. Ask the browser and reload.
  function detectZone() {
    var marker = document.querySelector("[data-detect-zone]");
    var zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!marker || !zone) return;
    var url = new URL(window.location.href);
    url.searchParams.set("tz", "local");
    url.searchParams.set("zone", zone);
    window.location.replace(url.toString());
  }

  function init(grid) {
    var status = document.getElementById("avail-status");
    var selection = null; // {anchor, current}: a range built with the keyboard or the mouse
    var drag = null;
    var suppressClick = false;
    var queue = Promise.resolve();
    var lastSlot = null; // the slot last focused: the tab stop and where focus returns after a save

    function cells() {
      return Array.prototype.slice.call(grid.querySelectorAll(CELL + ":not(:disabled)"));
    }

    function nextState(state) {
      return STATES[(STATES.indexOf(state) + 1) % STATES.length];
    }

    function span(a, b) {
      var all = cells();
      var from = all.indexOf(a);
      var to = all.indexOf(b);
      if (from < 0 || to < 0) return [];
      return all.slice(Math.min(from, to), Math.max(from, to) + 1);
    }

    function mark(list) {
      grid.querySelectorAll(CELL + "[data-selected]").forEach(function (cell) {
        cell.removeAttribute("data-selected");
      });
      list.forEach(function (cell) {
        cell.setAttribute("data-selected", "true");
      });
    }

    function clearSelection() {
      selection = null;
      mark([]);
    }

    // Only one cell is a tab stop; the arrow keys move between the others.
    function roving(preferred) {
      var all = cells();
      var chosen = all.filter(function (cell) {
        return cell.getAttribute("data-slot") === preferred;
      })[0] || all[0];
      all.forEach(function (cell) {
        cell.tabIndex = cell === chosen ? 0 : -1;
      });
    }

    grid.addEventListener("focusin", function (event) {
      var cell = event.target.closest && event.target.closest(CELL);
      if (cell) lastSlot = cell.getAttribute("data-slot");
    });

    function focusCell(cell) {
      if (!cell) return;
      roving(cell.getAttribute("data-slot"));
      cell.focus();
    }

    function say(text) {
      if (status) status.textContent = text;
    }

    function save(list, state) {
      if (!list.length) return;
      var values = {
        start: list[0].getAttribute("data-slot"),
        end: list[list.length - 1].getAttribute("data-slot"),
        state: state,
        tz: grid.getAttribute("data-tz"),
        zone: grid.getAttribute("data-zone"),
        osu_id: grid.getAttribute("data-member")
      };
      say("Saving…");
      queue = queue.then(function () {
        return htmx.ajax("POST", grid.getAttribute("data-post-url"), {
          target: status,
          swap: "innerHTML",
          values: values
        });
      }).catch(function () {
        // The error events below already told the person.
      });
    }

    function apply(list, anchor) {
      save(list, nextState(anchor.getAttribute("data-state")));
      clearSelection();
    }

    // After a save the changed rows are new elements: restore the tab stop and the focus.
    document.addEventListener("htmx:afterSettle", function () {
      roving(lastSlot);
      var cell = lastSlot && grid.querySelector(CELL + '[data-slot="' + lastSlot + '"]');
      if (cell && document.activeElement === document.body) cell.focus();
    });
    document.addEventListener("htmx:responseError", function (event) {
      say(MESSAGES[event.detail.xhr.status] || "Could not save. Try again in a moment.");
    });
    document.addEventListener("htmx:sendError", function () {
      say("The server could not be reached. Nothing was saved.");
    });

    // Mouse and pen: press on a slot and drag along the column to choose a range.
    grid.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 || event.pointerType === "touch") return;
      var cell = event.target.closest(CELL);
      if (!cell || cell.disabled) return;
      event.preventDefault(); // no text selection while dragging
      cell.focus();
      roving(cell.getAttribute("data-slot"));
      drag = { anchor: cell, current: cell, moved: false };
      clearSelection();
    });
    document.addEventListener("pointermove", function (event) {
      if (!drag) return;
      var under = document.elementFromPoint(event.clientX, event.clientY);
      var cell = under && under.closest ? under.closest(CELL) : null;
      if (!cell || cell.disabled || cell === drag.current) return;
      drag.current = cell;
      drag.moved = true;
      mark(span(drag.anchor, cell));
    });
    document.addEventListener("pointerup", function () {
      if (!drag) return;
      var finished = drag;
      drag = null;
      apply(finished.moved ? span(finished.anchor, finished.current) : [finished.anchor], finished.anchor);
      // The click that follows a press must not change the slot a second time.
      suppressClick = true;
      window.setTimeout(function () {
        suppressClick = false;
      }, 0);
    });
    document.addEventListener("pointercancel", function () {
      drag = null;
      clearSelection();
    });

    // Keyboard (Space or Enter) and touch taps arrive as clicks.
    grid.addEventListener("click", function (event) {
      if (suppressClick) return;
      var cell = event.target.closest(CELL);
      if (!cell || cell.disabled) return;
      if (selection) {
        apply(span(selection.anchor, selection.current), selection.anchor);
      } else {
        apply([cell], cell);
      }
    });

    grid.addEventListener("keydown", function (event) {
      var cell = event.target.closest && event.target.closest(CELL);
      if (!cell) return;
      var all = cells();
      var index = all.indexOf(cell);
      var step = { ArrowDown: 1, ArrowUp: -1, PageDown: SLOTS_PER_DAY, PageUp: -SLOTS_PER_DAY }[event.key];
      var target = null;
      if (step !== undefined) {
        target = all[Math.max(0, Math.min(all.length - 1, index + step))];
      } else if (event.key === "Home") {
        target = all[0];
      } else if (event.key === "End") {
        target = all[all.length - 1];
      } else if (event.key === "Escape") {
        clearSelection();
        return;
      } else {
        return;
      }
      event.preventDefault();
      if (event.shiftKey && step !== undefined && Math.abs(step) === 1) {
        selection = selection || { anchor: cell, current: cell };
        selection.current = target;
        mark(span(selection.anchor, target));
      } else {
        clearSelection();
      }
      focusCell(target);
    });

    roving(null);
  }

  document.addEventListener("DOMContentLoaded", function () {
    detectZone();
    var grid = document.querySelector("[data-avail-grid]");
    if (grid) init(grid);
  });
})();
