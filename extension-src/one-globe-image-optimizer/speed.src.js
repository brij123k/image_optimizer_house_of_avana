/* one-globe-image-optimizer. Readable source; assets/speed.js is the minified build.
 * Reads its settings from window.IHS (written by blocks/speed.liquid). */
(function () {
  'use strict';
  var C = window.IHS;
  if (!C || window.__ihsLoaded) return;
  window.__ihsLoaded = true;

  var CDN = /^https?:\/\/cdn\.shopify\.com\/s\/files\//;
  var WIDTHS = [360, 540, 720, 900, 1080, 1296, 1512, 1728];
  var BLOCKED = 'javascript/ihs-blocked';

  function list(v) {
    return String(v || '').split(/[\n,]+/).map(function (s) { return s.trim().toLowerCase(); }).filter(Boolean);
  }
  var cssExclude = list(C.cssExclude), jsExclude = list(C.jsExclude), delayList = list(C.delayPatterns);
  function matches(url, pats) {
    url = String(url || '').toLowerCase();
    return pats.some(function (p) { return url.indexOf(p) > -1; });
  }

  /* ---------- images: lazy + responsive ---------- */
  var seen = new WeakSet(), imgIndex = 0;

  function makeResponsive(img) {
    var src = img.getAttribute('src');
    if (!src || img.hasAttribute('srcset') || !CDN.test(src)) return;
    var base = src.replace(/([?&])width=\d+&?/, '$1').replace(/[?&]$/, '');
    var sep = base.indexOf('?') > -1 ? '&' : '?';
    var intrinsic = parseInt(img.getAttribute('width'), 10) || 0;
    var ws = WIDTHS.filter(function (w) { return !intrinsic || w <= intrinsic; });
    if (ws.length < 2) return;
    img.setAttribute('srcset', ws.map(function (w) { return base + sep + 'width=' + w + ' ' + w + 'w'; }).join(', '));
    if (!img.hasAttribute('sizes')) {
      var w = Math.round(img.getBoundingClientRect().width);
      img.setAttribute('sizes', w > 0 ? w + 'px' : '100vw');
    }
  }

  function processImage(img, final) {
    if (!seen.has(img)) {
      seen.add(img);
      var idx = imgIndex++;
      if (C.lazy && idx >= (C.skip | 0) && !img.hasAttribute('loading') && img.getAttribute('fetchpriority') !== 'high') {
        img.setAttribute('loading', 'lazy');
        img.setAttribute('decoding', 'async');
      }
    }
    if (C.responsive && final) makeResponsive(img);
  }

  /* ---------- stylesheets: defer ---------- */
  function processLink(l) {
    if (!C.deferCss || l.rel !== 'stylesheet' || l.__ihs) return;
    if (l.media && l.media !== 'all') return;
    if (matches(l.href, cssExclude)) return;
    l.__ihs = true;
    var restore = function () { l.media = 'all'; };
    l.media = 'print';
    l.addEventListener('load', restore);
    setTimeout(restore, 4000); // never leave a stylesheet off
  }

  /* ---------- scripts: defer / delay ---------- */
  var held = [];

  function thirdParty(src) {
    try {
      var h = new URL(src, location.href).hostname;
      return !(h === location.hostname || /(^|\.)(shopify\.com|shopifycdn\.com|shopifycloud\.com)$/.test(h));
    } catch (e) { return false; }
  }

  function modeFor(s) {
    var src = s.getAttribute('src');
    if (!src || s.__ihs) return null;
    var t = (s.getAttribute('type') || '').toLowerCase();
    if (t && t !== 'text/javascript' && t !== 'application/javascript') return null; // modules, JSON, etc.
    if (matches(src, jsExclude)) return null;
    if (C.delayApps && matches(src, delayList)) return 'interaction';
    if (C.deferJs && thirdParty(src)) return 'load';
    return null;
  }

  function hold(s, mode) {
    s.__ihs = true;
    s.__orig = s.getAttribute('type');
    s.setAttribute('type', BLOCKED);
    s.addEventListener('beforescriptexecute', function stop(e) {   // Firefox
      if (s.getAttribute('type') === BLOCKED) e.preventDefault();
      s.removeEventListener('beforescriptexecute', stop);
    });
    held.push({ el: s, mode: mode });
  }

  function release(mode) {
    var rest = [];
    held.forEach(function (h) {
      if (mode && h.mode !== mode) { rest.push(h); return; }
      var old = h.el, fresh = document.createElement('script');
      for (var i = 0; i < old.attributes.length; i++) {
        var a = old.attributes[i];
        if (a.name !== 'type') fresh.setAttribute(a.name, a.value);
      }
      if (old.__orig) fresh.setAttribute('type', old.__orig);
      fresh.async = false; // keep the original order
      fresh.__ihs = true;
      if (old.parentNode) old.parentNode.replaceChild(fresh, old);
      else document.head.appendChild(fresh);
    });
    held = rest;
  }

  // Scripts that apps add from JavaScript (the common case) are caught here,
  // before they are attached, so they can be held reliably.
  ['appendChild', 'insertBefore'].forEach(function (name) {
    var orig = Node.prototype[name];
    Node.prototype[name] = function (node) {
      if (node && node.tagName === 'SCRIPT') {
        var m = modeFor(node);
        if (m) hold(node, m);
      }
      return orig.apply(this, arguments);
    };
  });

  var interacted = false;
  function onInteract() {
    if (interacted) return;
    interacted = true;
    ['scroll', 'click', 'keydown', 'touchstart', 'mousemove'].forEach(function (ev) {
      removeEventListener(ev, onInteract, true);
    });
    release('interaction');
  }
  if (C.delayApps) {
    ['scroll', 'click', 'keydown', 'touchstart', 'mousemove'].forEach(function (ev) {
      addEventListener(ev, onInteract, { capture: true, passive: true });
    });
    setTimeout(onInteract, Math.max(2, C.delayTimeout | 0 || 6) * 1000);
  }
  addEventListener('load', function () { setTimeout(function () { release('load'); }, 50); });

  /* ---------- watch the document as it is parsed ---------- */
  function handle(node) {
    if (node.nodeType !== 1) return;
    var tag = node.tagName;
    if (tag === 'IMG') processImage(node, false);
    else if (tag === 'LINK') processLink(node);
    else if (tag === 'SCRIPT') { var m = modeFor(node); if (m) hold(node, m); }
    else if (node.querySelectorAll) {
      var imgs = node.querySelectorAll('img');
      for (var i = 0; i < imgs.length; i++) processImage(imgs[i], false);
    }
  }
  new MutationObserver(function (records) {
    records.forEach(function (r) {
      for (var i = 0; i < r.addedNodes.length; i++) handle(r.addedNodes[i]);
    });
  }).observe(document.documentElement, { childList: true, subtree: true });

  function finalPass() {
    var imgs = document.getElementsByTagName('img');
    for (var i = 0; i < imgs.length; i++) processImage(imgs[i], true);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', finalPass);
  else finalPass();
})();
