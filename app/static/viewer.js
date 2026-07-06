/* viewer.js -- the Viewer page: paste -> tree + raw, two-way highlighting. */
"use strict";

(function () {
  var input = document.getElementById("message-input");
  var parseBtn = document.getElementById("parse-btn");
  var corpusSelect = document.getElementById("corpus-select");
  var panes = document.getElementById("viewer-panes");
  var treePane = document.getElementById("tree-pane");
  var rawPane = document.getElementById("raw-pane");
  var summaryBar = document.getElementById("summary-bar");

  /* State for the currently parsed message. */
  var state = null; /* { originalText, tree, map, byRef, selectedRow } */

  /* ---------------- Listener handoff ----------------
   * The Listener page's "open in Viewer" link stashes a captured
   * message's text here and redirects; if present, load and parse it
   * immediately, then clear it so a plain reload of "/" doesn't repeat it. */
  (function loadListenerHandoff() {
    var preload = sessionStorage.getItem("hl7wb_preload_text");
    if (!preload) { return; }
    sessionStorage.removeItem("hl7wb_preload_text");
    input.value = preload;
    doParse();
  })();

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
    .catch(function () { /* corpus list is a convenience; ignore failure */ });

  corpusSelect.addEventListener("change", function () {
    if (!corpusSelect.value) { return; }
    fetch("/api/corpus/" + encodeURIComponent(corpusSelect.value))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        input.value = data.text;
        doParse();
      });
  });

  /* ---------------- parse ---------------- */

  parseBtn.addEventListener("click", doParse);

  function doParse() {
    var text = input.value;
    if (!text.trim()) {
      summaryBar.textContent = "Nothing to parse — paste a message first.";
      return;
    }
    fetch("/api/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    })
      .then(function (r) {
        if (!r.ok) { throw new Error("parse request failed (" + r.status + ")"); }
        return r.json();
      })
      .then(function (data) { showMessage(text, data); })
      .catch(function (err) {
        summaryBar.textContent = "Error: " + err.message;
      });
  }

  function showMessage(originalText, data) {
    var map = HL7Tree.buildDisplayMap(originalText);
    var rendered = HL7Tree.render(treePane, data.tree, { onSelect: onTreeSelect });
    state = {
      originalText: originalText,
      tree: data.tree,
      map: map,
      byRef: rendered.byRef,
      selectedRow: null
    };
    renderRaw(null, null);
    panes.classList.remove("hidden");

    var s = data.summary || {};
    var bits = [];
    if (s.message_type) { bits.push("<strong>" + esc(s.message_type) + "</strong>"); }
    if (s.control_id) { bits.push("control ID <strong>" + esc(s.control_id) + "</strong>"); }
    if (s.version) { bits.push("v" + esc(s.version)); }
    bits.push(data.segment_count + " segment" + (data.segment_count === 1 ? "" : "s"));
    summaryBar.innerHTML = bits.join(" &nbsp;·&nbsp; ");
  }

  function esc(s) {
    var d = document.createElement("span");
    d.textContent = s;
    return d.innerHTML;
  }

  /* ---------------- raw pane rendering + highlight ---------------- */

  /* Rebuild the raw pane, optionally highlighting original-text span
   * [origStart, origEnd). Uses the display map so \r shows as a line break. */
  function renderRaw(origStart, origEnd) {
    var map = state.map;
    rawPane.textContent = "";
    if (origStart === null || origStart === undefined) {
      rawPane.appendChild(document.createTextNode(map.text));
      return;
    }
    var ds = map.origToDisp[origStart];
    var de = map.origToDisp[origEnd];
    rawPane.appendChild(document.createTextNode(map.text.slice(0, ds)));
    var hl = document.createElement("span");
    hl.className = ds === de ? "hl hl-empty" : "hl";
    hl.textContent = map.text.slice(ds, de);
    rawPane.appendChild(hl);
    rawPane.appendChild(document.createTextNode(map.text.slice(de)));
    hl.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  /* ---------------- tree -> raw ---------------- */

  function onTreeSelect(node, li) {
    if (state.selectedRow) { state.selectedRow.classList.remove("selected"); }
    var row = li.querySelector(":scope > .node-row");
    row.classList.add("selected");
    state.selectedRow = row;
    renderRaw(node.start, node.end);
  }

  /* ---------------- raw -> tree ---------------- */

  rawPane.addEventListener("mouseup", function () {
    if (!state) { return; }
    var sel = window.getSelection();
    if (!sel.rangeCount) { return; }
    var range = sel.getRangeAt(0);
    if (!rawPane.contains(range.startContainer)) { return; }

    var dispStart = displayOffsetOf(range.startContainer, range.startOffset);
    var dispEnd = rawPane.contains(range.endContainer)
      ? displayOffsetOf(range.endContainer, range.endOffset)
      : dispStart;
    if (dispStart === null) { return; }
    if (dispEnd === null || dispEnd < dispStart) { dispEnd = dispStart; }

    var origStart = state.map.dispToOrig[dispStart];
    var origEnd = state.map.dispToOrig[dispEnd];
    var node = HL7Tree.findDeepest(state.tree, origStart, origEnd);
    if (!node) { return; }
    var li = state.byRef[node.ref];
    if (!li) { return; }
    if (state.selectedRow) { state.selectedRow.classList.remove("selected"); }
    var row = li.querySelector(":scope > .node-row");
    row.classList.add("selected");
    state.selectedRow = row;
    HL7Tree.revealAndFlash(li);
  });

  /* Character offset of (textNode, offsetInNode) within the raw pane's
   * full display text, accounting for the highlight <span> wrapper. */
  function displayOffsetOf(container, offset) {
    var walker = document.createTreeWalker(rawPane, NodeFilter.SHOW_TEXT);
    var total = 0;
    var textNode;
    /* If the container is an element (click on the pane itself), resolve
     * to its offset-th child's start. */
    if (container.nodeType !== Node.TEXT_NODE) {
      if (container === rawPane || rawPane.contains(container)) {
        var target = container.childNodes[offset] || null;
        while ((textNode = walker.nextNode())) {
          if (target && (textNode === target || target.contains && target.contains(textNode))) {
            return total;
          }
          total += textNode.nodeValue.length;
        }
        return target ? null : total;
      }
      return null;
    }
    while ((textNode = walker.nextNode())) {
      if (textNode === container) { return total + offset; }
      total += textNode.nodeValue.length;
    }
    return null;
  }
})();
