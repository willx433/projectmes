// Station kiosk client (P3-05/P3-06/P3-07). Thin client per DD §12.1: no
// state machine lives here -- every action round-trips to the server. This
// file only does what a browser must do locally: keep the hidden scanner
// input focused, turn scan/finish payloads into fetch() calls against the
// real JSON endpoints (POST /scan, POST /operations/{id}/finish -- both
// pydantic JSON bodies, not form-encoded), show the offline screen when a
// request fails outright, and run the measurement keypad overlay.
//
// Everything else (pause/resume/clock-out/substep actions) is a plain HTML
// <form method="post"> that round-trips normally (PRG) -- no JS required,
// works even if this file fails to load, matches CR-009's "thin client"
// spirit and the rest of the app's admin-form convention.

(function () {
  "use strict";

  // -- request_id (state-machine.md §7 idempotency) ------------------------

  function newRequestId() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    // ponytail: Math.random fallback for non-secure-context/old WebViews --
    // uniqueness, not unpredictability, is all §7 idempotency needs.
    return "rid-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    var field = form.querySelector(".request-id-field");
    if (field) field.value = newRequestId();
  }, true);

  // -- offline screen (fetch failure -> full-screen paper-guide fallback) --

  var OFFLINE_PATH = "/station/offline";

  function goOffline() {
    if (window.location.pathname !== OFFLINE_PATH) {
      window.location.href = OFFLINE_PATH;
    }
  }

  function fetchJson(url, options) {
    return fetch(url, options).catch(function (err) {
      goOffline();
      throw err;
    });
  }

  // htmx's own error hooks (the idle queue's 10s poll) -- same fallback.
  document.body && document.body.addEventListener("htmx:sendError", goOffline);
  document.body && document.body.addEventListener("htmx:responseError", function (e) {
    if (e.detail && e.detail.xhr && e.detail.xhr.status >= 500) goOffline();
  });

  if (window.location.pathname === OFFLINE_PATH) {
    var tries = 0;
    var retry = setInterval(function () {
      tries += 1;
      var el = document.getElementById("offline-retry-count");
      if (el) el.textContent = "Retry " + tries + "…";
      fetch("/healthz").then(function (resp) {
        if (resp.ok) {
          clearInterval(retry);
          window.location.href = "/station";
        }
      }).catch(function () { /* still offline, keep looping */ });
    }, 5000);
  }

  // -- scan bar (hidden always-focused input + manual fallback, CR-009) ----

  var scanForm = document.getElementById("scan-form");
  var hiddenInput = document.getElementById("scan-hidden-input");
  var manualInput = document.getElementById("scan-manual-input");
  var banner = document.getElementById("scan-banner");

  function keepScannerFocused() {
    if (!hiddenInput) return;
    var active = document.activeElement;
    // don't steal focus from a manual text field, a modal, or a form the
    // operator is actively filling in.
    if (active && active !== document.body && active !== hiddenInput) return;
    hiddenInput.focus();
  }
  if (hiddenInput) {
    setInterval(keepScannerFocused, 1000);
    keepScannerFocused();
  }

  var RESULT_MESSAGES = {
    badge_rejected: "Badge not recognized.",
    operator_required: "Scan your badge before scanning a box.",
    unknown_box: "Unknown box code.",
    unbound_box: "This box isn't assigned to a unit yet.",
    unit_terminal: "This unit is already done or scrapped.",
    unit_busy_elsewhere: "Another operator already has this unit open elsewhere.",
    already_active: "Already active -- opening it now.",
  };

  function showBanner(text, isError) {
    if (!banner) { if (text) alert(text); return; }
    banner.textContent = text || "";
    banner.className = "scan-banner" + (isError ? " scan-banner-error" : "");
  }

  // POST /scan's rejection context never echoes the raw payload back
  // (app/domain/statemachine.py's `_reject` only adds unit_id/unit_status +
  // whatever `context=` the specific branch passes) -- remembered here so
  // the wrong-station screen's override resubmission has something to
  // re-send.
  var lastScanPayload = null;

  function handleScanResult(result) {
    var code = result.code;
    var ctx = result.context || {};
    if (code === "operator_session_opened") {
      window.location.reload();
    } else if (code === "accepted" || code === "already_active") {
      window.location.href = "/station/scan-result/" + ctx.unit_id;
    } else if (code === "wrong_station") {
      var params = new URLSearchParams({
        payload: lastScanPayload || "",
        expected_work_center: ctx.expected_work_center || "",
        operation_title: ctx.expected_operation || "",
      });
      window.location.href = "/station/wrong-station?" + params.toString();
    } else {
      showBanner(RESULT_MESSAGES[code] || ("Scan rejected (" + code + ")"), true);
    }
  }

  function submitScan(payload, extra) {
    if (!payload) return;
    lastScanPayload = payload;
    var body = Object.assign({ payload: payload, request_id: newRequestId() }, extra || {});
    fetchJson("/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (resp) { return resp.json().then(function (data) { return { resp: resp, data: data }; }); })
      .then(function (r) {
        if (r.resp.status === 403) { showBanner(r.data.detail || "Override rejected.", true); return; }
        handleScanResult(r.data);
      })
      .catch(function () { /* goOffline() already ran inside fetchJson */ });
  }

  if (scanForm) {
    scanForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var payload = (manualInput && manualInput.value.trim()) || (hiddenInput && hiddenInput.value.trim());
      submitScan(payload);
      if (hiddenInput) hiddenInput.value = "";
      if (manualInput) manualInput.value = "";
      keepScannerFocused();
    });
  }

  // wedge scanners send Enter-terminated payloads into whichever input has
  // focus -- the hidden input is normally focused, so Enter alone submits.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && document.activeElement === hiddenInput && scanForm) {
      e.preventDefault();
      scanForm.requestSubmit ? scanForm.requestSubmit() : scanForm.dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  // -- wrong-station override form (posts override_badge to /scan) ---------

  var overrideForm = document.getElementById("override-form");
  if (overrideForm) {
    overrideForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var payload = overrideForm.querySelector("[name=payload]").value;
      var overrideBadge = overrideForm.querySelector("[name=override_badge]").value.trim();
      submitScan(payload, { override_badge: overrideBadge });
    });
  }

  // -- finish operation (fixed contract: POST /operations/{id}/finish) -----

  document.querySelectorAll(".finish-form").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var planOpId = form.dataset.planOpId;
      var unitId = form.dataset.unitId;
      var btn = form.querySelector("button");
      btn.disabled = true;
      fetchJson("/operations/" + planOpId + "/finish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ unit_id: unitId, request_id: newRequestId() }),
      }).then(function (resp) {
        return resp.json().then(function (data) { return { resp: resp, data: data }; });
      }).then(function (r) {
        if (r.resp.ok) {
          window.location.href = "/station";
        } else {
          btn.disabled = false;
          alert((r.data && r.data.detail) || "Could not finish the operation.");
        }
      }).catch(function () { btn.disabled = false; });
    });
  });

  // -- elapsed timer (display only -- server times are authoritative) ------

  var footer = document.querySelector(".exec-footer");
  if (footer && footer.dataset.startedAt) {
    var startedAt = new Date(footer.dataset.startedAt).getTime();
    var elapsedEl = document.getElementById("elapsed-timer");
    function tick() {
      var s = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      var hh = String(Math.floor(s / 3600)).padStart(2, "0");
      var mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
      var ss = String(s % 60).padStart(2, "0");
      if (elapsedEl) elapsedEl.textContent = hh + ":" + mm + ":" + ss;
    }
    tick();
    setInterval(tick, 1000);
  }

  // -- measurement keypad overlay (P3-07, DD §12.2.4) -----------------------

  window.openMeasurementKeypad = function (unitId, stepSeq, subSeq, spec) {
    spec = spec || {};
    var nominal = Number(spec.nominal);
    var tolPlus = Math.abs(Number(spec.tol_plus || 0));
    var tolMinus = Math.abs(Number(spec.tol_minus || 0));
    var lower = nominal - tolMinus;
    var upper = nominal + tolPlus;

    var overlay = document.createElement("div");
    overlay.className = "keypad-overlay";
    overlay.innerHTML =
      '<div class="keypad-card">' +
      '<p class="muted">Nominal ' + (isNaN(nominal) ? "—" : nominal) + " " + (spec.unit || "") +
      " &middot; tolerance +" + tolPlus + " / -" + tolMinus + '</p>' +
      '<div class="keypad-display" id="keypad-display">&nbsp;</div>' +
      '<div class="keypad-grid">' +
      ["7", "8", "9", "4", "5", "6", "1", "2", "3", ".", "0", "⌫"]
        .map(function (k) { return '<button type="button" class="btn keypad-key" data-key="' + k + '">' + k + "</button>"; })
        .join("") +
      "</div>" +
      '<div class="keypad-actions">' +
      '<button type="button" class="btn" id="keypad-cancel">Cancel</button>' +
      '<button type="button" class="btn btn-danger" id="keypad-submit" disabled>Record reading</button>' +
      "</div></div>";
    document.body.appendChild(overlay);

    var display = overlay.querySelector("#keypad-display");
    var submitBtn = overlay.querySelector("#keypad-submit");
    var value = "";

    function refresh() {
      display.textContent = value || " ";
      display.classList.remove("keypad-ok", "keypad-bad");
      submitBtn.disabled = value === "" || value === "-" || value === ".";
      if (!submitBtn.disabled) {
        var v = parseFloat(value);
        if (!isNaN(v) && !isNaN(nominal)) {
          display.classList.add(v >= lower && v <= upper ? "keypad-ok" : "keypad-bad");
        }
      }
    }

    overlay.querySelectorAll(".keypad-key").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var k = btn.dataset.key;
        if (k === "⌫") value = value.slice(0, -1);
        else if (k === "." && value.includes(".")) { /* no-op */ }
        else value += k;
        refresh();
      });
    });

    overlay.querySelector("#keypad-cancel").addEventListener("click", function () {
      overlay.remove();
    });

    submitBtn.addEventListener("click", function () {
      // real form submit (PRG), not fetch: the measurement endpoint is a
      // plain form-post-then-redirect like every other substep action
      // (app/api/substeps.py), so a native submission lands the browser on
      // the redirect target (including any ?error=) for free -- no need to
      // hand-roll response/redirect handling in JS for this one case.
      var form = document.createElement("form");
      form.method = "post";
      form.action = "/station/units/" + unitId + "/substeps/" + stepSeq + "/" + subSeq + "/measurement";
      form.style.display = "none";
      [["value", value], ["request_id", newRequestId()]].forEach(function (pair) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = pair[0];
        input.value = pair[1];
        form.appendChild(input);
      });
      document.body.appendChild(form);
      form.submit();
    });

    refresh();
  };
})();
