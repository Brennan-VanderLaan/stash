// Real-time availability check for the leaderboard-handle forms
// on /usage and /leaderboard.  Feedback #67: server already
// rejects taken handles on POST, but the user only finds out
// after submitting + getting redirected back with an error.
// Inline feedback while typing is nicer.
//
// Wires onto any form with class ``contrib-handle-form`` or
// ``leaderboard-handle-form``.  Looks for a single
// ``<input name="handle">`` inside and appends a
// ``<span class="handle-availability">`` that the form's CSS
// styles (red for "taken", green for "ok").
//
// Debounce keeps the /usage/handle/check endpoint from getting
// hammered on every keystroke — only fires after the user pauses
// typing for 350 ms.  The submit button is disabled while a
// candidate is known-taken so a hasty user can't fire a doomed
// POST.

(function () {
  const forms = document.querySelectorAll(
    ".contrib-handle-form, .leaderboard-handle-form"
  );
  forms.forEach(initOne);

  function initOne(form) {
    const input = form.querySelector('input[name="handle"]');
    if (!input) return;
    const submitBtn = form.querySelector('button[type="submit"]');
    const status = document.createElement("span");
    status.className = "handle-availability";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    // Append at the END of the form so it wraps under the input
    // + button via the CSS ``flex: 1 1 100%`` rule.
    form.appendChild(status);

    const initialValue = (input.value || "").trim();
    let debounceTimer = null;
    let inFlight = null;

    async function probe(value) {
      // Empty input → clear status, don't call the endpoint.
      if (!value) {
        setStatus("", "");
        if (submitBtn) submitBtn.disabled = false;
        return;
      }
      // The user re-typed their CURRENT handle — don't warn them
      // about themselves.  The POST upsert is idempotent.
      if (value === initialValue) {
        setStatus("", "");
        if (submitBtn) submitBtn.disabled = false;
        return;
      }
      if (inFlight) inFlight.abort();
      const ac = new AbortController();
      inFlight = ac;
      try {
        const u = new URL("/usage/handle/check", window.location.origin);
        u.searchParams.set("handle", value);
        const r = await fetch(u.toString(), {
          signal: ac.signal,
          headers: { Accept: "application/json" },
        });
        if (!r.ok) throw new Error("http " + r.status);
        const data = await r.json();
        if (ac.signal.aborted) return;
        if (data.available) {
          setStatus("✓ available", "ok");
          if (submitBtn) submitBtn.disabled = false;
        } else {
          setStatus(data.reason || "Not available.", "taken");
          if (submitBtn) submitBtn.disabled = true;
        }
      } catch (err) {
        if (err && err.name === "AbortError") return;
        // Network blip or 5xx — don't disable submit; the server's
        // POST validation still catches taken handles.  Just
        // clear the inline status so the form doesn't lie.
        setStatus("", "");
        if (submitBtn) submitBtn.disabled = false;
      } finally {
        if (inFlight === ac) inFlight = null;
      }
    }

    function setStatus(text, state) {
      status.textContent = text;
      if (state) status.dataset.state = state;
      else delete status.dataset.state;
    }

    input.addEventListener("input", function () {
      const value = (input.value || "").trim();
      // Show a "checking…" placeholder so the user knows we're
      // waiting on debounce + network, not silently ignoring.
      if (value && value !== initialValue) {
        setStatus("Checking…", "");
      } else {
        setStatus("", "");
      }
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () {
        probe(value);
      }, 350);
    });
  }
})();
