/* =====================================================================
   settings-nav.js
   ---------------------------------------------------------------------
   Shared helper for Settings sub-pages. It marks the current page's
   left-nav link as active, and fills the engine label if base.js exposes
   a helper. Keep this file tiny and dependency-free.
   ===================================================================== */

(() => {
  'use strict';

  // Mark current nav link active using the data-active attribute
  const active = document.querySelector('.sideNav .navLink[data-active]');
  if (active) {
    active.classList.add('active');
  }

  // Optional: display the effective engine label if base.js provides it
  const engineLabel = document.getElementById('engineLabel');
  if (engineLabel && window.CCRS && typeof window.CCRS.effectiveEngineLabel === 'function') {
    engineLabel.textContent = 'Engine: ' + window.CCRS.effectiveEngineLabel();
  }
})();