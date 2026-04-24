/* Round-61 pt.8 Option B — dashboard render core.
 *
 * Pure helpers extracted from templates/dashboard.html's inline <script>.
 * Loaded via <script src="/static/dashboard_render_core.js"></script>
 * BEFORE the inline script, so these functions are already attached to
 * `window` (as globals) by the time inline code references them.
 *
 * Why extract?
 *   - The 7000-line inline script is invisible to unit tests except via a
 *     jsdom + vm.runInThisContext shim (tests/js/loadDashboardJs.js).
 *     That's slow + fragile.
 *   - Pure helpers (no fetch, no module-local `let`) can live in a
 *     separate file AND be tested by plain import.
 *   - Matches the pt.7 Python pattern: screener_core.py + scorecard_core.py
 *     extracted pure math from update_dashboard.py + update_scorecard.py.
 *
 * Contract:
 *   - Every function in this file MUST be pure (given `document` for the
 *     ones that use DOM APIs like createElement / DOMParser).
 *   - Every function attaches to `window` / `globalThis` for backward
 *     compat with the 9600-line inline script that was already calling
 *     them as free globals.
 *   - NO module-local mutable state (that would re-introduce the
 *     "can't-inject-from-tests" problem Option A tried to solve).
 */
(function () {
  'use strict';

  /* ====== XSS escape helpers ====== */
  function esc(s) {
    if (s == null) return '';
    var str = String(s);
    var d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
      .replace(/`/g, '&#96;');
  }

  function jsStr(s) {
    if (s == null) return '';
    return String(s)
      .replace(/\\/g, '\\\\')
      .replace(/'/g, "\\u0027")
      .replace(/"/g, "\\u0022")
      .replace(/</g, "\\u003c")
      .replace(/>/g, "\\u003e")
      .replace(/&/g, "\\u0026")
      .replace(/\n/g, "\\n")
      .replace(/\r/g, "\\r");
  }

  /* ====== Formatting helpers ====== */
  function fmtMoney(n) {
    var x = Number(n);
    if (!isFinite(x)) x = 0;
    return '$' + x.toLocaleString(undefined, {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }
  function fmtPct(n) {
    var x = Number(n);
    if (!isFinite(x)) x = 0;
    return (x >= 0 ? '+' : '') + x.toFixed(1) + '%';
  }
  function pnlClass(n) {
    var v = parseFloat(n);
    if (!isFinite(v) || v === 0) return 'neutral';
    return v > 0 ? 'positive' : 'negative';
  }

  function fmtUpdatedET(ts) {
    if (!ts) return 'N/A';
    try {
      var s = String(ts);
      if (s.indexOf(' ET') !== -1) {
        var m = s.match(/\d{1,2}:\d{2}:\d{2}\s*(AM|PM)?\s*ET/i);
        if (m) return m[0].replace(/\s+/g, ' ');
      }
      var iso = s.replace(' UTC', 'Z').replace(' ', 'T');
      var d = new Date(iso);
      if (isNaN(d.getTime())) return 'N/A';
      return d.toLocaleTimeString('en-US', {
        hour: 'numeric', minute: '2-digit', second: '2-digit',
        hour12: true, timeZone: 'America/New_York'
      }) + ' ET';
    } catch (e) { return 'N/A'; }
  }

  function fmtAuditTime(iso) {
    if (!iso) return '—';
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleString('en-US', {
        month: 'short', day: 'numeric',
        hour: 'numeric', minute: '2-digit', second: '2-digit',
        hour12: true, timeZone: 'America/New_York'
      }) + ' ET';
    } catch (e) { return iso; }
  }

  function fmtRelative(isoOrTs) {
    if (!isoOrTs) return 'never';
    var when;
    if (typeof isoOrTs === 'number') {
      when = new Date(isoOrTs * 1000);
    } else if (typeof isoOrTs === 'string' && isoOrTs.match(/^\d{4}-\d{2}-\d{2}/)) {
      when = new Date(isoOrTs);
    } else {
      return String(isoOrTs);
    }
    var diff = Date.now() - when.getTime();
    if (diff < 0) return 'future';
    var mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + 'h ago';
    return Math.floor(hrs / 24) + 'd ago';
  }

  /* ====== OCC option-symbol parser ====== */
  function _occParse(sym) {
    var m = /^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/.exec(sym || '');
    if (!m) return null;
    var yy = 2000 + parseInt(m[2], 10);
    var mo = parseInt(m[3], 10);
    var dd = parseInt(m[4], 10);
    var monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var expiry = new Date(yy, mo - 1, dd);
    var dte = Math.max(0, Math.round((expiry - Date.now()) / 86400000));
    return {
      underlying: m[1],
      type: m[5] === 'C' ? 'call' : 'put',
      strike: parseInt(m[6], 10) / 1000,
      expiry: expiry,
      dte: dte,
      expiryLabel: monthNames[mo - 1] + ' ' + dd + ', ' + yy,
    };
  }

  /* ====== Scheduler-aware relative time ====== */
  var SCHEDULED_TIME_MAP = {
    auto_deployer: { h: 9,  m: 45 },
    wheel_deploy:  { h: 9,  m: 40 },
    monthly_rebalance: { h: 9, m: 45 },
    daily_close:   { h: 16, m: 5 },
    friday_reduction: { h: 15, m: 45 },
    weekly_learning: { h: 17, m: 0 },
    daily_backup_all: { h: 3, m: 0 },
    pead_refresh:  { h: 6, m: 0 }
  };

  function parseSchedTs(v, taskKey) {
    if (v == null) return null;
    if (typeof v === 'number') return v * 1000;
    if (typeof v !== 'string') return null;
    var m = v.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (m) {
      var y = parseInt(m[1], 10), mo = parseInt(m[2], 10) - 1, d = parseInt(m[3], 10);
      var sched = taskKey && SCHEDULED_TIME_MAP[taskKey];
      var hh = sched ? sched.h : 12, mm = sched ? sched.m : 0;
      return new Date(y, mo, d, hh, mm, 0).getTime();
    }
    if (v.match(/^\d{4}-\d{2}-\d{2}/)) return new Date(v).getTime();
    return null;
  }

  function latestForTask(lastRuns, taskKey) {
    var best = null;
    var bestMs = -1;
    if (Object.prototype.hasOwnProperty.call(lastRuns, taskKey)) {
      var sms = parseSchedTs(lastRuns[taskKey], taskKey);
      if (sms != null) { bestMs = sms; best = lastRuns[taskKey]; }
    }
    var prefix = taskKey + '_';
    Object.keys(lastRuns).forEach(function(k) {
      if (k.indexOf(prefix) !== 0) return;
      var ms = parseSchedTs(lastRuns[k], taskKey);
      if (ms == null) return;
      if (ms > bestMs) { bestMs = ms; best = lastRuns[k]; }
    });
    return best;
  }

  function fmtSchedLast(v, taskKey) {
    if (v == null) return 'never';
    var ms = parseSchedTs(v, taskKey);
    if (ms == null) return String(v);
    var diff = Date.now() - ms;
    if (diff < 0) return 'scheduled';
    var mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + 'h ago';
    if (typeof v === 'string' && v.match(/^\d{4}-\d{2}-\d{2}$/)) {
      var t = new Date();
      var today = new Date(t.getFullYear(), t.getMonth(), t.getDate());
      var m2 = v.match(/^(\d{4})-(\d{2})-(\d{2})$/);
      var that = new Date(parseInt(m2[1], 10), parseInt(m2[2], 10) - 1, parseInt(m2[3], 10));
      var days = Math.round((today.getTime() - that.getTime()) / 86400000);
      if (days === 1) return 'yesterday';
      if (days > 1) return days + 'd ago';
    }
    return Math.floor(hrs / 24) + 'd ago';
  }

  /* ====== Market-regime derivation ====== */
  function getMarketRegime(data) {
    if (data.market_regime) return data.market_regime;
    var picks = data.picks || [];
    if (picks.length === 0) return 'neutral';
    var avgChange = picks.slice(0, 20).reduce(function(s, p) {
      return s + (p.daily_change || 0);
    }, 0) / Math.min(picks.length, 20);
    if (avgChange > 1) return 'bull';
    if (avgChange < -1) return 'bear';
    return 'neutral';
  }

  /* ====== Heatmap color classifier ====== */
  function heatmapColor(pct) {
    if (pct === null || pct === undefined) return 'empty';
    if (pct < -2) return 'loss-big';
    if (pct < -0.5) return 'loss';
    if (pct < -0.05) return 'loss-small';
    if (pct < 0.05) return 'flat';
    if (pct < 0.5) return 'win-small';
    if (pct < 2) return 'win';
    return 'win-big';
  }

  /* ====== Data-freshness chip ====== */
  function freshnessChip(updatedAt, label) {
    if (!updatedAt) return '';
    var ms = null;
    try {
      if (typeof updatedAt === 'number') {
        ms = updatedAt * 1000;
      } else if (typeof updatedAt === 'string') {
        var cleaned = updatedAt.replace(/\s+ET$/i, '').trim();
        var parsed = new Date(cleaned);
        if (!isNaN(parsed.getTime())) ms = parsed.getTime();
      }
    } catch (_e) { ms = null; }
    if (ms === null) return '';
    var diffSec = Math.floor((Date.now() - ms) / 1000);
    if (diffSec < 0) diffSec = 0;
    var ageStr;
    if (diffSec < 60) ageStr = diffSec + 's ago';
    else if (diffSec < 3600) ageStr = Math.floor(diffSec / 60) + 'm ago';
    else ageStr = Math.floor(diffSec / 3600) + 'h ago';
    var cls = '';
    if (diffSec > 300) cls = 'very-stale';
    else if (diffSec > 120) cls = 'stale';
    var prefix = label ? esc(label) + ' ' : '';
    var labelAttr = label ? ' data-label="' + esc(label) + '"' : '';
    return '<span class="data-freshness ' + cls + '"' + labelAttr +
           ' title="Last updated: ' + esc(updatedAt) +
           '"><span class="dot"></span>' + prefix + ageStr + '</span>';
  }

  /* ====== Preset detection ====== */
  function detectActivePreset(d) {
    var g = d.guardrails || {};
    var c = d.auto_deployer_config || {};
    var stopPct = (c.risk_settings && c.risk_settings.default_stop_loss_pct) || 0.10;
    var maxPos = c.max_positions || g.max_positions || 5;
    var maxPerStock = g.max_position_pct || 0.10;

    if (stopPct === 0.05 && maxPos === 3 && maxPerStock === 0.05) return 'conservative';
    if (stopPct === 0.10 && maxPos === 5 && (maxPerStock === 0.07 || maxPerStock === 0.10)) return 'moderate';
    if (stopPct === 0.05 && maxPos >= 8 && maxPerStock >= 0.15) return 'aggressive';
    return 'custom';
  }

  /* ====== README HTML sanitizer ====== */
  var _README_ALLOWED_TAGS = new Set([
    'A','B','BLOCKQUOTE','BR','CODE','DEL','DIV','EM','H1','H2','H3',
    'H4','H5','H6','HR','I','IMG','LI','OL','P','PRE','SPAN','STRONG',
    'SUB','SUP','TABLE','TBODY','TD','TH','THEAD','TR','UL',
  ]);
  var _README_ALLOWED_ATTRS = {
    'A': new Set(['href','title','id']),
    'IMG': new Set(['src','alt','title','width','height']),
    '*': new Set(['id','class']),
  };

  function _sanitizeReadmeHtml(html) {
    var doc = new DOMParser().parseFromString('<div>' + html + '</div>', 'text/html');
    var root = doc.body.firstChild;
    function walk(node) {
      var kids = Array.prototype.slice.call(node.childNodes);
      kids.forEach(function(child) {
        if (child.nodeType !== 1) { return; }
        var tag = child.tagName;
        if (!_README_ALLOWED_TAGS.has(tag)) {
          var txt = document.createTextNode(child.textContent || '');
          child.parentNode.replaceChild(txt, child);
          return;
        }
        var allowed = _README_ALLOWED_ATTRS[tag] || _README_ALLOWED_ATTRS['*'];
        var globalAllowed = _README_ALLOWED_ATTRS['*'];
        Array.prototype.slice.call(child.attributes).forEach(function(attr) {
          var name = attr.name.toLowerCase();
          if (!allowed.has(name) && !globalAllowed.has(name)) {
            child.removeAttribute(attr.name);
            return;
          }
          if (name === 'href' || name === 'src') {
            var v = (attr.value || '').trim().toLowerCase();
            if (v.indexOf('javascript:') === 0 || v.indexOf('data:') === 0 || v.indexOf('vbscript:') === 0) {
              child.removeAttribute(attr.name);
            }
          }
        });
        walk(child);
      });
    }
    walk(root);
    return root.innerHTML;
  }

  /* ====== Attach to global scope =====
   *
   * Every function in this file goes on `window` (and `globalThis`) so the
   * 9600-line inline <script> at the bottom of dashboard.html can call them
   * as free globals — unchanged from pre-extraction.
   */
  var api = {
    esc: esc,
    jsStr: jsStr,
    fmtMoney: fmtMoney,
    fmtPct: fmtPct,
    pnlClass: pnlClass,
    fmtUpdatedET: fmtUpdatedET,
    fmtAuditTime: fmtAuditTime,
    fmtRelative: fmtRelative,
    _occParse: _occParse,
    SCHEDULED_TIME_MAP: SCHEDULED_TIME_MAP,
    parseSchedTs: parseSchedTs,
    latestForTask: latestForTask,
    fmtSchedLast: fmtSchedLast,
    getMarketRegime: getMarketRegime,
    heatmapColor: heatmapColor,
    freshnessChip: freshnessChip,
    detectActivePreset: detectActivePreset,
    _sanitizeReadmeHtml: _sanitizeReadmeHtml,
    _README_ALLOWED_TAGS: _README_ALLOWED_TAGS,
    _README_ALLOWED_ATTRS: _README_ALLOWED_ATTRS,
  };
  var g = (typeof window !== 'undefined') ? window :
          (typeof globalThis !== 'undefined') ? globalThis : this;
  Object.keys(api).forEach(function(k) { g[k] = api[k]; });
  g.DashboardRenderCore = api;
})();
