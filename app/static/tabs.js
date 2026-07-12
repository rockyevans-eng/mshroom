/* tabs.js -- single-page tab switching for the View | Send | Receive layout.
 *
 * The URL hash is the single source of truth (#view / #send / #receive):
 * clicking a tab link changes the hash, the hashchange listener applies it,
 * and other modules switch tabs with MshTabs.show("view") -- e.g. the
 * Receive tab's capture log opening a message in the View tab. The old
 * multi-page URLs (/sender, /listener) still work: the server redirects
 * them to the matching hash.
 *
 * Load order matters: this file must run before viewer.js/sender.js/
 * listener.js only in the sense that MshTabs must exist when a click
 * handler eventually fires -- all three tool modules are loaded on the one
 * page and stay live regardless of which tab is visible.
 */
"use strict";

window.MshTabs = (function () {
  var NAMES = ["view", "send", "receive"];

  /* Show panel `name`, hide the other two, and mark its tab link active.
   * Unknown/empty hash falls back to the View tab. */
  function apply(name) {
    if (NAMES.indexOf(name) === -1) { name = "view"; }
    NAMES.forEach(function (n) {
      document.getElementById("tab-" + n).classList.toggle("hidden", n !== name);
      document.getElementById("tab-link-" + n).classList.toggle("active", n === name);
    });
  }

  function currentName() {
    return (location.hash || "#view").replace("#", "");
  }

  /* Programmatic tab switch. Setting location.hash fires hashchange,
   * which calls apply() -- so browser back/forward and direct calls
   * take the exact same path. */
  function show(name) {
    if (currentName() === name) {
      apply(name); /* hash already right; just make sure the panel shows */
      return;
    }
    location.hash = "#" + name;
  }

  window.addEventListener("hashchange", function () { apply(currentName()); });
  apply(currentName()); /* honor a deep link like /#receive on first load */

  return { show: show };
})();
