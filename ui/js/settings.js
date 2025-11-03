/* =======================================================================
  CCRS Settings - Page Logic
   -----------------------------------------------------------------------
   Responsibilities:
   1) Left-nav smooth scrolling + scrollspy
   2) Load config from /settings/config
   3) Hydrate form fields with current values
   4) Track changes and enable/disable Save button
   5) Collect changes and POST to /settings/config
   6) Show restart requirement message after save
   ======================================================================= */

(function () {
  'use strict';

  // State management
  let originalConfig = null;
  let isDirty = false;

  // Element references
  const els = {
    // Event & Engine
    eventName: document.getElementById('eventName'),
    eventDate: document.getElementById('eventDate'),
    eventLocation: document.getElementById('eventLocation'),
    defaultMinLap: document.getElementById('defaultMinLap'),
    allowUnknownTags: document.getElementById('allowUnknownTags'),
    unknownTagName: document.getElementById('unknownTagName'),

    // Ingest & Timing
    debounceMs: document.getElementById('debounceMs'),
    diagEnabled: document.getElementById('diagEnabled'),
    diagBuffer: document.getElementById('diagBuffer'),
    beepMax: document.getElementById('beepMax'),

    // Hardware & Decoders (unified)
    decoderType: document.getElementById('decoderType'),
    serialPort: document.getElementById('serialPort'),
    serialBaud: document.getElementById('serialBaud'),
    decoderMinLap: document.getElementById('decoderMinLap'),
    ilapInit7Digit: document.getElementById('ilapInit7Digit'),
    ilapSpecific: document.getElementById('ilapSpecific'),

    // Track & Locations
    locationSF: document.getElementById('locationSF'),
    locationPitIn: document.getElementById('locationPitIn'),
    locationPitOut: document.getElementById('locationPitOut'),
    locationX1: document.getElementById('locationX1'),
    
    // Hardware bindings (4 bindings, each with 4 fields)
    binding0Computer: document.getElementById('binding0Computer'),
    binding0Decoder: document.getElementById('binding0Decoder'),
    binding0Port: document.getElementById('binding0Port'),
    binding0Location: document.getElementById('binding0Location'),
    binding1Computer: document.getElementById('binding1Computer'),
    binding1Decoder: document.getElementById('binding1Decoder'),
    binding1Port: document.getElementById('binding1Port'),
    binding1Location: document.getElementById('binding1Location'),
    binding2Computer: document.getElementById('binding2Computer'),
    binding2Decoder: document.getElementById('binding2Decoder'),
    binding2Port: document.getElementById('binding2Port'),
    binding2Location: document.getElementById('binding2Location'),
    binding3Computer: document.getElementById('binding3Computer'),
    binding3Decoder: document.getElementById('binding3Decoder'),
    binding3Port: document.getElementById('binding3Port'),
    binding3Location: document.getElementById('binding3Location'),

    // Flags & Safety
    flagBlocklist: document.getElementById('flagBlocklist'),
    flagGraceMs: document.getElementById('flagGraceMs'),
    missedMode: document.getElementById('missedMode'),
    missedWindowLaps: document.getElementById('missedWindowLaps'),
    missedSigmaK: document.getElementById('missedSigmaK'),
    missedMinGapMs: document.getElementById('missedMinGapMs'),
    missedMaxConsec: document.getElementById('missedMaxConsec'),
    missedMark: document.getElementById('missedMark'),

    // Qualifying
    brakeTestPolicy: document.getElementById('brakeTestPolicy'),

    // UI & Sounds
    uiTheme: document.getElementById('uiTheme'),
    showSimPill: document.getElementById('showSimPill'),
    soundVolumeMaster: document.getElementById('soundVolumeMaster'),
    soundVolumeHorns: document.getElementById('soundVolumeHorns'),
    soundVolumeBeeps: document.getElementById('soundVolumeBeeps'),

    // Action bar
    changeIndicator: document.getElementById('changeIndicator'),
    statusMessage: document.getElementById('statusMessage'),
    cancelBtn: document.getElementById('cancelBtn'),
    saveBtn: document.getElementById('saveBtn')
  };

  // ---------------------------------------------------------------------
  // Boot sequence
  // ---------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', async () => {
    await loadConfig();
    wireChangeDetection();
    wireActionButtons();
    wireDecoderTypeToggle();
  });

  // ---------------------------------------------------------------------
  // Load config from backend
  // ---------------------------------------------------------------------
  async function loadConfig() {
    try {
      const res = await fetch('/settings/config', { credentials: 'same-origin' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      originalConfig = await res.json();
      hydrate(originalConfig);
      setStatus('Settings loaded', false);
    } catch (err) {
      console.error('[settings] load failed:', err);
      setStatus('Failed to load settings', true);
    }
  }

  // ---------------------------------------------------------------------
  // Hydrate form fields from config
  // ---------------------------------------------------------------------
  function hydrate(cfg) {
    // Event & Engine
    setValue(els.eventName, cfg?.app?.engine?.event?.name);
    setValue(els.eventDate, cfg?.app?.engine?.event?.date);
    setValue(els.eventLocation, cfg?.app?.engine?.event?.location);
    setValue(els.defaultMinLap, cfg?.app?.engine?.default_min_lap_s);
    setValue(els.allowUnknownTags, String(cfg?.app?.engine?.unknown_tags?.allow ?? true));
    setValue(els.unknownTagName, cfg?.app?.engine?.unknown_tags?.auto_create_name);

    // Ingest & Timing
    setValue(els.debounceMs, cfg?.app?.engine?.ingest?.debounce_ms);
    setValue(els.diagEnabled, String(cfg?.app?.engine?.diagnostics?.enabled ?? true));
    setValue(els.diagBuffer, cfg?.app?.engine?.diagnostics?.buffer_size);
    setValue(els.beepMax, cfg?.app?.engine?.diagnostics?.beep?.max_per_sec);

    // Hardware & Decoders - determine active decoder from scanner.decoder config
    const activeDecoder = cfg?.scanner?.decoder || 'ilap_serial';
    setValue(els.decoderType, activeDecoder);
    
    // Load settings for the active decoder
    const decoderCfg = cfg?.app?.hardware?.decoders?.[activeDecoder] || {};
    setValue(els.serialPort, decoderCfg.port);
    setValue(els.serialBaud, decoderCfg.baudrate);
    setValue(els.decoderMinLap, decoderCfg.min_lap_s);
    
    // I-Lap specific
    if (activeDecoder === 'ilap_serial') {
      setValue(els.ilapInit7Digit, String(decoderCfg.init_7digit ?? true));
    }
    
    // Show/hide I-Lap specific settings
    updateDecoderSpecificUI();

    // Track & Locations - editable inputs
    const locations = cfg?.app?.track?.locations || {};
    setValue(els.locationSF, locations.SF);
    setValue(els.locationPitIn, locations.PIT_IN);
    setValue(els.locationPitOut, locations.PIT_OUT);
    setValue(els.locationX1, locations.X1);
    
    // Hardware bindings - populate up to 4 bindings
    const bindings = cfg?.track?.bindings || [];
    for (let i = 0; i < 4; i++) {
      const binding = bindings[i] || {};
      setValue(els[`binding${i}Computer`], binding.computer_id);
      setValue(els[`binding${i}Decoder`], binding.decoder_id);
      setValue(els[`binding${i}Port`], binding.port);
      setValue(els[`binding${i}Location`], binding.location_id);
    }

    // Flags & Safety
    const blocklist = cfg?.app?.race?.flags?.inference_blocklist || [];
    setValue(els.flagBlocklist, Array.isArray(blocklist) ? blocklist.join(',') : '');
    setValue(els.flagGraceMs, cfg?.app?.race?.flags?.post_green_grace_ms);

    // Missed-lap
    const missedEnabled = cfg?.app?.race?.missed_lap?.enabled ?? false;
    const missedApplyMode = cfg?.app?.race?.missed_lap?.apply_mode || 'propose';
    const missedMode = missedEnabled ? missedApplyMode : 'off';
    setValue(els.missedMode, missedMode);
    setValue(els.missedWindowLaps, cfg?.app?.race?.missed_lap?.window_laps);
    setValue(els.missedSigmaK, cfg?.app?.race?.missed_lap?.sigma_k);
    setValue(els.missedMinGapMs, cfg?.app?.race?.missed_lap?.min_gap_ms);
    setValue(els.missedMaxConsec, cfg?.app?.race?.missed_lap?.max_consecutive_inferred);
    setValue(els.missedMark, String(cfg?.app?.race?.missed_lap?.mark_inferred ?? true));

    // Qualifying
    setValue(els.brakeTestPolicy, cfg?.app?.engine?.qualifying?.brake_test_policy || 'demote');

    // UI & Sounds
    setValue(els.uiTheme, cfg?.app?.ui?.theme);
    setValue(els.showSimPill, String(cfg?.app?.ui?.show_sim_pill ?? true));
    setValue(els.soundVolumeMaster, cfg?.sounds?.volume?.master);
    setValue(els.soundVolumeHorns, cfg?.sounds?.volume?.horns);
    setValue(els.soundVolumeBeeps, cfg?.sounds?.volume?.beeps);

    // Reset dirty flag after hydration
    isDirty = false;
    updateDirtyUI();
  }

  function setValue(el, value) {
    if (!el) return;
    if (value !== undefined && value !== null) {
      el.value = value;
    }
  }

  // ---------------------------------------------------------------------
  // Decoder type toggle - show/hide decoder-specific settings
  // ---------------------------------------------------------------------
  function wireDecoderTypeToggle() {
    if (els.decoderType) {
      els.decoderType.addEventListener('change', updateDecoderSpecificUI);
    }
  }

  function updateDecoderSpecificUI() {
    const decoderType = els.decoderType?.value;
    
    // Show/hide I-Lap specific settings
    if (els.ilapSpecific) {
      els.ilapSpecific.style.display = decoderType === 'ilap_serial' ? 'block' : 'none';
    }
  }

  // ---------------------------------------------------------------------
  // Change detection
  // ---------------------------------------------------------------------
  function wireChangeDetection() {
    const inputs = [
      els.eventName, els.eventDate, els.eventLocation, els.defaultMinLap,
      els.allowUnknownTags, els.unknownTagName,
      els.debounceMs, els.diagEnabled, els.diagBuffer, els.beepMax,
      els.decoderType, els.serialPort, els.serialBaud, els.decoderMinLap, els.ilapInit7Digit,
      els.locationSF, els.locationPitIn, els.locationPitOut, els.locationX1,
      els.binding0Computer, els.binding0Decoder, els.binding0Port, els.binding0Location,
      els.binding1Computer, els.binding1Decoder, els.binding1Port, els.binding1Location,
      els.binding2Computer, els.binding2Decoder, els.binding2Port, els.binding2Location,
      els.binding3Computer, els.binding3Decoder, els.binding3Port, els.binding3Location,
      els.flagBlocklist, els.flagGraceMs,
      els.missedMode, els.missedWindowLaps, els.missedSigmaK,
      els.missedMinGapMs, els.missedMaxConsec, els.missedMark,
      els.brakeTestPolicy,
      els.uiTheme, els.showSimPill,
      els.soundVolumeMaster, els.soundVolumeHorns, els.soundVolumeBeeps
    ].filter(Boolean);

    inputs.forEach(el => {
      el.addEventListener('input', () => {
        isDirty = true;
        updateDirtyUI();
      });
    });
  }

  function updateDirtyUI() {
    if (els.changeIndicator) {
      els.changeIndicator.style.display = isDirty ? 'inline' : 'none';
    }
    if (els.saveBtn) {
      els.saveBtn.disabled = !isDirty;
    }
  }

  // ---------------------------------------------------------------------
  // Action buttons
  // ---------------------------------------------------------------------
  function wireActionButtons() {
    if (els.cancelBtn) {
      els.cancelBtn.addEventListener('click', () => {
        if (originalConfig) {
          hydrate(originalConfig);
          setStatus('Changes cancelled', false);
        }
      });
    }

    if (els.saveBtn) {
      els.saveBtn.addEventListener('click', async () => {
        await saveConfig();
      });
    }
  }

  // ---------------------------------------------------------------------
  // Collect changes and save
  // ---------------------------------------------------------------------
  async function saveConfig() {
    try {
      setStatus('Saving...', false);

      const patch = collectPatch();
      
      const res = await fetch('/settings/config', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(patch)
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || 'Save failed');
      }

      const data = await res.json();

      // Update originalConfig with saved values
      originalConfig = deepMerge(originalConfig || {}, patch);
      isDirty = false;
      updateDirtyUI();

      // Show popup alert
      alert('Settings saved successfully!\n\nRestart the application for changes to take effect.');
      setStatus('', false);
    } catch (err) {
      console.error('[settings] save failed:', err);
      setStatus('Save failed: ' + err.message, true);
    }
  }

  function collectPatch() {
    const patch = { app: {}, sounds: {} };

    // Event & Engine
    patch.app.engine = patch.app.engine || {};
    patch.app.engine.event = {
      name: els.eventName?.value || '',
      date: els.eventDate?.value || '',
      location: els.eventLocation?.value || ''
    };
    patch.app.engine.default_min_lap_s = numOrUndef(els.defaultMinLap?.value, 10);
    patch.app.engine.unknown_tags = {
      allow: els.allowUnknownTags?.value === 'true',
      auto_create_name: els.unknownTagName?.value || 'Unknown'
    };

    // Ingest & Timing
    patch.app.engine.ingest = {
      debounce_ms: numOrUndef(els.debounceMs?.value, 250)
    };
    patch.app.engine.diagnostics = {
      enabled: els.diagEnabled?.value === 'true',
      buffer_size: numOrUndef(els.diagBuffer?.value, 500),
      beep: {
        max_per_sec: numOrUndef(els.beepMax?.value, 5)
      }
    };

    // Hardware & Decoders - save to both scanner.decoder and app.hardware.decoders
    const decoderType = els.decoderType?.value || 'ilap_serial';
    
    // Update scanner.decoder to set active decoder
    patch.scanner = {
      decoder: decoderType
    };
    
    // Build the config for the selected decoder
    const decoderConfig = {
      port: els.serialPort?.value || 'COM3',
      baudrate: numOrUndef(els.serialBaud?.value, 9600),
      min_lap_s: numOrUndef(els.decoderMinLap?.value, 10)
    };
    
    // Add I-Lap specific settings
    if (decoderType === 'ilap_serial') {
      decoderConfig.init_7digit = els.ilapInit7Digit?.value === 'true';
    }
    
    // Save to app.hardware.decoders.[decoder_type]
    patch.app.hardware = {
      decoders: {
        [decoderType]: decoderConfig
      }
    };

    // Track & Locations
    patch.app.track = {
      locations: {
        SF: els.locationSF?.value || 'Start/Finish',
        PIT_IN: els.locationPitIn?.value || 'Pit In',
        PIT_OUT: els.locationPitOut?.value || 'Pit Out',
        X1: els.locationX1?.value || 'Crossing X'
      }
    };

    // Hardware bindings - collect up to 4 bindings, skip empty ones
    const bindings = [];
    for (let i = 0; i < 4; i++) {
      const computer = els[`binding${i}Computer`]?.value?.trim();
      const decoder = els[`binding${i}Decoder`]?.value?.trim();
      const port = els[`binding${i}Port`]?.value?.trim();
      const location = els[`binding${i}Location`]?.value?.trim();
      
      // Only include binding if at least computer_id and location_id are set
      if (computer && location) {
        bindings.push({
          computer_id: computer,
          decoder_id: decoder || '',
          port: port || '',
          location_id: location
        });
      }
    }
    
    // Add bindings at top level (not under app)
    patch.track = {
      bindings: bindings
    };

    // Flags & Safety
    patch.app.race = patch.app.race || {};
    patch.app.race.flags = {
      inference_blocklist: (els.flagBlocklist?.value || '')
        .split(',').map(s => s.trim()).filter(Boolean),
      post_green_grace_ms: numOrUndef(els.flagGraceMs?.value, 3000)
    };

    // Missed-lap
    const missedMode = els.missedMode?.value || 'off';
    patch.app.race.missed_lap = {
      enabled: missedMode !== 'off',
      apply_mode: missedMode === 'off' ? 'propose' : missedMode,
      window_laps: numOrUndef(els.missedWindowLaps?.value, 5),
      sigma_k: numOrUndef(els.missedSigmaK?.value, 2.0),
      min_gap_ms: numOrUndef(els.missedMinGapMs?.value, 8000),
      max_consecutive_inferred: numOrUndef(els.missedMaxConsec?.value, 1),
      mark_inferred: els.missedMark?.value === 'true'
    };

    // Qualifying
    patch.app.engine.qualifying = {
      brake_test_policy: els.brakeTestPolicy?.value || 'demote'
    };

    // UI & Sounds
    patch.app.ui = {
      theme: els.uiTheme?.value || 'default-dark',
      show_sim_pill: els.showSimPill?.value === 'true'
    };
    patch.sounds.volume = {
      master: numOrUndef(els.soundVolumeMaster?.value, 1.0),
      horns: numOrUndef(els.soundVolumeHorns?.value, 1.0),
      beeps: numOrUndef(els.soundVolumeBeeps?.value, 1.0)
    };

    return patch;
  }

  function numOrUndef(v, dflt) {
    const n = Number(v);
    return Number.isFinite(n) ? n : dflt;
  }

  function deepMerge(base, overlay) {
    const result = { ...base };
    for (const key in overlay) {
      if (overlay[key] && typeof overlay[key] === 'object' && !Array.isArray(overlay[key])) {
        result[key] = deepMerge(result[key] || {}, overlay[key]);
      } else {
        result[key] = overlay[key];
      }
    }
    return result;
  }

  // ---------------------------------------------------------------------
  // Status message helper
  // ---------------------------------------------------------------------
  function setStatus(msg, isError) {
    if (els.statusMessage) {
      els.statusMessage.textContent = msg;
      els.statusMessage.style.color = isError ? '#ff5555' : '#b3c2cc';
    }
  }
})();
