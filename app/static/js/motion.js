/* Motion enhancements: count-up numbers, cursor light on cards, magnetic buttons, hero parallax.
   Purely decorative: nothing depends on this file, it never touches layout, and it does nothing for
   people who prefer reduced motion. Style changes go through the CSSOM (allowed by the strict CSP). */
(function () {
  "use strict";
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) return;
  var fine = window.matchMedia("(hover: hover) and (pointer: fine)").matches;

  // ---------------------------------------------------------------- count-up for dashboard numbers
  function easeOutExpo(t) { return t === 1 ? 1 : 1 - Math.pow(2, -10 * t); }
  document.querySelectorAll(".stat strong").forEach(function (el) {
    var target = parseInt(el.textContent.replace(/[^0-9]/g, ""), 10);
    if (!isFinite(target) || target < 1) return;
    var start = null, dur = Math.min(1400, 500 + target * 12);
    el.textContent = "0";
    function frame(ts) {
      if (start === null) start = ts;
      var p = Math.min(1, (ts - start) / dur);
      el.textContent = String(Math.round(target * easeOutExpo(p)));
      if (p < 1) requestAnimationFrame(frame); else el.textContent = String(target);
    }
    setTimeout(function () { requestAnimationFrame(frame); }, 350);
  });


  // ---------------------------------------------------------------- totals tick on the order form
  var totals = document.querySelectorAll(".totals [id^=t-]");
  if (totals.length && "MutationObserver" in window) {
    var mo = new MutationObserver(function (list) {
      list.forEach(function (m) {
        var el = m.target.nodeType === 3 ? m.target.parentNode : m.target;
        if (!el || !el.classList) return;
        el.classList.remove("is-bump");
        void el.offsetWidth; // restart the animation
        el.classList.add("is-bump");
      });
    });
    totals.forEach(function (el) { mo.observe(el, { childList: true, characterData: true, subtree: true }); });
  }

  // ---------------------------------------------------------------- success / info flash messages fade away
  document.querySelectorAll("main > .notice-success, main > .notice-info").forEach(function (n) {
    setTimeout(function () {
      n.classList.add("is-leaving");
      setTimeout(function () { n.remove(); }, 500);
    }, 6500);
  });

  if (!fine) return;

  // ---------------------------------------------------------------- cursor light on cards
  var SPOT = ".stat, .flow li, .quick a, .choice";
  var raf = 0, pend = null;
  document.addEventListener("pointermove", function (e) {
    var card = e.target.closest ? e.target.closest(SPOT) : null;
    if (card) {
      pend = { el: card, x: e.clientX, y: e.clientY };
      if (!raf) raf = requestAnimationFrame(function () {
        raf = 0;
        var r = pend.el.getBoundingClientRect();
        pend.el.style.setProperty("--mx", (pend.x - r.left) + "px");
        pend.el.style.setProperty("--my", (pend.y - r.top) + "px");
      });
    }

    // Magnetic primary buttons: they lean a few pixels toward the pointer.
    var btn = e.target.closest ? e.target.closest(".btn-primary:not(.btn-block):not(:disabled)") : null;
    if (btn) {
      var b = btn.getBoundingClientRect();
      var dx = (e.clientX - (b.left + b.width / 2)) / b.width;
      var dy = (e.clientY - (b.top + b.height / 2)) / b.height;
      btn.style.translate = (dx * 9).toFixed(1) + "px " + (dy * 7).toFixed(1) + "px";
    }
  }, { passive: true });
  document.addEventListener("pointerout", function (e) {
    var btn = e.target.closest ? e.target.closest(".btn-primary") : null;
    if (btn && !btn.contains(e.relatedTarget)) btn.style.translate = "";
  }, { passive: true });

  // ---------------------------------------------------------------- hero parallax (sign-in)
  var hero = document.querySelector(".login-hero");
  if (hero) {
    window.addEventListener("pointermove", function (e) {
      var r = hero.getBoundingClientRect();
      var px = ((e.clientX - r.left) / r.width - 0.5) * 2;
      var py = ((e.clientY - r.top) / r.height - 0.5) * 2;
      hero.style.setProperty("--px", Math.max(-1, Math.min(1, px)).toFixed(3));
      hero.style.setProperty("--py", Math.max(-1, Math.min(1, py)).toFixed(3));
    }, { passive: true });
  }
})();
