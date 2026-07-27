"""The client runtime — the part that makes a built site feel live.

Two scripts, split by when they have to run. `BOOT` is a few hundred bytes
inlined in `<head>`: it drops the `no-js` class and applies the stored theme
*before first paint*, which is the only way to avoid a white flash on a dark
page. `runtime` is everything else, written once to `assets/docs.js` and
loaded with `defer` so it is cached across the whole site instead of paid for
per page.

Everything here is an enhancement over markup that already works. With
JavaScript off you get every page, every link, every nav section (`<details>`),
every content tab (a CSS radio group), and the whole table of contents; you lose
the search palette, instant navigation, the copy buttons, and the scroll-spy.
Nothing that carries content depends on this file.

No framework, no build step, no dependency — ES5-compatible syntax with modern
APIs used only behind feature checks, so the file is served exactly as written.
"""

from __future__ import annotations

__all__ = ["BOOT", "runtime"]

#: Inlined in <head>, before the first paint. Two jobs and nothing else.
BOOT = (
    "(function(){var r=document.documentElement;r.classList.remove('no-js');"
    "try{var t=localStorage.getItem('wreath-docs-theme');"
    "if(t==='dark'||t==='light')r.setAttribute('data-theme',t);}catch(e){}})()"
)


_RUNTIME = r"""
/* wreath docs runtime — no dependencies, no build step. */
(function () {
  'use strict';

  var root = document.documentElement;
  var THEME_KEY = 'wreath-docs-theme';
  var SIDE_KEY = 'wreath-docs-side';
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)');

  function $(sel, scope) { return (scope || document).querySelector(sel); }
  function $$(sel, scope) {
    return Array.prototype.slice.call((scope || document).querySelectorAll(sel));
  }
  function esc(text) {
    var node = document.createElement('div');
    node.textContent = text;
    return node.innerHTML;
  }
  function store(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }
  function read(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

  /* --- theme: system -> light -> dark -------------------------------------- */
  /* Three states, not two. A two-state toggle silently discards "follow the OS"
     the first time it is pressed, and there is then no way back to it. */

  var themeBtn = $('#theme-toggle');
  var THEMES = ['system', 'light', 'dark'];
  var THEME_LABEL = {
    system: 'Theme: match system. Switch to light.',
    light: 'Theme: light. Switch to dark.',
    dark: 'Theme: dark. Match system instead.'
  };

  function applyTheme(mode) {
    if (mode === 'system') { root.removeAttribute('data-theme'); }
    else { root.setAttribute('data-theme', mode); }
    if (!themeBtn) { return; }
    themeBtn.setAttribute('data-mode', mode);
    themeBtn.setAttribute('aria-label', THEME_LABEL[mode]);
    themeBtn.title = THEME_LABEL[mode];
  }

  if (themeBtn) {
    var stored = read(THEME_KEY);
    applyTheme(stored === 'light' || stored === 'dark' ? stored : 'system');
    themeBtn.addEventListener('click', function () {
      var next = THEMES[(THEMES.indexOf(themeBtn.getAttribute('data-mode')) + 1) % 3];
      applyTheme(next);
      if (next === 'system') { try { localStorage.removeItem(THEME_KEY); } catch (e) {} }
      else { store(THEME_KEY, next); }
    });
  }

  /* --- mobile drawer -------------------------------------------------------- */

  var menuBtn = $('#menu-toggle');
  var scrim = $('#nav-scrim');

  function closeNav() {
    document.body.classList.remove('nav-open');
    if (menuBtn) { menuBtn.setAttribute('aria-expanded', 'false'); }
  }
  if (menuBtn) {
    menuBtn.addEventListener('click', function () {
      var open = document.body.classList.toggle('nav-open');
      menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }
  if (scrim) { scrim.addEventListener('click', closeNav); }

  /* --- copy buttons --------------------------------------------------------- */
  /* Added by script because a copy button with no clipboard is a dead control. */

  var COPY_ICON = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor"' +
    ' stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"' +
    ' aria-hidden="true"><rect x="7" y="7" width="9.5" height="9.5" rx="2"/>' +
    '<path d="M13 4.5H5.5a2 2 0 0 0-2 2V14"/></svg>';
  var DONE_ICON = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor"' +
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"' +
    ' aria-hidden="true"><path d="M4.5 10.5 8 14l7.5-8"/></svg>';

  function addCopyButtons(scope) {
    if (!navigator.clipboard) { return; }
    $$('.code', scope).forEach(function (block) {
      if ($('.copy-btn', block)) { return; }
      var button = document.createElement('button');
      button.className = 'copy-btn';
      button.type = 'button';
      button.innerHTML = COPY_ICON;
      button.setAttribute('aria-label', 'Copy code to clipboard');
      button.addEventListener('click', function () {
        var code = $('code', block);
        if (!code) { return; }
        navigator.clipboard.writeText(code.textContent).then(function () {
          button.innerHTML = DONE_ICON;
          button.classList.add('copied');
          button.setAttribute('aria-label', 'Copied');
          setTimeout(function () {
            button.innerHTML = COPY_ICON;
            button.classList.remove('copied');
            button.setAttribute('aria-label', 'Copy code to clipboard');
          }, 1400);
        });
      });
      var head = $('.code-head', block);
      if (head) { head.appendChild(button); } else { block.appendChild(button); }
    });
  }

  /* --- scroll-spy ----------------------------------------------------------- */
  /* Deliberately not IntersectionObserver. An observer fires on *any* heading
     entering the viewport, so a tall section between two short ones kept losing
     the highlight to whatever crossed the margin last. "The last heading above
     the reading line" is the thing a reader means, and it is one comparison. */

  var spyLinks = {};
  var spyHeads = [];
  var spyTicking = false;
  var spyCurrent = null;

  function collectSpy() {
    spyLinks = {};
    $$('.toc-rail a[href^="#"]').forEach(function (link) {
      spyLinks[decodeURIComponent(link.getAttribute('href').slice(1))] = link;
    });
    spyHeads = $$('main h2[id], main h3[id]').filter(function (h) {
      return spyLinks[h.id];
    });
    spyCurrent = null;
    updateSpy();
  }

  function updateSpy() {
    if (!spyHeads.length) { return; }
    var line = window.scrollY + (root.clientHeight * 0.25);
    var found = spyHeads[0];
    for (var i = 0; i < spyHeads.length; i++) {
      if (spyHeads[i].getBoundingClientRect().top + window.scrollY <= line) {
        found = spyHeads[i];
      } else { break; }
    }
    // At the very bottom nothing further can scroll into view, so the last
    // heading owns the rest of the page however short its section is.
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 4) {
      found = spyHeads[spyHeads.length - 1];
    }
    if (found === spyCurrent) { return; }
    spyCurrent = found;
    for (var id in spyLinks) { spyLinks[id].classList.remove('toc-active'); }
    var link = spyLinks[found.id];
    if (link) {
      link.classList.add('toc-active');
      var rail = link.parentNode;
      if (rail && rail.scrollHeight > rail.clientHeight) {
        var top = link.offsetTop - rail.clientHeight / 2;
        if (rail.scrollTo) { rail.scrollTop = top; }
      }
    }
  }

  /* --- back to top ---------------------------------------------------------- */

  var toTop = $('#to-top');
  if (toTop) {
    toTop.addEventListener('click', function () {
      window.scrollTo({ top: 0, behavior: reduced.matches ? 'auto' : 'smooth' });
      var skip = $('.skip');
      if (skip) { skip.focus({ preventScroll: true }); skip.blur(); }
    });
  }

  function onScroll() {
    if (spyTicking) { return; }
    spyTicking = true;
    window.requestAnimationFrame(function () {
      spyTicking = false;
      updateSpy();
      if (toTop) { toTop.classList.toggle('on', window.scrollY > root.clientHeight); }
    });
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll, { passive: true });

  /* --- search --------------------------------------------------------------- */

  var dialog = $('#search-dialog');
  var input = $('#docs-search');
  var results = $('#docs-results');
  var openBtn = $('#search-open');
  var index = null;
  var loading = null;
  var lastQuery = '';

  if (openBtn && /Mac|iPhone|iPad/.test(navigator.platform || '')) {
    var chip = $('kbd', openBtn);
    if (chip) { chip.textContent = '⌘K'; }
  }

  function indexRoot() { return (input && input.getAttribute('data-root')) || ''; }

  function loadIndex() {
    if (index) { return Promise.resolve(index); }
    if (loading) { return loading; }
    loading = fetch(indexRoot() + 'assets/search-index.json')
      .then(function (response) { return response.json(); })
      .then(function (data) { index = data; return data; })
      .catch(function () { return null; });
    return loading;
  }

  /* Scoring. A heading match is what people are almost always looking for in
     docs, so it outranks a body hit by an order of magnitude; a page title
     matching outranks a body hit in a differently-titled page. Every term has
     to appear somewhere in the section or the section is not a result at all,
     which is what makes two-word queries useful rather than noisier. */
  function score(section, page, terms) {
    var heading = section.h.toLowerCase();
    var title = page.t.toLowerCase();
    var body = section.x.toLowerCase();
    var total = 0;
    for (var i = 0; i < terms.length; i++) {
      var term = terms[i];
      var inHeading = heading.indexOf(term);
      var inTitle = title.indexOf(term);
      var inBody = body.indexOf(term);
      if (inHeading < 0 && inTitle < 0 && inBody < 0) { return 0; }
      if (heading === term) { total += 200; }
      else if (inHeading === 0) { total += 120; }
      else if (inHeading > 0) { total += 60; }
      if (title === term) { total += 90; }
      else if (inTitle === 0) { total += 45; }
      else if (inTitle > 0) { total += 20; }
      if (inBody >= 0) { total += 8; }
    }
    // A shorter heading containing the query is a tighter match than a long one.
    return total + Math.max(0, 24 - heading.length / 4);
  }

  function highlight(text, terms) {
    var out = esc(text);
    for (var i = 0; i < terms.length; i++) {
      var safe = terms[i].replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      out = out.replace(new RegExp('(' + safe + ')', 'gi'), '<mark>$1</mark>');
    }
    return out;
  }

  function snippet(text, terms) {
    var lower = text.toLowerCase();
    var at = -1;
    for (var i = 0; i < terms.length && at < 0; i++) { at = lower.indexOf(terms[i]); }
    if (at < 0) { return text.slice(0, 120); }
    var from = Math.max(0, at - 48);
    return (from > 0 ? '…' : '') + text.slice(from, from + 140);
  }

  function render(query) {
    var terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length || !index) { results.innerHTML = ''; return; }
    var root_ = indexRoot();
    var hits = [];
    for (var i = 0; i < index.s.length; i++) {
      var section = index.s[i];
      var page = index.p[section.p];
      var points = score(section, page, terms);
      if (points > 0) { hits.push({ s: section, p: page, n: points }); }
    }
    hits.sort(function (a, b) { return b.n - a.n; });
    hits = hits.slice(0, 24);
    if (!hits.length) {
      results.innerHTML = '<div class="palette-empty">No results for &ldquo;' +
        esc(query) + '&rdquo;</div>';
      return;
    }
    var html = '';
    var group = null;
    for (var j = 0; j < hits.length; j++) {
      var hit = hits[j];
      if (hit.p.t !== group) {
        group = hit.p.t;
        html += '<div class="group">' + esc(group) + '</div>';
      }
      var href = root_ + hit.p.u + (hit.s.a ? '#' + hit.s.a : '');
      html += '<a href="' + esc(href) + '"><span class="r-title">' +
        highlight(hit.s.h, terms) + '</span><span class="r-ctx">' +
        highlight(snippet(hit.s.x, terms), terms) + '</span></a>';
    }
    results.innerHTML = html;
  }

  function openSearch() {
    if (!dialog) { return; }
    if (!dialog.open) {
      if (dialog.showModal) { dialog.showModal(); } else { dialog.setAttribute('open', ''); }
    }
    input.focus();
    input.select();
    loadIndex().then(function () { if (input.value) { render(input.value); } });
  }

  if (openBtn) { openBtn.addEventListener('click', openSearch); }

  if (input) {
    input.addEventListener('input', function () {
      var query = input.value.trim();
      if (query === lastQuery) { return; }
      lastQuery = query;
      if (!query) { results.innerHTML = ''; return; }
      loadIndex().then(function () { render(query); });
    });
    // Arrow keys move real focus between the result links rather than faking a
    // listbox with ARIA. Focus is the thing screen readers and browsers already
    // agree on, and Enter then activates the link with no handler at all.
    input.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowDown') {
        var first = $('a', results);
        if (first) { event.preventDefault(); first.focus(); }
      }
    });
    results.addEventListener('keydown', function (event) {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') { return; }
      var links = $$('a', results);
      var at = links.indexOf(document.activeElement);
      event.preventDefault();
      if (event.key === 'ArrowUp' && at <= 0) { input.focus(); return; }
      var next = links[at + (event.key === 'ArrowDown' ? 1 : -1)];
      if (next) { next.focus(); }
    });
  }

  document.addEventListener('keydown', function (event) {
    if (!dialog) { return; }
    var open = dialog.open;
    if ((event.key === 'k' || event.key === 'K') && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      if (open) { dialog.close(); } else { openSearch(); }
      return;
    }
    if (event.key === '/' && !open && !isTyping(event.target)) {
      event.preventDefault();
      openSearch();
    }
  });

  function isTyping(node) {
    if (!node || !node.tagName) { return false; }
    var tag = node.tagName.toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || node.isContentEditable;
  }

  /* --- instant navigation --------------------------------------------------- */
  /* Fetch the next page, swap the parts that changed, and leave the sidebar
     alone. The win is not the milliseconds; it is that the nav does not blink
     and lose its scroll position on every click through a 129-page tree. */

  var prefetched = {};
  var prefetchCount = 0;
  var swapping = false;
  var canSwap = (location.protocol === 'http:' || location.protocol === 'https:') &&
    window.history && window.history.pushState && window.fetch && window.DOMParser;

  function internal(link) {
    if (!link || link.target || link.hasAttribute('download')) { return null; }
    var href = link.getAttribute('href');
    if (!href || href.charAt(0) === '#') { return null; }
    var url;
    try { url = new URL(link.href, location.href); } catch (e) { return null; }
    if (url.origin !== location.origin) { return null; }
    if (!/\.html$|\/$/.test(url.pathname)) { return null; }
    return url;
  }

  function prefetch(url) {
    var key = url.origin + url.pathname;
    if (prefetched[key] || prefetchCount > 40) { return; }
    prefetchCount++;
    prefetched[key] = fetch(url.pathname, { credentials: 'same-origin' })
      .then(function (response) { return response.ok ? response.text() : null; })
      .catch(function () { return null; });
  }

  function fetchPage(url) {
    var key = url.origin + url.pathname;
    if (prefetched[key]) { return prefetched[key]; }
    prefetch(url);
    return prefetched[key];
  }

  function swapIn(html, url, push) {
    var next = new DOMParser().parseFromString(html, 'text/html');
    var main = $('main');
    var incoming = $('main', next);
    if (!main || !incoming) { return false; }

    var side = $('nav.side');
    var offset = side ? side.scrollTop : 0;

    if (push) { history.pushState({ y: 0 }, '', url.href); }
    document.title = next.title;
    main.innerHTML = incoming.innerHTML;
    replace('nav.side', next);
    replace('aside.toc', next);
    replace('nav.tabs', next);
    var nextInput = $('#docs-search', next);
    if (input && nextInput) {
      input.setAttribute('data-root', nextInput.getAttribute('data-root') || '');
    }
    var newSide = $('nav.side');
    if (newSide) { newSide.scrollTop = offset; }
    enhance();
    return true;
  }

  function replace(selector, next) {
    var current = $(selector);
    var incoming = $(selector, next);
    if (current && incoming) { current.replaceWith(incoming); }
    else if (current && !incoming) { current.remove(); }
  }

  function navigate(url, push) {
    if (swapping) { return; }
    swapping = true;
    document.body.setAttribute('data-loading', '');
    fetchPage(url).then(function (html) {
      swapping = false;
      document.body.removeAttribute('data-loading');
      if (!html) { location.href = url.href; return; }
      var run = function () { swapIn(html, url, push); };
      if (document.startViewTransition && !reduced.matches) {
        document.startViewTransition(run);
      } else { run(); }
      // The view transition captures the old frame first, so scrolling has to
      // wait for the swap or the animation starts from the wrong place.
      setTimeout(function () {
        if (url.hash) {
          var target = document.getElementById(decodeURIComponent(url.hash.slice(1)));
          if (target) { target.scrollIntoView(); return; }
        }
        window.scrollTo(0, 0);
      }, 0);
    }, function () {
      swapping = false;
      location.href = url.href;
    });
  }

  if (canSwap) {
    document.addEventListener('mouseover', function (event) {
      var link = event.target.closest && event.target.closest('a[href]');
      var url = internal(link);
      if (url) { prefetch(url); }
    }, { passive: true });

    document.addEventListener('click', function (event) {
      if (event.defaultPrevented || event.button !== 0) { return; }
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) { return; }
      var link = event.target.closest && event.target.closest('a[href]');
      var url = internal(link);
      if (!url) { return; }
      if (url.pathname === location.pathname) { return; }   // same page, let #anchors work
      event.preventDefault();
      closeNav();
      if (dialog && dialog.open) { dialog.close(); }
      navigate(url, true);
    });

    window.addEventListener('popstate', function () {
      var url = new URL(location.href);
      fetchPage(url).then(function (html) {
        if (html) { swapIn(html, url, false); } else { location.reload(); }
      });
    });
  }

  /* --- sidebar scroll memory ------------------------------------------------ */
  /* Survives a full page load too, so a hard refresh does not throw away where
     you were in a tree that is taller than the window. */

  function rememberSide() {
    var side = $('nav.side');
    if (side) { try { sessionStorage.setItem(SIDE_KEY, String(side.scrollTop)); } catch (e) {} }
  }
  window.addEventListener('beforeunload', rememberSide);
  window.addEventListener('pagehide', rememberSide);

  (function restoreSide() {
    var side = $('nav.side');
    if (!side) { return; }
    var saved = null;
    try { saved = sessionStorage.getItem(SIDE_KEY); } catch (e) {}
    if (saved === null) { return; }
    side.scrollTop = Number(saved) || 0;
    // If the active page is off-screen the remembered offset was for a
    // different section; centring the current page is the better answer.
    var active = $('.nav-page.active', side);
    if (active) {
      var box = active.getBoundingClientRect();
      var frame = side.getBoundingClientRect();
      if (box.top < frame.top || box.bottom > frame.bottom) {
        side.scrollTop = active.offsetTop - side.clientHeight / 2;
      }
    }
  })();

  /* --- run ------------------------------------------------------------------ */

  function enhance() {
    addCopyButtons(document);
    collectSpy();
    onScroll();
  }
  enhance();
})();
"""


def runtime() -> str:
    """The full client runtime, written once to `assets/docs.js`."""
    return _RUNTIME.strip() + "\n"
