/* sender.js -- the Send tab: corpus load, MLLP send, ACK mini-tree. */
"use strict";

(function () {
  var hostInput = document.getElementById("send-host");
  var portInput = document.getElementById("send-port");
  var corpusSelect = document.getElementById("send-corpus-select");
  var messageInput = document.getElementById("send-message");
  var sendBtn = document.getElementById("send-btn");
  var keepOpenBox = document.getElementById("send-keep-open");
  var closeConnBtn = document.getElementById("close-conn-btn");
  var connNote = document.getElementById("send-conn-note");
  var statusEl = document.getElementById("send-status");
  var ackPanel = document.getElementById("ack-panel");
  var ackSummary = document.getElementById("ack-summary");
  var ackTree = document.getElementById("ack-tree");
  var ackRaw = document.getElementById("ack-raw");

  /* ---------------- corpus dropdown ---------------- */

  fetch("/api/corpus")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      data.files.forEach(function (f) {
        var opt = document.createElement("option");
        opt.value = f.name;
        opt.textContent = f.name + (f.message_type ? "  (" + f.message_type + ")" : "");
        corpusSelect.appendChild(opt);
      });
    })
    .catch(function () { /* convenience only */ });

  corpusSelect.addEventListener("change", function () {
    if (!corpusSelect.value) { return; }
    fetch("/api/corpus/" + encodeURIComponent(corpusSelect.value))
      .then(function (r) { return r.json(); })
      .then(function (data) { messageInput.value = data.text; });
  });

  /* ---------------- keep connection open ---------------- */

  /* Like an interface engine's "Keep Connection Open": unchecked = a new
     connection per message; checked = the server keeps one socket per
     host:port between sends. The browser can't hold that socket itself, so
     the server does (see /api/send and /api/send/close). */

  function currentTarget() {
    return { host: hostInput.value.trim(), port: parseInt(portInput.value, 10) };
  }

  function closeConnection(quiet) {
    var target = currentTarget();
    if (!target.host || !target.port) { return; }
    fetch("/api/send/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(target)
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!quiet) { connNote.textContent = data.closed ? "connection closed" : "no open connection"; }
      })
      .catch(function () { /* best effort: the server also closes idle connections */ });
  }

  keepOpenBox.addEventListener("change", function () {
    closeConnBtn.classList.toggle("hidden", !keepOpenBox.checked);
    connNote.textContent = "";
    /* Turning the option off drops the held connection so "off" really is
       one-connection-per-message from here on. */
    if (!keepOpenBox.checked) { closeConnection(true); }
  });

  closeConnBtn.addEventListener("click", function () { closeConnection(false); });

  /* ---------------- send ---------------- */

  sendBtn.addEventListener("click", function () {
    var host = hostInput.value.trim();
    var port = parseInt(portInput.value, 10);
    var message = messageInput.value;

    if (!host) { return setStatus("err", "Enter a host."); }
    if (!port || port < 1 || port > 65535) { return setStatus("err", "Enter a valid port (1–65535)."); }
    if (!message.trim()) { return setStatus("err", "Nothing to send — paste or load a message first."); }

    setStatus("", "Sending to " + host + ":" + port + " …");
    sendBtn.disabled = true;
    ackPanel.classList.add("hidden");
    connNote.textContent = "";

    fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: host, port: port, message: message, keep_open: keepOpenBox.checked })
    })
      .then(function (r) {
        if (!r.ok) { throw new Error("send request failed (" + r.status + ")"); }
        return r.json();
      })
      .then(function (data) {
        sendBtn.disabled = false;
        connNote.textContent = data.reused ? "connection reused" : "new connection";
        if (!data.ok) {
          setStatus("err", data.error);
          return;
        }
        showAck(data.ack);
      })
      .catch(function (err) {
        sendBtn.disabled = false;
        setStatus("err", "Error: " + err.message);
      });
  });

  function setStatus(cls, text) {
    statusEl.className = cls;
    statusEl.textContent = text;
  }

  function showAck(ack) {
    if (ack.code === null) {
      setStatus("err", "Got a response, but it contains no MSA segment — not an ACK.");
    } else if (ack.is_accept) {
      setStatus("ok", "Message accepted (" + ack.code + ").");
    } else {
      setStatus("err", "Receiver responded " + ack.code + (ack.text ? " — " + ack.text : "") + ".");
    }

    var codeCls = ack.is_accept ? "aa" : "bad";
    ackSummary.innerHTML = "";
    var p = document.createElement("p");
    var codeSpan = document.createElement("span");
    codeSpan.className = "ack-code " + codeCls;
    codeSpan.textContent = ack.code === null ? "(no MSA)" : ack.code;
    p.appendChild(document.createTextNode("MSA-1: "));
    p.appendChild(codeSpan);
    if (ack.control_id) {
      p.appendChild(document.createTextNode("  ·  acknowledged control ID: " + ack.control_id));
    }
    if (ack.text) {
      p.appendChild(document.createTextNode("  ·  " + ack.text));
    }
    ackSummary.appendChild(p);

    HL7Tree.render(ackTree, ack.tree, {});
    /* show the ACK's raw text with \r rendered as line breaks */
    ackRaw.textContent = HL7Tree.buildDisplayMap(ack.raw).text;
    ackPanel.classList.remove("hidden");
  }
})();
