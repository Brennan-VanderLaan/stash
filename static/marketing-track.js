// First-party marketing analytics tracker + consent banner.
//
// Two cookies in play:
//
// * ``stash_consent`` (1 year) — records the user's accept /
//   decline decision.  Set on banner click.  Always set (so we
//   don't keep nagging) even when the user declined.
// * ``stash_mkt`` (30 days) — the session id used for pageview /
//   leave events.  Only ever set when stash_consent == 'accepted'.
//
// Behaviour on each public-page load:
//
// 1. Look at stash_consent.
//    * ``'accepted'`` → run the tracker.
//    * ``'declined'`` → noop.  No banner, no cookie write, no
//      events posted.
//    * No cookie → show the banner.  User clicks Accept (run
//      tracker + remember choice) or Decline (remember choice,
//      stop).
//
// Tracker events go to stash's own ``/marketing/track`` endpoint
// (in the auth bypass list); no third-party data shipping ever.
//
// Disclosed on /about/privacy.

(function () {
  var CONSENT_COOKIE = "stash_consent";
  var TRACK_COOKIE = "stash_mkt";
  var CONSENT_MAX_AGE = 60 * 60 * 24 * 365;  // 1 year
  var TRACK_MAX_AGE = 60 * 60 * 24 * 30;     // 30 days

  // ── Cookie helpers ─────────────────────────────────────────
  function readCookie(name) {
    var entries = (document.cookie || "").split(";");
    for (var i = 0; i < entries.length; i++) {
      var parts = entries[i].split("=");
      var k = (parts[0] || "").trim();
      if (k === name) {
        return decodeURIComponent(parts.slice(1).join("="));
      }
    }
    return "";
  }
  function writeCookie(name, value, maxAge) {
    document.cookie =
      name + "=" + encodeURIComponent(value)
      + ";path=/;max-age=" + maxAge + ";SameSite=Lax";
  }
  function clearCookie(name) {
    document.cookie = name + "=;path=/;max-age=0;SameSite=Lax";
  }

  // ── Tracker (only runs after consent accepted) ─────────────
  function genSessionId() {
    if (window.crypto && window.crypto.getRandomValues) {
      var bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);
      return Array.from(bytes)
        .map(function (b) { return b.toString(16).padStart(2, "0"); })
        .join("");
    }
    return Math.random().toString(36).slice(2) + Date.now().toString(36);
  }
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
    } catch (e) { /* swallow — tracking is best-effort */ }
  }
  function initTracker() {
    var sessionId = readCookie(TRACK_COOKIE);
    if (!sessionId) {
      sessionId = genSessionId();
    }
    // Always refresh max-age so an active visitor doesn't age out.
    writeCookie(TRACK_COOKIE, sessionId, TRACK_MAX_AGE);

    var path = window.location.pathname || "/";
    var loadedAt = Date.now();
    var leaveSent = false;

    post({
      event: "pageview",
      session_id: sessionId,
      path: path,
      referrer: document.referrer || "",
      viewport_w: window.innerWidth || 0,
      viewport_h: window.innerHeight || 0,
    });

    function sendLeave() {
      if (leaveSent) return;
      leaveSent = true;
      post({
        event: "leave",
        session_id: sessionId,
        path: path,
        duration_ms: Date.now() - loadedAt,
      });
    }
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") sendLeave();
    });
    window.addEventListener("pagehide", sendLeave);
    window.addEventListener("beforeunload", sendLeave);
  }

  // ── Consent banner ─────────────────────────────────────────
  function showBanner() {
    if (document.getElementById("consent-banner")) return;
    var banner = document.createElement("div");
    banner.id = "consent-banner";
    banner.className = "consent-banner";
    banner.setAttribute("role", "dialog");
    banner.setAttribute("aria-labelledby", "consent-banner-title");
    banner.innerHTML =
      '<div class="consent-banner-body">'
      + '<h3 id="consent-banner-title" class="consent-banner-title">'
      + 'Page analytics</h3>'
      + '<p class="consent-banner-copy">'
      + 'We use a single first-party cookie to log which pages get '
      + 'visited and how long people stay (page name + time only — '
      + 'no form contents, no third parties, never shared with '
      + 'advertisers).  Helps us know what to fix.  '
      + '<a href="/about/privacy">Privacy details</a>'
      + '</p>'
      + '<div class="consent-banner-actions">'
      + '<button type="button" class="btn btn-secondary btn-sm" '
      + '  data-consent-choice="declined">Decline</button>'
      + '<button type="button" class="btn btn-primary btn-sm" '
      + '  data-consent-choice="accepted">Accept</button>'
      + '</div>'
      + '</div>';
    document.body.appendChild(banner);
    // Fade in next frame so the CSS transition fires.
    requestAnimationFrame(function () {
      banner.classList.add("consent-banner-visible");
    });

    banner.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-consent-choice]");
      if (!btn) return;
      var choice = btn.dataset.consentChoice;
      writeCookie(CONSENT_COOKIE, choice, CONSENT_MAX_AGE);
      banner.classList.remove("consent-banner-visible");
      // Remove from DOM after the transition.
      setTimeout(function () {
        if (banner.parentNode) banner.parentNode.removeChild(banner);
      }, 240);
      if (choice === "accepted") initTracker();
    });
  }

  // ── Entry point ────────────────────────────────────────────
  var consent = readCookie(CONSENT_COOKIE);
  if (consent === "accepted") {
    initTracker();
  } else if (consent === "declined") {
    // Honour the decline: do absolutely nothing.  In particular,
    // clean up any stale tracking cookie left over from a
    // previous accepted state.
    if (readCookie(TRACK_COOKIE)) clearCookie(TRACK_COOKIE);
    return;
  } else {
    // First visit (or cookies cleared) — show the banner.
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", showBanner);
    } else {
      showBanner();
    }
  }
})();
