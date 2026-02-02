/* ==========================================================================
  ChronoCore Race Software (CCRS) - Base Browser Helpers
   --------------------------------------------------------------------------
   PURPOSE
   - Provide a tiny, well-documented helper layer that ALL Operator UI pages
     can depend on, without pulling in frameworks.
   - Centralize common utilities: DOM helpers, JSON fetch/post, pollers,
     footer net-status indicator, and a few UI-wide constants.

   EXPORTS
   - window.CCRS = {
       $,
       $$,
       apiUrl,      // NEW: stable API URL builder (apiBase + path)
       fetchJSON,
       postJSON,
       makePoller,
       setNetStatus,
       CONFIG
     }

   DESIGN NOTES
   - NO legacy shims. This file is CCRS-only by design.
   - Keep this file small, dependency-free, and boring. Stability wins here.
   - Keep functions side-effect-free unless obviously UI-related (setNetStatus).
   - Multi-line, verbose comments are intentional - they document our intent.
   ========================================================================== */
(function () {
  'use strict';

  /* ------------------------------------------------------------------------
     DOM HELPERS
     ------------------------------------------------------------------------
     $  -> First matching element (querySelector)
     $$ -> All matching elements as a real Array (querySelectorAll -> Array)
     These helpers reduce repetitive boilerplate in the UI code. Keep them tiny.
     ------------------------------------------------------------------------ */
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  /* ------------------------------------------------------------------------
     FULLSCREEN KEYBOARD CONTROLS
     ------------------------------------------------------------------------
     F11  - Toggle fullscreen mode (if pywebview API is available)
     ESC  - Exit fullscreen mode (if pywebview API is available)
     ------------------------------------------------------------------------ */
  let isFullscreen = false;

  document.addEventListener('keydown', (e) => {
    // Check if pywebview API is available (only in desktop mode)
    if (!window.pywebview || !window.pywebview.api) return;

    // F11 key - toggle fullscreen
    if (e.key === 'F11') {
      e.preventDefault();
      isFullscreen = !isFullscreen;
      window.pywebview.api.toggle_fullscreen().catch(err => {
        console.error('Fullscreen toggle failed:', err);
      });
      return;
    }

    // ESC key - exit fullscreen (only if currently fullscreen)
    if (e.key === 'Escape' && isFullscreen) {
      e.preventDefault();
      isFullscreen = false;
      window.pywebview.api.exit_fullscreen().catch(err => {
        console.error('Exit fullscreen failed:', err);
      });
      return;
    }
  });

  /* ------------------------------------------------------------------------
     API URL BUILDER (NEW)
     ------------------------------------------------------------------------
     apiUrl(path)
       - Stable URL builder used across all Operator UI pages.
       - If CCRS.apiBase is set (ex: "http://127.0.0.1:8080"), apiUrl()
         returns apiBase + path.
       - If CCRS.apiBase is empty/missing, apiUrl() returns the path as-is.
       - Ensures a missing apiUrl never breaks pages (results.html error).
     ------------------------------------------------------------------------ */
  function apiUrl(path) {
    const p = String(path || '');
    const base = (typeof window.CCRS?.apiBase === 'string' && window.CCRS.apiBase)
      ? window.CCRS.apiBase
      : '';
    return base + p;
  }

  /* ------------------------------------------------------------------------
     NETWORK HELPERS
     ------------------------------------------------------------------------
     fetchJSON(url, init)
       - GET/POST/etc returning parsed JSON
       - Throws on non-2xx with the response text (when available) baked into
         the Error message for easier debugging in the UI console.

     postJSON(url, body, init)
       - POST helper that sets Content-Type and JSON-stringifies the body.
       - Returns the raw Response so callers can choose .json() or .text().
     ------------------------------------------------------------------------ */
  async function fetchJSON(url, init = {}) {
    const headers = Object.assign(
      { 'Accept': 'application/json' },
      init.headers || {}
    );

    const res = await fetch(
      url,
      Object.assign({}, init, { headers })
    );

    if (!res.ok) {
      // Prefer response text for human-friendly error messages.
      const text = await res.text().catch(() => '');
      const err = new Error(text || `HTTP ${res.status} for ${url}`);
      // Attach the Response object for optional inspection by callers.
      err.response = res;
      throw err;
    }

    // We expect JSON here; callers depending on text should not use fetchJSON.
    return res.json();
  }

  function postJSON(url, body, init = {}) {
    const headers = Object.assign(
      { 'Content-Type': 'application/json' },
      init.headers || {}
    );

    return fetch(
      url,
      Object.assign(
        { method: 'POST' },
        init,
        { headers, body: JSON.stringify(body) }
      )
    );
  }

  /* ------------------------------------------------------------------------
     POLLER
     ------------------------------------------------------------------------
     makePoller(fn, intervalMs, onError)
       - Repeatedly executes async function `fn` every `intervalMs` milliseconds.
       - If `fn` throws, the error is routed to `onError` (if provided), then
         polling continues after the same delay. This keeps the UI resilient
         against transient network errors or backend restarts.
     USAGE
       const poll = makePoller(async () => { ... }, 1000, console.error);
       poll.start();
       // later: poll.stop();
     ------------------------------------------------------------------------ */
  function makePoller(fn, intervalMs = 2000, onError) {
    let timerId = null;
    let active = false;

    async function tick() {
      if (!active) return;
      try {
        await fn();
      } catch (err) {
        if (onError) onError(err);
      } finally {
        if (active) timerId = setTimeout(tick, intervalMs);
      }
    }

    return {
      start() {
        if (active) return;
        active = true;
        tick();
      },
      stop() {
        active = false;
        if (timerId) {
          clearTimeout(timerId);
          timerId = null;
        }
      }
    };
  }

  /* ------------------------------------------------------------------------
     FOOTER NETWORK STATUS INDICATOR
     ------------------------------------------------------------------------
     setNetStatus(ok, message)
       - Updates the small status dot and optional message in the app footer.
       - Requires two elements to exist in the DOM:
           #netDot  -> the colored circular indicator
           #netMsg  -> a text span for short status messages
       - Colors are driven by CSS custom properties defined in base.css:
           --ok    -> success color (e.g., green)
           --error -> error color  (e.g., red)
     ------------------------------------------------------------------------ */
  function setNetStatus(ok, message) {
    const dot = document.getElementById('netDot');
    const msg = document.getElementById('netMsg');

    if (dot) {
      dot.style.background = ok ? 'var(--ok)' : 'var(--error)';
      dot.style.boxShadow = ok
        ? '0 0 0 2px #0a1a24'
        : '0 0 0 2px #2a1010';
    }

    if (msg && typeof message === 'string') {
      msg.textContent = message;
    }
  }

  /* ------------------------------------------------------------------------
     UI-WIDE CONFIG DEFAULTS
     ------------------------------------------------------------------------ */
  const CONFIG = {
    SSE_URL: '/sensors/stream',
    POLL_URL: '/sensors/peek',
    POLL_INTERVAL: 200,
    MIN_TAG_LEN: 7
  };

  /* ------------------------------------------------------------------------
     EXPORT PUBLIC API
     ------------------------------------------------------------------------ */
  window.CCRS = Object.assign(
    window.CCRS || {},
    {
      $,
      $$,
      apiUrl,      // NEW
      fetchJSON,
      postJSON,
      makePoller,
      setNetStatus,
      CONFIG
    }
  );
})();

// Theme (light/dark) toggle
(function () {
  const STORAGE_KEY = 'ccrs.theme';
  const btn = document.getElementById('themeToggle');
  if (!btn) return;

  const root = document.documentElement;

  function applyTheme(theme) {
    root.setAttribute('data-bs-theme', theme);
    btn.textContent = theme === 'dark' ? '🌙' : '☀️';
    btn.setAttribute(
      'aria-label',
      theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'
    );
  }

  function getInitialTheme() {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'light' || stored === 'dark') return stored;
    return window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light';
  }

  let theme = getInitialTheme();
  applyTheme(theme);

  btn.addEventListener('click', () => {
    theme = theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem(STORAGE_KEY, theme);
    applyTheme(theme);
  });
})();

// Navbar active link indicator
(function () {
  function normalize(path) {
    // strip query, hash, trailing slash
    return path.replace(/[?#].*$/, '').replace(/\/$/, '');
  }

  const current = normalize(window.location.pathname);

  document.querySelectorAll('.navbar .nav-link[href]').forEach(link => {
    const href = normalize(link.getAttribute('href'));
    if (!href) return;

    const isActive =
      current === href ||
      (href.endsWith('/index.html') && current.endsWith('/operator'));

    if (isActive) {
      link.classList.add('active');
      link.setAttribute('aria-current', 'page');
    } else {
      link.classList.remove('active');
      link.removeAttribute('aria-current');
    }
  });
})();
