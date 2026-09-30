/**
 * snap-dashboard — minimal vanilla JS
 */

(function () {
  'use strict';

  // ---- Header user menu (avatar/name -> logout popover) -----
  // Small adaptation of Vanilla Framework's contextual-menu pattern,
  // scoped to the nav user menu toggle.
  (function () {
    var toggle = document.querySelector('.nav-user-toggle');
    if (!toggle) return;
    var dropdown = document.getElementById(toggle.getAttribute('aria-controls'));
    if (!dropdown) return;

    function setOpen(open) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      dropdown.setAttribute('aria-hidden', open ? 'false' : 'true');
    }

    toggle.addEventListener('click', function (event) {
      event.preventDefault();
      setOpen(toggle.getAttribute('aria-expanded') !== 'true');
    });

    document.addEventListener('click', function (event) {
      if (!toggle.contains(event.target) && !dropdown.contains(event.target)) {
        setOpen(false);
      }
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        setOpen(false);
      }
    });
  })();


  // ================================================================
  // Shared UI helpers — exposed as window.AK so page scripts can use
  // them too (AK.toast, AK.confirm).
  // ================================================================
  var AK = window.AK = window.AK || {};

  // ---- Toasts ---------------------------------------------------
  function toastRegion() {
    var r = document.getElementById('toast-region');
    if (!r) {
      r = document.createElement('div');
      r.id = 'toast-region';
      r.className = 'toast-region';
      r.setAttribute('role', 'status');
      r.setAttribute('aria-live', 'polite');
      document.body.appendChild(r);
    }
    return r;
  }

  AK.toast = function (message, tone, timeoutMs) {
    var t = document.createElement('div');
    t.className = 'toast toast--' + (tone || 'info');
    var msg = document.createElement('div');
    msg.className = 'toast__msg';
    msg.textContent = message;
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast__close';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';
    close.addEventListener('click', function () { t.remove(); });
    t.appendChild(msg);
    t.appendChild(close);
    toastRegion().appendChild(t);
    var ms = timeoutMs === undefined ? (tone === 'negative' ? 10000 : 5000) : timeoutMs;
    if (ms) setTimeout(function () { t.remove(); }, ms);
    return t;
  };

  // ---- Confirm modal (replaces window.confirm) ------------------
  AK.confirm = function (message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var dlg = document.createElement('dialog');
      if (typeof dlg.showModal !== 'function') {
        resolve(window.confirm(message));
        return;
      }
      dlg.className = 'ak-modal';
      var danger = opts.danger || /remove|delete|clear|reject|override|skip/i.test(message);
      dlg.innerHTML =
        '<form method="dialog">' +
        '<div class="ak-modal__body"><h2 class="ak-modal__title"></h2><p class="ak-modal__text"></p></div>' +
        '<div class="ak-modal__actions">' +
        '<button value="cancel" class="p-button">Cancel</button>' +
        '<button value="ok" class="' + (danger ? 'p-button--negative' : 'p-button--positive') + '"></button>' +
        '</div></form>';
      dlg.querySelector('.ak-modal__title').textContent = opts.title || 'Are you sure?';
      dlg.querySelector('.ak-modal__text').textContent = message;
      dlg.querySelector('button[value="ok"]').textContent = opts.confirmLabel || 'Continue';
      dlg.addEventListener('close', function () {
        var ok = dlg.returnValue === 'ok';
        dlg.remove();
        resolve(ok);
      });
      document.body.appendChild(dlg);
      dlg.showModal();
      dlg.querySelector('button[value="' + (danger ? 'cancel' : 'ok') + '"]').focus();
    });
  };

  // htmx hx-confirm → our modal instead of the browser's native dialog.
  document.addEventListener('htmx:confirm', function (evt) {
    if (!evt.detail || !evt.detail.question) return;
    evt.preventDefault();
    AK.confirm(evt.detail.question).then(function (ok) {
      if (ok) evt.detail.issueRequest(true);
    });
  });

  // Plain (non-htmx) forms: <form data-confirm="...">.
  document.addEventListener('submit', function (evt) {
    var form = evt.target;
    if (!(form instanceof HTMLFormElement)) return;
    var question = form.getAttribute('data-confirm');
    if (!question || form.hasAttribute('hx-post') || form.dataset.confirmed === '1') return;
    evt.preventDefault();
    var submitter = evt.submitter;
    AK.confirm(question).then(function (ok) {
      if (!ok) return;
      form.dataset.confirmed = '1';
      if (form.hasAttribute('data-async-action')) {
        runAsyncAction(form, submitter);
      } else if (form.requestSubmit) {
        form.requestSubmit(submitter || undefined);
      } else {
        form.submit();
      }
      setTimeout(function () { delete form.dataset.confirmed; }, 0);
    });
  }, true);

  // ---- Async actions --------------------------------------------
  // <form data-async-action [data-remove-closest=".row"]>: POSTs via
  // fetch with X-Requested-With so the route answers with JSON
  // ({ok, message}) instead of redirecting, then shows a toast.
  function runAsyncAction(form, submitter) {
    var buttons = form.querySelectorAll('button');
    buttons.forEach(function (b) { b.disabled = true; });
    var fd = new FormData(form);
    if (submitter && submitter.name) fd.append(submitter.name, submitter.value);
    fetch(form.action, {
      method: 'POST',
      body: fd,
      headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' },
      credentials: 'same-origin'
    })
      .then(function (resp) {
        // A non-JSON body (e.g. the login page after a redirect on an
        // expired session) means the action did not run.
        return resp.json().catch(function () { return { ok: false }; }).then(function (data) {
          data = data || {};
          data.ok = resp.ok && data.ok !== false && !data.error;
          return data;
        });
      })
      .then(function (data) {
        var msg = data.message || data.error || (data.ok ? 'Done.' : 'Something went wrong.');
        AK.toast(msg, data.ok ? 'positive' : 'negative');
        if (data.ok) {
          var sel = form.getAttribute('data-remove-closest');
          var target = sel ? form.closest(sel) : null;
          if (target) target.remove();
          else if (data.redirect) window.location.href = data.redirect;
        }
      })
      .catch(function () { AK.toast('Network error — please try again.', 'negative'); })
      .finally(function () { buttons.forEach(function (b) { b.disabled = false; }); });
  }

  document.addEventListener('submit', function (evt) {
    var form = evt.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-async-action')) return;
    if (evt.defaultPrevented) return;
    evt.preventDefault();
    runAsyncAction(form, evt.submitter);
  });

  // ---- Client-side tabs -----------------------------------------
  // <div data-tabs> <nav class="tab-bar" role="tablist"> <button role="tab"
  // aria-controls="panel-id"> … </nav> <section id="panel-id" class="tab-panel"
  // role="tabpanel"> … The active tab is mirrored in the URL hash.
  function initTabs(root) {
    (root || document).querySelectorAll('[data-tabs]').forEach(function (container) {
      if (container.dataset.tabsInit) return;
      container.dataset.tabsInit = '1';
      var tabs = Array.prototype.slice.call(container.querySelectorAll('[role="tab"]'));
      if (!tabs.length) return;

      function select(tab, updateHash) {
        tabs.forEach(function (t) {
          var on = t === tab;
          t.setAttribute('aria-selected', on ? 'true' : 'false');
          t.setAttribute('tabindex', on ? '0' : '-1');
          t.classList.toggle('is-active', on);
          var panel = document.getElementById(t.getAttribute('aria-controls'));
          if (panel) panel.hidden = !on;
        });
        if (updateHash && history.replaceState) {
          history.replaceState(null, '', '#' + tab.getAttribute('aria-controls'));
        }
      }

      tabs.forEach(function (tab, i) {
        tab.addEventListener('click', function () { select(tab, true); });
        tab.addEventListener('keydown', function (e) {
          var next = null;
          if (e.key === 'ArrowRight') next = tabs[(i + 1) % tabs.length];
          if (e.key === 'ArrowLeft') next = tabs[(i - 1 + tabs.length) % tabs.length];
          if (next) { e.preventDefault(); next.focus(); select(next, true); }
        });
      });

      var fromHash = window.location.hash.slice(1);
      var initial = tabs.filter(function (t) { return t.getAttribute('aria-controls') === fromHash; })[0]
        || tabs.filter(function (t) { return t.getAttribute('aria-selected') === 'true'; })[0]
        || tabs[0];
      select(initial, false);
    });
  }
  AK.initTabs = initTabs;
  initTabs(document);
  document.addEventListener('htmx:afterSettle', function (evt) { initTabs(evt.target); });

  // Links to "#panel-id" elsewhere on the page switch tabs too.
  window.addEventListener('hashchange', function () {
    var id = window.location.hash.slice(1);
    var tab = id && document.querySelector('[role="tab"][aria-controls="' + CSS.escape(id) + '"]');
    if (tab) tab.click();
  });

  // ---- Close <details class="more-menu"> on outside click / Esc --
  document.addEventListener('click', function (evt) {
    document.querySelectorAll('details.more-menu[open]').forEach(function (d) {
      if (!d.contains(evt.target)) d.removeAttribute('open');
    });
  });
  document.addEventListener('keydown', function (evt) {
    if (evt.key !== 'Escape') return;
    document.querySelectorAll('details.more-menu[open]').forEach(function (d) {
      d.removeAttribute('open');
    });
  });

  // ---- Intersection observer fade-in on .card elements -----
  if ('IntersectionObserver' in window) {
    // Mark cards for animation
    document.querySelectorAll('.card').forEach(function (card) {
      card.classList.add('fade-in');
    });

    const observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add('visible');
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.1 }
    );

    document.querySelectorAll('.card.fade-in').forEach(function (card) {
      observer.observe(card);
    });
  }

})();
