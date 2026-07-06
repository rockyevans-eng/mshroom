/* listener.js -- the Listener page: start/stop, probe counters, capture log.
 *
 * Polling only (no websockets) -- fine for a lab tool. Clicking an HL7 row
 * hands its text to the Viewer via sessionStorage and navigates there.
 */
"use strict";

(function () {
  var statusBadge = document.getElementById("listener-status");
  var portEl = document.getElementById("listener-port");
  var startBtn = document.getElementById("start-btn");
  var stopBtn = document.getElementById("stop-btn");
  var errorEl = document.getElementById("listener-error");
  var countersEl = document.getElementById("counters");
  var eventsBody = document.getElementById("events-body");
  var eventsEmpty = document.getElementById("events-empty");

  var POLL_MS = 3000;
  var CLASS_ORDER = [
    "HL7", "NON_HL7_PAYLOAD", "HTTP_PROBE", "TLS_PROBE", "SCAN_PROBE", "JUNK", "TIMEOUT"
  ];

  /* ---------------- status + counters ---------------- */

  function refreshStatus() {
    fetch("/api/listener/status")
      .then(function (r) { return r.json(); })
      .then(renderStatus)
      .catch(function () { /* transient network hiccup; next poll retries */ });
  }

  function renderStatus(data) {
    statusBadge.textContent = data.running ? "RUNNING" : "STOPPED";
    statusBadge.className = "status-badge " + (data.running ? "ok" : "err");
    portEl.textContent = data.port;
    startBtn.disabled = data.running;
    stopBtn.disabled = !data.running;

    if (data.error) {
      errorEl.textContent = data.error;
      errorEl.classList.remove("hidden");
    } else {
      errorEl.classList.add("hidden");
    }

    countersEl.innerHTML = "";
    CLASS_ORDER.forEach(function (cls) {
      var n = (data.counts && data.counts[cls]) || 0;
      var span = document.createElement("span");
      span.className = "counter";
      span.textContent = cls + ": " + n;
      countersEl.appendChild(span);
    });
  }

  /* ---------------- capture log table ---------------- */

  function refreshEvents() {
    fetch("/api/listener/events?limit=200")
      .then(function (r) { return r.json(); })
      .then(function (data) { renderEvents(data.events || []); })
      .catch(function () { /* transient; next poll retries */ });
  }

  function renderEvents(events) {
    eventsBody.innerHTML = "";
    eventsEmpty.classList.toggle("hidden", events.length > 0);
    events.forEach(function (ev) {
      var tr = document.createElement("tr");
      tr.appendChild(cell(ev.timestamp));
      tr.appendChild(cell(ev.peer_host + ":" + ev.peer_port));
      var classCell = cell(ev.event_class);
      classCell.className = "class-tag class-" + ev.event_class;
      tr.appendChild(classCell);
      tr.appendChild(cell(ev.msh9 || ""));
      tr.appendChild(cell(ev.msh10 || ""));
      tr.appendChild(cell(ev.ack_code || ""));
      if (ev.has_message) {
        tr.className = "clickable-row";
        tr.title = "Open in Viewer";
        tr.addEventListener("click", function () { openInViewer(ev.id); });
      }
      eventsBody.appendChild(tr);
    });
  }

  function cell(text) {
    var td = document.createElement("td");
    td.textContent = text;
    return td;
  }

  function openInViewer(id) {
    fetch("/api/listener/events/" + id)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var text = data.event && data.event.full_message;
        if (!text) { return; }
        sessionStorage.setItem("hl7wb_preload_text", text);
        window.location.href = "/";
      })
      .catch(function () { /* nothing to open; leave the page as-is */ });
  }

  /* ---------------- controls ---------------- */

  startBtn.addEventListener("click", function () {
    startBtn.disabled = true;
    fetch("/api/listener/start", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        renderStatus(data);
        refreshEvents();
      })
      .catch(function () { startBtn.disabled = false; });
  });

  stopBtn.addEventListener("click", function () {
    stopBtn.disabled = true;
    fetch("/api/listener/stop", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(renderStatus)
      .catch(function () { stopBtn.disabled = false; });
  });

  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, POLL_MS);
  setInterval(refreshEvents, POLL_MS);
})();
