// First-party page-leave beacon.
//
// Pageviews are tracked SERVER-SIDE in the route handlers (see
// _track_pageview in app.py) — no client identifier needed for
// that, the request itself is the signal.  This script exists
// only to send a LEAVE event (with the elapsed time on the page)
// when the visitor closes the tab or navigates away, since the
// server has no other way to know dwell time.
//
// No cookie, no localStorage, no client-side identifier.  The
// POST body is just ``{path, duration_ms}``.  The server
// re-derives the same anonymous bucket id the corresponding
// pageview used (sha256 of IP+UA+KEK+30-min-window) so the
// leave links to its pageview without persistent state.  Bucket
// rotates every 30 min — a visitor who lingers past that boundary
// looks like a new visitor, which is the documented privacy
// trade-off of the no-cookie approach.
//
// No consent banner: there's no client-side identifier of any
// kind for the visitor to opt out of.  The server-side bucket
// hash isn't reversible without the deployment's KEK and
// rotates often enough that it's not a stable visitor profile.
// Disclosed on /about/privacy.

(function () {
  function post(body) {
    var json = JSON.stringify(body);
    try {
      if (navigator.sendBeacon) {
        var blob = new Blob([json], { type: "application/json" });
        return navigator.sendBeacon("/marketing/track", blob);
      }
    } catch (e) { /* fall through */ }
    try {
      fetch("/marketing/track", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: json,
        keepalive: true,
      });
    } catch (e) { /* swallow — analytics is best-effort */ }
  }

  var path = window.location.pathname || "/";
  var loadedAt = Date.now();
  var leaveSent = false;

  function sendLeave() {
    if (leaveSent) return;
    leaveSent = true;
    post({ path: path, duration_ms: Date.now() - loadedAt });
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") sendLeave();
  });
  window.addEventListener("pagehide", sendLeave);
  window.addEventListener("beforeunload", sendLeave);
})();
