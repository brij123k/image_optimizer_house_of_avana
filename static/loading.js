/* Shows that something is loading whenever a request takes a moment.
 *  - a bar across the top and a "Working…" pill (only after 350 ms, so quick
 *    requests don't flash anything)
 *  - a spinner on the button that was clicked, for requests that change data
 * Wraps window.fetch, so every page gets it without touching its own code. */
(function () {
  'use strict';
  var nativeFetch = window.fetch.bind(window);
  var bar = document.createElement('div'); bar.id = 'ihs-topbar';
  var pill = document.createElement('div'); pill.id = 'ihs-busy';
  pill.innerHTML = '<span class="spin"></span><span class="msg">Working…</span>';
  var msg = pill.querySelector('.msg');
  function mount() { document.body.appendChild(bar); document.body.appendChild(pill); }
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);

  var pending = 0, showT = null, midT = null, slowT = null, hideT = null;
  var QUIET = /\/api\/status\b/;     // polled every second — never show a loader for it

  function start() {
    pending++;
    if (pending !== 1) return;
    clearTimeout(hideT);
    showT = setTimeout(function () {
      bar.classList.add('on'); bar.style.width = '35%'; pill.classList.add('show'); msg.textContent = 'Working…';
      midT = setTimeout(function () { if (pending) bar.style.width = '75%'; }, 700);
      slowT = setTimeout(function () { if (pending) msg.textContent = 'Still working — large stores can take a minute…'; }, 8000);
    }, 350);
  }
  function end() {
    pending = Math.max(0, pending - 1);
    if (pending) return;
    clearTimeout(showT); clearTimeout(midT); clearTimeout(slowT);
    pill.classList.remove('show');
    if (bar.classList.contains('on')) {
      bar.style.width = '100%';
      hideT = setTimeout(function () { if (!pending) { bar.classList.remove('on'); bar.style.width = '0'; } }, 300);
    }
  }

  // Safari and Firefox on macOS don't focus a button when it's clicked, so
  // remember the last click ourselves instead of relying on document.activeElement.
  var lastBtn = null, lastAt = 0;
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('button');
    if (b) { lastBtn = b; lastAt = Date.now(); }
  }, true);

  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    if (QUIET.test(url)) return nativeFetch(input, init);
    var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD' && !/^https?:\/\//i.test(url)) {
      // Marks the request as coming from this app's own page (see block_forged_requests in app.py).
      init = Object.assign({}, init);
      init.headers = Object.assign({}, init.headers, { 'X-Requested-With': 'ihs' });
    }
    var btn = (method !== 'GET' && lastBtn && Date.now() - lastAt < 1500 && !lastBtn.disabled) ? lastBtn : null;
    if (btn) btn.classList.add('is-loading');
    start();
    return nativeFetch(input, init).finally(function () {
      end();
      if (btn) btn.classList.remove('is-loading');
    });
  };
})();
