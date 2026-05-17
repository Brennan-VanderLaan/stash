// Background watcher for /maintenance and /admin/maintenance pages.
// Polls /maintenance/version every few seconds; when the version
// changes from what the page loaded with (i.e. the backend has
// restarted under a new image — e.g. watchtower pulled a release),
// shows a slide-down toast and gently reloads after a short pause.
//
// "Gentle" means: not a yanked hard reload at the moment of
// detection.  We show the toast first so the operator can see
// what changed, then reload.  The reload itself is still
// location.reload(), but with enough lead time that it doesn't
// feel jerky.
//
// Errors (404 mid-restart, transient network) are swallowed — the
// next poll catches up.  If the page already has the manual
// "Check for updates" button polling on top of this (see
// admin_maintenance.html), both can run in parallel; whichever
// detects the version change first triggers the reload, the
// other becomes a no-op.

(function () {
  const versionEl = document.getElementById("version-value");
  if (!versionEl) return; // page doesn't display a version

  const initialVersion = (versionEl.textContent || "").trim();
  if (!initialVersion) return;

  const POLL_MS = 3000;
  const RELOAD_DELAY_MS = 1500;
  let stopped = false;

  function showToast(newVersion) {
    const toast = document.createElement("div");
    toast.className = "version-update-toast";

    const spinner = document.createElement("span");
    spinner.className = "version-update-toast-spinner";

    const label = document.createElement("span");
    // Build text content via DOM so a maliciously-named version
    // string (vanishingly unlikely on our own /maintenance/version
    // endpoint, but cheap insurance) can't introduce markup.
    label.textContent = "Updated to " + newVersion + " — refreshing…";

    toast.appendChild(spinner);
    toast.appendChild(label);
    document.body.appendChild(toast);

    // Trigger the CSS transition on the next frame so the browser
    // commits the initial off-screen position before we slide it
    // down.  Without this the toast just appears in place.
    requestAnimationFrame(function () {
      toast.classList.add("visible");
    });
  }

  async function poll() {
    if (stopped) return;
    try {
      const r = await fetch("/maintenance/version", { cache: "no-store" });
      if (r.ok) {
        const data = await r.json();
        if (data.version && data.version !== initialVersion) {
          stopped = true;
          showToast(data.version);
          setTimeout(function () {
            location.reload();
          }, RELOAD_DELAY_MS);
          return;
        }
      }
    } catch (_e) {
      // Backend may be mid-restart; absorb and keep polling.
    }
    setTimeout(poll, POLL_MS);
  }

  // Initial delay matches the poll interval so we don't burn a
  // request the instant the page renders — the value we'd compare
  // against is already in initialVersion.
  setTimeout(poll, POLL_MS);
})();
