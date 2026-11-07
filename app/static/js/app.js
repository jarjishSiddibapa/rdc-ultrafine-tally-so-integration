/* Progressive enhancement only: every page works without this file except live totals. */
(function () {
  "use strict";

  // Prevent double submits and show a loading state in the pressed button.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || form.dataset.submitting === "1") {
      if (form && form.dataset && form.dataset.submitting === "1") e.preventDefault();
      return;
    }
    form.dataset.submitting = "1";
    var btn = e.submitter || form.querySelector("button[type=submit]");
    if (btn) {
      btn.classList.add("is-loading");
      btn.setAttribute("aria-busy", "true");
    }
    // If the browser comes back via bfcache, unlock the form.
    window.addEventListener("pageshow", function () {
      form.dataset.submitting = "0";
      if (btn) { btn.classList.remove("is-loading"); btn.removeAttribute("aria-busy"); }
    }, { once: true });
  });

  // Confirm dialog for destructive actions: <button data-confirm="Message" data-confirm-label="Delete">
  var confirmDlg = document.getElementById("confirm-dialog");
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-confirm]");
    if (t && confirmDlg && typeof confirmDlg.showModal === "function") {
      e.preventDefault();
      var form = t.form;
      confirmDlg.querySelector("[data-msg]").textContent = t.dataset.confirm;
      var ok = confirmDlg.querySelector("[data-ok]");
      ok.textContent = t.dataset.confirmLabel || "Confirm";
      ok.onclick = function () { confirmDlg.close(); if (form) { form.dataset.submitting = "0"; form.requestSubmit(t); } };
      confirmDlg.showModal();
      return;
    }
    var tp = e.target.closest("[data-toggle-password]");
    if (tp) {
      var pw = document.getElementById(tp.dataset.togglePassword);
      var show = pw.type === "password";
      pw.type = show ? "text" : "password";
      tp.textContent = show ? "Hide" : "Show";
      return;
    }
    var open = e.target.closest("[data-open-dialog]");
    if (open) {
      var d = document.getElementById(open.dataset.openDialog);
      if (d && typeof d.showModal === "function") { d.showModal(); var f = d.querySelector("textarea,input"); if (f) f.focus(); }
    }
    var close = e.target.closest("[data-close-dialog]");
    if (close) { var dd = close.closest("dialog"); if (dd) dd.close(); }
  });

  // ---------------------------------------------------------------- order form
  var form = document.getElementById("order-form");
  if (!form) return;

  var linesEl = document.getElementById("lines");
  var tpl = document.getElementById("line-template");
  var plantSel = form.querySelector("[name=plant_id]");
  function plantState() {
    var o = plantSel && plantSel.options[plantSel.selectedIndex];
    return (o && o.dataset.state) || "";
  }
  var MAX = 50;

  function num(v) {
    var n = parseFloat(String(v).replace(/,/g, ""));
    return isFinite(n) ? n : 0;
  }
  function inr(n) {
    return n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function round2(n) { return Math.round((n + Number.EPSILON) * 100) / 100; }

  // Unit labels follow the chosen item, and the narration preview is built the same way the server builds it.
  function fmtQty(v) {
    var n = parseFloat(String(v).replace(/,/g, ""));
    return isFinite(n) ? String(parseFloat(n.toFixed(3))) : "";
  }
  function narrate() {
    var box = document.getElementById("narration-preview");
    if (!box) return;
    var partySel = form.querySelector("[name=party_id]");
    var party = partySel && partySel.value ? partySel.options[partySel.selectedIndex].text : "";
    var parts = [];
    linesEl.querySelectorAll(".line").forEach(function (row) {
      var sel = row.querySelector("[name=item_id]");
      var opt = sel.options[sel.selectedIndex];
      var unit = opt && opt.dataset.unit ? opt.dataset.unit : "";
      row.querySelectorAll(".uom").forEach(function (u) { u.textContent = unit || "unit"; });
      var q = fmtQty(row.querySelector("[name=quantity]").value), r = num(row.querySelector("[name=rate]").value);
      if (opt && opt.value && q && r > 0 && party) {
        parts.push("We sold " + q + " " + unit + " of " + opt.dataset.name + " to " + party + " at Rs " + inr(r) + " per " + unit + ".");
      }
    });
    box.textContent = parts.length ? parts.join(" ") : "Choose the customer and add items to see the narration.";
    box.classList.toggle("is-empty", !parts.length);
  }

  function recalc() {
    narrate();
    var billing = form.querySelector("[name=billing_state_code]").value;
    var companyState = plantState();
    var byRate = {}, subtotal = 0;
    linesEl.querySelectorAll(".line").forEach(function (row) {
      var sel = row.querySelector("[name=item_id]");
      var opt = sel.options[sel.selectedIndex];
      var rate = opt && opt.dataset.gst ? num(opt.dataset.gst) : 0;
      var amt = round2(num(row.querySelector("[name=quantity]").value) * num(row.querySelector("[name=rate]").value));
      row.querySelector(".amount").textContent = amt ? inr(amt) : "";
      subtotal += amt;
      byRate[rate] = (byRate[rate] || 0) + amt;
    });
    var mode = !billing ? "" : (billing === companyState ? "cgst" : "igst");
    var cgst = 0, igst = 0;
    Object.keys(byRate).forEach(function (r) {
      var base = round2(byRate[r]);
      if (mode === "cgst") cgst += round2(base * (num(r) / 2) / 100);
      if (mode === "igst") igst += round2(base * num(r) / 100);
    });
    var set = function (id, v) { var el = document.getElementById(id); if (el) el.textContent = v; };
    set("t-sub", inr(subtotal));
    set("t-cgst", inr(cgst)); set("t-sgst", inr(cgst)); set("t-igst", inr(igst));
    document.getElementById("row-cgst").hidden = mode !== "cgst";
    document.getElementById("row-sgst").hidden = mode !== "cgst";
    document.getElementById("row-igst").hidden = mode !== "igst";
    set("t-total", inr(subtotal + 2 * cgst + igst));
    var note = document.getElementById("tax-note");
    if (note) {
      note.textContent = !companyState ? "Choose the plant to see which tax applies."
        : !billing ? "Choose the billing state to see which tax applies."
        : mode === "cgst" ? "Billing address is in the plant's state: Output CGST and Output SGST apply."
        : "Billing address is outside the plant's state: Output IGST applies.";
    }
  }

  function renumber() {
    var rows = linesEl.querySelectorAll(".line");
    rows.forEach(function (row, i) {
      row.querySelectorAll("[data-for]").forEach(function (el) {
        var base = el.dataset.for;
        el.id = base + "-" + i;
      });
      row.querySelectorAll("label[data-for]").forEach(function (l) { l.htmlFor = l.dataset.for + "-" + i; });
      var rm = row.querySelector(".remove");
      if (rm) rm.hidden = rows.length === 1;
    });
    document.getElementById("add-line").disabled = rows.length >= MAX;
  }

  document.getElementById("add-line").addEventListener("click", function () {
    if (linesEl.querySelectorAll(".line").length >= MAX) return;
    linesEl.appendChild(tpl.content.cloneNode(true));
    renumber();
    var last = linesEl.querySelector(".line:last-child select");
    if (last) last.focus();
  });

  linesEl.addEventListener("click", function (e) {
    var rm = e.target.closest(".remove");
    if (!rm) return;
    var row = rm.closest(".line");
    if (linesEl.querySelectorAll(".line").length > 1) { row.remove(); renumber(); recalc(); }
  });
  form.addEventListener("input", recalc);
  form.addEventListener("change", recalc);

  // Picking a customer pre-fills addresses, state and payment term (all still editable).
  var party = form.querySelector("[name=party_id]");
  party.addEventListener("change", function () {
    var o = party.options[party.selectedIndex];
    if (!o || !o.value) return;
    var fill = function (name, val, onlyIfEmpty) {
      var el = form.querySelector("[name=" + name + "]");
      if (el && val && (!onlyIfEmpty || !el.value.trim())) el.value = val;
    };
    var addr = o.dataset.address || "";
    fill("billing_address", addr, true);
    fill("shipping_address", addr, true);
    fill("billing_state_code", o.dataset.state, true);
    fill("shipping_state_code", o.dataset.state, true);
    fill("payment_term", o.dataset.term, true);
    recalc();
  });

  renumber();
  recalc();
})();

// Receipt printer after submitting an order: open it as a modal; the animation itself is pure CSS.
(function () {
  var dlg = document.querySelector("[data-receipt]");
  if (!dlg) return;
  try { history.replaceState(null, "", location.pathname); } catch (e) { /* not essential */ }   // a reload won't replay it
  if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
  dlg.scrollTop = 0;   // the browser focuses the first button, which would scroll the printer out of sight
  var firstBtn = dlg.querySelector("[data-receipt-close]");
  if (firstBtn) { try { firstBtn.focus({ preventScroll: true }); } catch (e) { /* older browsers */ } }
  // Fit the whole printer + slip into the window at any size or browser zoom (100%, 80%, a phone...).
  var fit = dlg.querySelector("[data-fit]"), pending = 0;
  function refit() {
    pending = 0;
    fit.style.setProperty("--rp-scale", "1");
    var h = fit.getBoundingClientRect().height;                 // natural height, in CSS px at the current browser zoom
    var room = window.innerHeight - 28;                          // dialog padding + breathing space
    var scale = h > 0 ? Math.max(0.6, Math.min(1, room / h)) : 1;
    fit.style.setProperty("--rp-scale", scale.toFixed(3));
  }
  function queueFit() { if (!pending) pending = requestAnimationFrame(refit); }
  refit();
  window.addEventListener("resize", queueFit, { passive: true });  // browser zoom and window resize both fire this
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(queueFit);
  dlg.addEventListener("close", function () { window.removeEventListener("resize", queueFit); });
  dlg.addEventListener("click", function (e) {
    if (e.target === dlg || (e.target.closest && e.target.closest("[data-receipt-close]"))) {
      if (e.target !== dlg) e.preventDefault();
      dlg.close();
    }
  });
})();

/* Phone layout for tables: copy each column heading onto its cells so CSS can show the row as a card. */
(function () {
  "use strict";
  document.querySelectorAll(".table-wrap table").forEach(function (t) {
    var heads = Array.prototype.map.call(t.querySelectorAll("thead th"), function (th) {
      return th.querySelector(".sr-only") ? "" : th.textContent.trim();
    });
    t.querySelectorAll("tbody tr").forEach(function (tr) {
      Array.prototype.forEach.call(tr.children, function (td, i) {
        if (!heads[i]) return;
        if (!td.textContent.trim() && !td.querySelector("*")) { td.classList.add("cell-empty"); return; }  // nothing to show on a phone
        td.setAttribute("data-label", heads[i]);
        var v = document.createElement("span");   // one value block, so a cell's main and sub lines stay stacked
        v.className = "cell-v";
        while (td.firstChild) v.appendChild(td.firstChild);
        td.appendChild(v);
      });
    });
    t.classList.add("cards-on-phone");
  });
})();

/* Lists: changing "rows per page" applies straight away; everything else waits for Apply. */
(function () {
  "use strict";
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (el && el.matches && el.matches("[data-autosubmit]") && el.form) el.form.requestSubmit();
  });
  // Only one dropdown menu (Download) open at a time; close it when clicking elsewhere.
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.menu[open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
  });
})();


/* Notification bell: keeps the unread badge and the dropdown fresh. It asks the server every 30 seconds while the
   tab is visible. The summary request does not count as activity, so an abandoned tab still signs out on time. */
(function () {
  "use strict";
  var bell = document.querySelector("[data-bell]");
  if (!bell || !window.fetch) return;
  var badge = bell.querySelector("[data-bell-count]");
  var list = bell.querySelector("[data-bell-list]");
  var toasts = document.querySelector("[data-toasts]");
  var base = document.title;
  var lastId = null, stopped = false, timer = null;

  function text(el, cls, value) { var s = document.createElement(el); if (cls) s.className = cls; s.textContent = value; return s; }

  function paint(data) {
    var n = data.unread || 0;
    badge.textContent = n > 99 ? "99+" : String(n);
    badge.hidden = n === 0;
    var side = document.querySelector("[data-nav-unread]");     // the same count in the sidebar
    if (side) { side.textContent = badge.textContent; side.hidden = n === 0; side.setAttribute("aria-label", n + " unread"); }
    bell.querySelector("summary").setAttribute("aria-label", "Notifications" + (n ? ", " + n + " unread" : ""));
    document.title = (n ? "(" + n + ") " : "") + base.replace(/^\(\d+\) /, "");
    list.textContent = "";
    if (!data.items.length) { list.appendChild(text("li", "bell-empty", "You are all caught up.")); return; }
    data.items.forEach(function (it) {
      var li = document.createElement("li");
      if (!it.read) li.className = "unread";
      var a = document.createElement("a");
      a.href = it.href;
      var t = text("span", "t", "");
      t.appendChild(text("i", "lvl lvl-" + it.level, ""));
      t.appendChild(document.createTextNode(it.title));
      a.appendChild(t);
      if (it.body) a.appendChild(text("span", "b", it.body));
      a.appendChild(text("span", "w", it.when));
      li.appendChild(a);
      list.appendChild(li);
    });
  }

  function toast(it) {
    if (!toasts) return;
    var a = document.createElement("a");
    a.className = "toast";
    a.href = it.href;
    a.appendChild(text("b", "", it.title));
    if (it.body) a.appendChild(text("span", "", it.body));
    toasts.appendChild(a);
    setTimeout(function () { a.remove(); }, 8000);
  }

  function refresh() {
    if (stopped) return;
    fetch("/notifications/summary", { credentials: "same-origin", headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) { stopped = true; return null; }     // signed out or idle: stop asking
        if (!r.ok || (r.headers.get("content-type") || "").indexOf("json") < 0) return null;
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        var newest = data.items.reduce(function (m, it) { return it.id > m ? it.id : m; }, 0);
        if (lastId !== null && newest > lastId) {
          data.items.filter(function (it) { return it.id > lastId && !it.read; }).slice(0, 3).forEach(toast);
        }
        lastId = newest;
        paint(data);
      })
      .catch(function () { /* offline: try again next time */ });
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(function () { if (document.visibilityState === "visible") refresh(); }, 30000);
  }
  bell.addEventListener("toggle", function () { if (bell.open) refresh(); });
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") refresh(); });
  refresh();
  schedule();
})();
