/* sender.js -- the Sender page: corpus load, MLLP send, ACK mini-tree. */
"use strict";

(function () {
  var hostInput = document.getElementById("send-host");
  var portInput = document.getElementById("send-port");
  var corpusSelect = document.getElementById("corpus-select");
  var messageInput = document.getElementById("send-message");
  var sendBtn = document.getElementById("send-btn");
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

    fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: host, port: port, message: message })
    })
      .then(function (r) {
        if (!r.ok) { throw new Error("send request failed (" + r.status + ")"); }
        return r.json();
      })
      .then(function (data) {
        sendBtn.disabled = false;
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
