/* tree.js -- shared HL7 tree renderer + text-offset mapping helpers.
 *
 * Used by both the Viewer (full tree with two-way highlighting) and the
 * Sender (mini-tree for the ACK). Plain vanilla JS, no dependencies.
 *
 * Everything is exposed on a single global object: window.HL7Tree
 */
"use strict";

window.HL7Tree = (function () {

  /* ------------------------------------------------------------------
   * Display mapping.
   *
   * HL7 uses bare \r segment terminators, which browsers don't render
   * as line breaks reliably. We display the text with \n line breaks
   * instead, and keep two lookup arrays so character offsets can be
   * translated both ways:
   *   origToDisp[i] = display offset for original offset i
   *   dispToOrig[j] = original offset for display offset j
   * (both have one extra trailing entry so end-offsets translate too).
   * ------------------------------------------------------------------ */
  function buildDisplayMap(originalText) {
    var display = [];
    var origToDisp = [];
    var dispToOrig = [];
    var i = 0;
    while (i < originalText.length) {
      var ch = originalText.charAt(i);
      if (ch === "\r") {
        var crlf = (i + 1 < originalText.length && originalText.charAt(i + 1) === "\n");
        origToDisp.push(display.length);
        if (crlf) { origToDisp.push(display.length); } /* the \n maps to same spot */
        dispToOrig.push(i);
        display.push("\n");
        i += crlf ? 2 : 1;
      } else {
        origToDisp.push(display.length);
        dispToOrig.push(i);
        display.push(ch);
        i += 1;
      }
    }
    origToDisp.push(display.length); /* end sentinel */
    dispToOrig.push(originalText.length);
    return {
      text: display.join(""),
      origToDisp: origToDisp,
      dispToOrig: dispToOrig
    };
  }

  /* ------------------------------------------------------------------
   * Tree rendering.
   *
   * nodes: the JSON array from /api/parse (or the ACK tree).
   * options:
   *   onSelect(node, li)  -- called when a node row is clicked
   *   collapsible         -- default true; segments render expanded,
   *                          deeper levels collapsed
   * Returns { root, byRef } where byRef maps node.ref -> its <li>.
   * ------------------------------------------------------------------ */
  function render(container, nodes, options) {
    options = options || {};
    var byRef = {};
    container.textContent = "";
    var rootUl = document.createElement("ul");
    rootUl.className = "hl7-tree";
    nodes.forEach(function (node) {
      rootUl.appendChild(renderNode(node, byRef, options, 0));
    });
    container.appendChild(rootUl);
    return { root: rootUl, byRef: byRef };
  }

  function renderNode(node, byRef, options, depth) {
    var li = document.createElement("li");
    li.className = "tree-node kind-" + node.kind;
    li.dataset.ref = node.ref;
    byRef[node.ref] = li;

    var row = document.createElement("div");
    row.className = "node-row";

    var hasChildren = node.children && node.children.length > 0;

    var twisty = document.createElement("span");
    twisty.className = "twisty" + (hasChildren ? "" : " twisty-leaf");
    twisty.textContent = hasChildren ? "▾" : "·"; /* ▾ / · */
    row.appendChild(twisty);

    var refSpan = document.createElement("span");
    refSpan.className = "node-ref";
    refSpan.textContent = node.label || node.ref;
    row.appendChild(refSpan);

    if (node.name) {
      var nameSpan = document.createElement("span");
      nameSpan.className = "node-name";
      nameSpan.textContent = node.name;
      row.appendChild(nameSpan);
    }

    if (node.kind !== "segment") {
      var valSpan = document.createElement("span");
      valSpan.className = "node-value" + (node.value === "" ? " empty" : "");
      valSpan.textContent = node.value === "" ? "(empty)" : node.value;
      row.appendChild(valSpan);
    }

    var copyBtn = document.createElement("button");
    copyBtn.className = "copy-btn";
    copyBtn.type = "button";
    copyBtn.title = "Copy reference " + node.ref;
    copyBtn.textContent = "⧉"; /* ⧉ */
    copyBtn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      copyText(node.ref, copyBtn);
    });
    row.appendChild(copyBtn);

    row.addEventListener("click", function () {
      if (options.onSelect) { options.onSelect(node, li); }
    });
    twisty.addEventListener("click", function (ev) {
      if (!hasChildren) { return; }
      ev.stopPropagation();
      li.classList.toggle("collapsed");
    });

    li.appendChild(row);

    if (hasChildren) {
      var ul = document.createElement("ul");
      ul.className = "children";
      node.children.forEach(function (child) {
        ul.appendChild(renderNode(child, byRef, options, depth + 1));
      });
      li.appendChild(ul);
      /* segments start expanded; everything deeper starts collapsed */
      if (node.kind !== "segment") { li.classList.add("collapsed"); }
    }
    return li;
  }

  function copyText(text, btn) {
    function flash() {
      btn.classList.add("copied");
      setTimeout(function () { btn.classList.remove("copied"); }, 700);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(flash, function () { fallbackCopy(text); flash(); });
    } else {
      fallbackCopy(text);
      flash();
    }
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* best effort */ }
    document.body.removeChild(ta);
  }

  /* ------------------------------------------------------------------
   * Offset search: deepest tree node containing [start, end) in
   * original-text space. For a plain click, pass end === start.
   * ------------------------------------------------------------------ */
  function findDeepest(nodes, start, end) {
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var contains;
      if (start === end) {
        /* point: inside the span, or exactly at a non-empty node's start */
        contains = (start >= node.start && start < node.end) ||
                   (start === node.start && node.start === node.end);
      } else {
        contains = (start >= node.start && end <= node.end);
      }
      if (contains) {
        var deeper = node.children && node.children.length
          ? findDeepest(node.children, start, end)
          : null;
        return deeper || node;
      }
    }
    return null;
  }

  /* Expand all collapsed ancestors of li, scroll it into view, flash it. */
  function revealAndFlash(li) {
    var anc = li.parentElement;
    while (anc) {
      if (anc.classList && anc.classList.contains("tree-node")) {
        anc.classList.remove("collapsed");
      }
      anc = anc.parentElement;
    }
    li.classList.remove("collapsed");
    li.scrollIntoView({ block: "center", behavior: "smooth" });
    var row = li.querySelector(":scope > .node-row");
    row.classList.remove("flash");
    /* force a reflow so re-adding the class restarts the animation */
    void row.offsetWidth;
    row.classList.add("flash");
  }

  return {
    buildDisplayMap: buildDisplayMap,
    render: render,
    findDeepest: findDeepest,
    revealAndFlash: revealAndFlash
  };
})();
