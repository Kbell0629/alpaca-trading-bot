/* Round-61 pt.8 Option B — dashboard render core (extracted from dashboard.html).
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

  /* ====== Section-visibility helpers ======
   * These persist a hidden-sections list in localStorage. `SECTION_KEYS`
   * lists every top-level dashboard section that can be toggled. The
   * inline script keeps its own copy of the array keyed by the same
   * ids; this extraction exposes just the getter/setter/toggle so tests
   * can drive them without touching the inline layer. */
  function getHiddenSections() {
    try { return JSON.parse(localStorage.getItem('hiddenSections') || '[]'); }
    catch (_e) { return []; }
  }
  function setHiddenSections(arr) {
    try { localStorage.setItem('hiddenSections', JSON.stringify(arr || [])); }
    catch (_e) { /* private mode — no-op */ }
  }
  function toggleSectionId(sectionId) {
    var hidden = getHiddenSections();
    var idx = hidden.indexOf(sectionId);
    if (idx >= 0) hidden.splice(idx, 1);
    else hidden.push(sectionId);
    setHiddenSections(hidden);
    return hidden;
  }

  /* ====== Section-help button ======
   * Small inline help icon that opens a section-specific guide modal. */
  function sectionHelpButton(sectionId) {
    return '<button class="section-help-btn" title="Explain this section" aria-label="Help for ' +
      esc(sectionId) + '" onclick="openSectionGuide(\'' + esc(sectionId) +
      '\')">&#9432;</button>';
  }

  /* ====== Guardrail meters (daily-loss + drawdown bars) ======
   * Renders the two progress-bar meters the user sees under "Safety
   * Limits". Accepts `guardrails` as an optional 4th argument — defaults
   * to window.guardrailsData for backward compat with the inline caller. */
  function buildGuardrailMeters(dailyPnlPct, portfolioValue, lastEquity, guardrails) {
    var gr = guardrails || (typeof window !== 'undefined' ? window.guardrailsData : null) || {};
    var dailyLimit = (gr.daily_loss_limit_pct || 0.03) * 100;
    var maxDrawdown = (gr.max_drawdown_pct || 0.10) * 100;
    var peakValue = gr.peak_portfolio_value || lastEquity || portfolioValue;
    // Round-61 pt.11: if portfolioValue is 0/missing (e.g. Alpaca
    // /account fetch failed), the drawdown computation against the
    // stored peak yields a misleading "100% drawdown" alarm. Skip
    // the drawdown panel entirely until we have a real portfolio
    // value — the dashboard already surfaces the API failure via the
    // api_errors banner.
    var portfolioValid = (portfolioValue && portfolioValue > 0);
    var currentDrawdown = (portfolioValid && peakValue > 0)
      ? ((peakValue - portfolioValue) / peakValue * 100) : 0;
    var dailyLossPct = Math.abs(Math.min(0, dailyPnlPct));

    var dailyRatio = dailyLimit > 0 ? (dailyLossPct / dailyLimit * 100) : 0;
    var dailyColor = 'green';
    var dailyWarn = '';
    if (dailyRatio > 80) { dailyColor = 'red'; dailyWarn = '<div class="guardrail-warning red">Approaching daily loss limit!</div>'; }
    else if (dailyRatio > 50) { dailyColor = 'yellow'; dailyWarn = '<div class="guardrail-warning yellow">Over 50% of daily loss limit used</div>'; }

    var ddRatio = (portfolioValid && maxDrawdown > 0) ? (currentDrawdown / maxDrawdown * 100) : 0;
    var ddColor = 'green';
    var ddWarn = '';
    if (portfolioValid) {
      if (ddRatio > 80) { ddColor = 'red'; ddWarn = '<div class="guardrail-warning red">Approaching max drawdown limit!</div>'; }
      else if (ddRatio > 50) { ddColor = 'yellow'; ddWarn = '<div class="guardrail-warning yellow">Over 50% of max drawdown limit used</div>'; }
    }
    // When portfolio value is unavailable, render the meter at 0% with
    // an explanatory note instead of the false "100% drawdown" alarm.
    var ddLabelRight = portfolioValid
      ? (currentDrawdown.toFixed(1) + '% / ' + maxDrawdown.toFixed(0) + '% limit')
      : ('— / ' + maxDrawdown.toFixed(0) + '% limit');
    var ddBarPct = portfolioValid ? Math.min(100, ddRatio) : 0;

    return '<div class="guardrail-meters">' +
      '<div class="guardrail-meter">' +
        '<div class="meter-label"><span>Daily Loss</span><span>' + dailyLossPct.toFixed(1) + '% / ' + dailyLimit.toFixed(0) + '% limit</span></div>' +
        '<div class="meter-bar"><div class="meter-fill ' + dailyColor + '" style="width:' + Math.min(100, dailyRatio) + '%"></div><div class="meter-limit"></div></div>' +
        dailyWarn +
      '</div>' +
      '<div class="guardrail-meter">' +
        '<div class="meter-label"><span>Drawdown from Peak</span><span>' + ddLabelRight + '</span></div>' +
        '<div class="meter-bar"><div class="meter-fill ' + ddColor + '" style="width:' + ddBarPct + '%"></div><div class="meter-limit"></div></div>' +
        ddWarn +
      '</div>' +
    '</div>';
  }

  /* ====== Today's Closes panel (Round-34) ======
   * Hidden entirely when empty. Otherwise renders a row per closed
   * trade with P&L color-coded and exit time in ET. */
  function buildTodaysClosesPanel(d) {
    var closes = Array.isArray(d.todays_closes) ? d.todays_closes : [];
    if (!closes.length) return '';
    var totalPnl = closes.reduce(function(acc, c) {
      var p = c.pnl;
      return acc + (typeof p === 'number' ? p : 0);
    }, 0);
    var rows = closes.map(function(c) {
      var pnlNum = (typeof c.pnl === 'number') ? c.pnl : null;
      var pnlColor = pnlNum === null ? 'var(--text-dim)' :
                     (pnlNum > 0 ? 'var(--green)' :
                      pnlNum < 0 ? 'var(--red)' : 'var(--text-dim)');
      var pnlStr = pnlNum === null ? '—' : fmtMoney(pnlNum);
      var pctStr = (typeof c.pnl_pct === 'number')
                   ? ' (' + (c.pnl_pct >= 0 ? '+' : '') + c.pnl_pct.toFixed(2) + '%)'
                   : '';
      var tm = '';
      try {
        var dt = new Date(c.exit_timestamp);
        tm = dt.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' });
      } catch (_) { tm = c.exit_timestamp || ''; }
      var orphan = c.orphan_close
        ? ' <span title="Synthetic entry — original open trade was not journaled" style="font-size:10px;color:var(--orange)">[orphan]</span>'
        : '';
      var exitPx = (typeof c.exit_price === 'number') ? '$' + c.exit_price.toFixed(2) : '—';
      return '<tr>' +
        '<td style="white-space:nowrap">' + esc(tm) + '</td>' +
        '<td><strong>' + esc(c.symbol || '?') + '</strong>' + orphan + '</td>' +
        '<td>' + esc(c.strategy || '?') + '</td>' +
        '<td>' + esc(c.exit_reason || '?') + '</td>' +
        '<td style="text-align:right">' + exitPx + '</td>' +
        '<td style="text-align:right;color:' + pnlColor + ';white-space:nowrap">' +
          pnlStr + pctStr +
        '</td>' +
      '</tr>';
    }).join('');
    var totalColor = totalPnl > 0 ? 'var(--green)' : totalPnl < 0 ? 'var(--red)' : 'var(--text-dim)';
    return '<div class="todays-closes-panel" style="background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:24px">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px">' +
        '<h3 style="margin:0;font-size:14px">Today\'s Closes' + sectionHelpButton('todays-closes') + '</h3>' +
        '<div style="font-size:13px"><span style="color:var(--text-dim)">Net P&L:</span> <strong style="color:' + totalColor + '">' + fmtMoney(totalPnl) + '</strong></div>' +
      '</div>' +
      '<div style="overflow-x:auto">' +
      '<table style="width:100%;font-size:12px;min-width:520px">' +
        '<thead><tr style="color:var(--text-dim);text-align:left">' +
          '<th>Time</th><th>Symbol</th><th>Strategy</th><th>Reason</th>' +
          '<th style="text-align:right">Exit</th><th style="text-align:right">P&L</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>' +
      '</div>' +
    '</div>';
  }

  /* ====== Short-selling strategy card ======
   * Pure given `d`. Read-only surface of the short-selling strategy
   * state: user toggle + unlock gate (bear regime + SPY mom < -3).
   * Moderate regime + SPY momentum 20d define `shortsUnlocked`. */
  function buildShortStrategyCard(d) {
    var marketRegime = (d.market_regime || (d.economic_calendar && d.economic_calendar.market_regime) || 'neutral').toLowerCase();
    var spyMom20 = d.spy_momentum_20d != null ? d.spy_momentum_20d : 0;
    var shortsUnlocked = (marketRegime === 'bear' && spyMom20 < -3);
    var config = d.auto_deployer_config || {};
    var shortConfig = (config.short_selling || {});
    var userEnabled = shortConfig.enabled !== false;
    var effectivelyActive = userEnabled && shortsUnlocked;

    var shortCount = 0;
    if (d.positions) {
      shortCount = d.positions.filter(function(p) { return parseFloat(p.qty || 0) < 0; }).length;
    }
    var maxShorts = shortConfig.max_short_positions || 1;

    var statusBadge;
    if (!userEnabled) {
      statusBadge = '<span class="badge-inactive">TURNED OFF</span>';
    } else if (effectivelyActive) {
      statusBadge = '<span class="badge-active">ACTIVE (BEAR MKT)</span>';
    } else {
      statusBadge = '<span class="badge-pending">STANDBY</span>';
    }

    var spyStr = (spyMom20 >= 0 ? '+' : '') + spyMom20.toFixed(1) + '%';
    var conditions = effectivelyActive
      ? 'Bear market detected. Shorts will deploy on qualifying candidates.'
      : (!userEnabled
        ? 'Short selling is turned OFF. Enable below to allow shorts in bear markets.'
        : 'Waiting for bear market. SPY 20d: ' + spyStr + ' (need &lt; -3%). Regime: ' + marketRegime);

    var topShort = (d.short_candidates && d.short_candidates.length) ? d.short_candidates[0] : null;
    var topShortHtml = topShort
      ? '<div class="stat"><div class="stat-label">Top Candidate</div><div class="stat-value" style="font-size:14px">' + esc(topShort.symbol) + ' (score ' + topShort.short_score + ')</div></div>'
      : '<div class="stat"><div class="stat-label">Top Candidate</div><div class="stat-value" style="font-size:12px;color:var(--text-dim)">None scoring &ge;15</div></div>';

    var toggleBtn = userEnabled
      ? '<button class="btn-warning btn-sm" onclick="toggleShortSelling(false)">Turn Off</button>'
      : '<button class="btn-primary btn-sm" onclick="toggleShortSelling(true)">Turn On</button>';

    return (
      '<div class="strategy-card short-sell">' +
        '<h2>6. Short Selling</h2>' +
        '<div class="subtitle">Bear market plays — profit when stocks fall</div>' +
        '<div class="stat-grid">' +
          '<div class="stat"><div class="stat-label">Status</div><div class="stat-value">' + statusBadge + '</div></div>' +
          '<div class="stat"><div class="stat-label">Positions</div><div class="stat-value">' + shortCount + '/' + maxShorts + '</div></div>' +
          '<div class="stat"><div class="stat-label">SPY 20d</div><div class="stat-value">' + spyStr + '</div></div>' +
          '<div class="stat"><div class="stat-label">Stop-Loss</div><div class="stat-value">8% (tight)</div></div>' +
          topShortHtml +
          '<div class="stat"><div class="stat-label">Profit Target</div><div class="stat-value">15%</div></div>' +
        '</div>' +
        '<div class="strategy-visual"><strong>Rules:</strong> Bear market only | Max 1 short | 5% portfolio per short | 48hr cooldown after loss | No meme stocks</div>' +
        '<div class="strategy-visual" style="background:rgba(239,68,68,0.05);border-color:rgba(239,68,68,0.2);margin-top:8px;font-size:11px">' + conditions + '</div>' +
        '<div class="strategy-actions">' +
          toggleBtn +
          '<button class="btn-warning btn-sm" onclick="if(confirm(\'Pause short selling? The bot will not deploy new shorts.\')) fetch(\'/api/pause-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'short_sell\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Paused\',\'info\');addLog(\'Paused short selling\',\'info\');refreshData();})">Pause</button>' +
          '<button class="btn-danger btn-sm" onclick="if(confirm(\'Stop short selling? This will cover (buy back) any open short positions.\')) fetch(\'/api/stop-strategy\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({strategy:\'short_sell\'})}).then(r=>r.json()).then(d=>{toast(d.message||\'Stopped\',\'info\');addLog(\'Stopped short selling\',\'info\');refreshData();})">Stop</button>' +
        '</div>' +
      '</div>'
    );
  }

  /* ====== Paper-vs-Live comparison panel ======
   * Pure given `d`. Renders the side-by-side comparison card. Post-61
   * pt.8 defensive fix: gates win-rate reliability on closed_trades >=
   * 5 directly so a missing API field can't flip the panel back to the
   * alarmist "0%" branch. */
  function buildComparisonPanel(d) {
    var acct = d.account || {};
    var sc = d.scorecard || {};
    var paperValue = parseFloat(acct.portfolio_value || 0);
    var paperStartValue = sc.starting_capital || 100000;
    var paperReturn = ((paperValue - paperStartValue) / paperStartValue * 100).toFixed(2);
    var paperReturnClass = parseFloat(paperReturn) >= 0 ? 'positive' : 'negative';
    var closedTrades = parseInt(sc.closed_trades) || 0;
    var winRateReliable = closedTrades >= 5 && sc.win_rate_reliable !== false;
    var winRateSample = sc.win_rate_sample_size != null ? sc.win_rate_sample_size : closedTrades;
    var winRate = sc.win_rate_pct || 0;
    var winRateHtml = winRateReliable
      ? '<div style="font-size:18px;font-weight:700">' + winRate.toFixed(0) + '%</div>' +
        '<div style="font-size:9px;color:var(--text-dim);margin-top:2px">target 50%+</div>'
      : '<div style="font-size:14px;font-weight:700;color:var(--orange)">N=' + winRateSample + '</div>' +
        '<div style="font-size:9px;color:var(--text-dim);margin-top:2px" title="' + esc(sc.win_rate_display_note || 'Not enough closed trades for a reliable win rate.') + '">Need 5+ trades</div>';
    var readiness = sc.readiness_score || 0;
    var readyForLive = readiness >= 80;
    var liveActive = (d.auto_deployer_config && d.auto_deployer_config.live_mode_active) || false;

    var paperHtml =
      '<div class="comparison-card paper">' +
        '<div class="comparison-title">Paper Trading <span class="comparison-status active">ACTIVE</span></div>' +
        '<div style="font-size:11px;color:var(--text-dim);margin-bottom:12px">Simulated trading — no real money</div>' +
        '<div class="comparison-metrics">' +
          '<div><div style="font-size:10px;color:var(--text-dim)">Portfolio</div><div style="font-size:18px;font-weight:700">$' + paperValue.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '</div></div>' +
          '<div><div style="font-size:10px;color:var(--text-dim)">Total Return</div><div class="' + paperReturnClass + '" style="font-size:18px;font-weight:700">' + (parseFloat(paperReturn) >= 0 ? '+' : '') + paperReturn + '%</div></div>' +
          '<div><div style="font-size:10px;color:var(--text-dim)">Win Rate</div>' + winRateHtml + '</div>' +
          '<div><div style="font-size:10px;color:var(--text-dim)">Readiness</div><div style="font-size:18px;font-weight:700">' + readiness + '/100</div></div>' +
        '</div>' +
      '</div>';

    var liveHtml;
    if (liveActive) {
      liveHtml =
        '<div class="comparison-card live">' +
          '<div class="comparison-title">Live Trading <span class="comparison-status active">ACTIVE</span></div>' +
          '<div style="font-size:11px;color:var(--text-dim);margin-bottom:12px">Real money — actual trades</div>' +
          '<div class="comparison-metrics">' +
            '<div><div style="font-size:10px;color:var(--text-dim)">Portfolio</div><div style="font-size:18px;font-weight:700">Coming soon</div></div>' +
          '</div>' +
        '</div>';
    } else {
      var readyMsg = readyForLive
        ? '<strong style="color:var(--green)">✓ Ready for live trading!</strong> Readiness score: ' + readiness + '/100'
        : '<strong style="color:var(--orange)">Not ready yet.</strong> Need ' + (80 - readiness) + ' more readiness points (currently ' + readiness + '/100).';
      liveHtml =
        '<div class="comparison-card live inactive">' +
          '<div class="comparison-title">Live Trading <span class="comparison-status inactive">NOT ACTIVE</span></div>' +
          '<div style="font-size:11px;color:var(--text-dim);margin-bottom:12px">Real money trading — not yet configured</div>' +
          '<div class="comparison-setup">' +
            '<div style="margin-bottom:8px">' + readyMsg + '</div>' +
            '<div><strong>To activate live trading:</strong></div>' +
            '<ol>' +
              '<li>Hit 80/100 readiness score (30 days of profitable paper trading)</li>' +
              '<li>Create a live Alpaca account and fund with $5k</li>' +
              '<li>Generate live API keys at alpaca.markets</li>' +
              '<li>Update Railway env vars: ALPACA_ENDPOINT to https://api.alpaca.markets/v2</li>' +
              '<li>Update ALPACA_API_KEY and ALPACA_API_SECRET to live keys</li>' +
              '<li>Keep paper running in parallel to compare</li>' +
            '</ol>' +
          '</div>' +
        '</div>';
    }

    var delta = '';
    if (liveActive) {
      delta = '<div class="comparison-delta">Performance gap: paper returns typically exceed live by ~2-5% due to slippage and fills.</div>';
    }

    return '<div class="comparison-grid">' + paperHtml + liveHtml + '</div>' + delta;
  }

  /* ====== Strategy Templates (Settings tab) ======
   * Pure given `d`. Renders the three preset cards (conservative,
   * moderate, aggressive) + active-mode indicator. */
  function buildStrategyTemplates(d) {
    var active = detectActivePreset(d);
    var c = d.auto_deployer_config || {};
    var g = d.guardrails || {};

    var curStop = Math.round(((c.risk_settings && c.risk_settings.default_stop_loss_pct) || 0.10) * 100);
    var curMaxPos = c.max_positions || g.max_positions || 5;
    var curMaxPerStock = Math.round((g.max_position_pct || 0.10) * 100);
    var curStrats = (g.strategies_allowed || []).length;

    var presets = {
      conservative: {
        name: 'Conservative', tagline: 'Capital preservation first',
        stopLoss: '5%', maxPositions: 3, maxPerStock: '5%', maxNewPerDay: 1,
        strategies: ['Wheel (premium income)', 'Trailing-stop exits on every entry'],
        excluded: ['Breakout', 'Mean Reversion', 'Short Selling'],
        detail: 'Tight 5% stops to cut losses fast. Fewer positions (max 3) means less overall market exposure. Smaller 5% position sizing per stock. Only runs proven, slower strategies — no aggressive breakout chasing or short selling.',
        goodFor: 'First-time traders, small accounts under $5k, during high market uncertainty, or when you want to sleep well at night.',
        tradeoffs: 'Lower returns in bull markets (misses breakouts). Slower to deploy capital. Won\'t capture big short-term swings.',
        expectedReturn: '5-15% annually (lower volatility)', maxDrawdown: '~5-8% typical',
        color: '#10b981'
      },
      moderate: {
        name: 'Moderate', tagline: 'Balanced risk/reward — round-20 trade quality',
        stopLoss: '10%', maxPositions: 5, maxPerStock: '7%', maxNewPerDay: 2,
        strategies: ['Breakout + Mean Reversion + Wheel + PEAD', '(shorts only in bear markets)'],
        excluded: ['Short Selling auto-deploy in bull'],
        detail: 'Standard 10% stop-loss with a wider 12% breakout stop (volatile breakouts need room). Up to 5 concurrent positions sized at 7% each so a single loser is bounded at -0.7% portfolio. All 5 strategies enabled. Round-20 trade-quality gates active: skip Breakout/PEAD picks already +8% intraday (don\'t-chase) and skip volatility >20% picks (avoid INFQ-tier meme noise). Shorts only auto-deploy in bear markets.',
        goodFor: 'The default recommendation. Accounts $5k-$50k. Users who want the bot to work across all market conditions without babysitting.',
        tradeoffs: 'Middle ground — won\'t be the best in any single market regime but stays reasonable across all of them. Smaller positions mean lower upside on a winner; the trade-quality gates may pass on hot momentum names.',
        expectedReturn: '15-25% annually', maxDrawdown: '~10% max (enforced by guardrails)',
        color: '#3b82f6'
      },
      aggressive: {
        name: 'Aggressive', tagline: 'Maximize upside, accept volatility',
        stopLoss: '5% (tight)', maxPositions: 8, maxPerStock: '15%', maxNewPerDay: 3,
        strategies: ['Breakout + Mean Reversion + Wheel + PEAD + Short Selling', 'Shorts enabled anytime'],
        excluded: [],
        detail: 'Tight 5% stops (fail fast), but larger 15% positions and up to 8 concurrent trades. Extended hours trading enabled. Short selling runs in any market regime, not just bear. Breakouts prioritized. Pre-market and after-hours sessions used when appropriate.',
        goodFor: 'Experienced traders. Accounts $25k+ (pattern day trader rules). Active day traders who want maximum signal deployment.',
        tradeoffs: 'Higher drawdowns. More false signals (tight stops = more stop-outs). Requires closer monitoring. Higher tax bills from more frequent trades.',
        expectedReturn: '20-40% annually (or -20% in bad year)', maxDrawdown: '~15-20% possible',
        color: '#ef4444'
      }
    };

    var order = ['conservative', 'moderate', 'aggressive'];
    var cards = order.map(function(key) {
      var p = presets[key];
      var isActive = (active === key);
      var badge = isActive ? '<span class="preset-active-badge">ACTIVE</span>' : '';
      var btnLabel = isActive ? 'Currently Active' : 'Apply ' + p.name;
      var btnDisabled = isActive ? 'disabled' : '';
      var strategiesHtml = p.strategies.map(function(s) {
        return '<span class="preset-pill ok">' + esc(s) + '</span>';
      }).join('');
      var excludedHtml = p.excluded.length ? p.excluded.map(function(s) {
        return '<span class="preset-pill no">' + esc(s) + '</span>';
      }).join('') : '';
      return (
        '<div class="preset-card-v2 ' + (isActive ? 'active' : '') + '" style="border-top-color:' + p.color + '">' +
          '<div class="preset-header">' +
            '<div>' +
              '<div class="preset-name" style="color:' + p.color + '">' + p.name + '</div>' +
              '<div class="preset-tag">' + esc(p.tagline) + '</div>' +
            '</div>' + badge +
          '</div>' +
          '<div class="preset-stats">' +
            '<div><span class="lbl">Stop-Loss</span><span class="val">' + p.stopLoss + '</span></div>' +
            '<div><span class="lbl">Max Positions</span><span class="val">' + p.maxPositions + '</span></div>' +
            '<div><span class="lbl">Per Stock</span><span class="val">' + p.maxPerStock + '</span></div>' +
            '<div><span class="lbl">New/Day</span><span class="val">' + p.maxNewPerDay + '</span></div>' +
          '</div>' +
          '<div class="preset-section"><div class="preset-section-title">How it works</div><div class="preset-section-body">' + esc(p.detail) + '</div></div>' +
          '<div class="preset-section"><div class="preset-section-title">Strategies Enabled</div><div>' + strategiesHtml + '</div></div>' +
          (excludedHtml ? '<div class="preset-section"><div class="preset-section-title">Disabled</div><div>' + excludedHtml + '</div></div>' : '') +
          '<div class="preset-section"><div class="preset-section-title">Good for</div><div class="preset-section-body">' + esc(p.goodFor) + '</div></div>' +
          '<div class="preset-section"><div class="preset-section-title">Tradeoffs</div><div class="preset-section-body">' + esc(p.tradeoffs) + '</div></div>' +
          '<div class="preset-outcome">' +
            '<div class="preset-outcome-row"><span>Expected return</span><strong>' + esc(p.expectedReturn) + '</strong></div>' +
            '<div class="preset-outcome-row"><span>Max drawdown</span><strong>' + esc(p.maxDrawdown) + '</strong></div>' +
          '</div>' +
          '<button class="preset-apply-btn" onclick="applyPreset(\'' + key + '\')" ' + btnDisabled + ' style="background:' + (isActive ? 'var(--border)' : p.color) + '">' + btnLabel + '</button>' +
        '</div>'
      );
    }).join('');

    var activeLabel = active === 'custom'
      ? '<span style="color:var(--orange)">CUSTOM (doesn\'t match any preset)</span>'
      : '<span style="color:' + presets[active].color + '">' + presets[active].name.toUpperCase() + '</span>';

    return (
      '<div class="marketplace">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:16px">' +
          '<div>' +
            '<h3 style="margin:0">Strategy Templates</h3>' +
            '<div style="font-size:13px;color:var(--text-dim);margin-top:6px">Currently running: ' + activeLabel +
              ' &nbsp;·&nbsp; Stop: ' + curStop + '% · Max positions: ' + curMaxPos + ' · Per stock: ' + curMaxPerStock + '% · ' + curStrats + ' strategies enabled' +
            '</div>' +
          '</div>' +
          '<div class="marketplace-actions">' +
            '<button class="btn-primary btn-sm" onclick="exportStrategies()">Export My Strategies</button>' +
            '<button class="btn-sm" onclick="document.getElementById(\'importFile\').click()">Import Strategy</button>' +
            '<input type="file" id="importFile" accept=".json" style="display:none" onchange="importStrategy(this)">' +
          '</div>' +
        '</div>' +
        '<div class="preset-strategies-v2">' + cards + '</div>' +
      '</div>'
    );
  }

  /* ====== Next-actions (What Happens at Market Open) ======
   * Reads 3 module-local flags that the inline script owns:
   * `autoDeployerEnabled`, `killSwitchActive`, `guardrailsData`.
   * `opts` lets tests inject each; production callers from inline pass
   * nothing and we fall through to `window.<name>` (which the inline
   * script has populated). */
  function buildNextActionsPanel(d, opts) {
    opts = opts || {};
    var w = (typeof window !== 'undefined') ? window : {};
    var autoDeployerEnabled = (opts.autoDeployerEnabled != null)
      ? opts.autoDeployerEnabled : w.autoDeployerEnabled;
    var killSwitchActive = (opts.killSwitchActive != null)
      ? opts.killSwitchActive : w.killSwitchActive;
    var gr = opts.guardrails || w.guardrailsData || {};

    var orders = d.open_orders || [];
    var positions = d.positions || [];

    var adOn = autoDeployerEnabled && !killSwitchActive;
    var monOn = !killSwitchActive;
    var wheelOn = !killSwitchActive;

    var timelineHtml =
      '<div class="timeline-item"><span class="time">3:00 AM ET</span><span class="action">Daily backup: snapshots users.db + all per-user data (14-day retention)</span><span class="badge-on">AUTO</span></div>' +
      '<div class="timeline-item"><span class="time">6:00 AM ET</span><span class="action">PEAD scan: yfinance pulls EPS actuals/estimates for ~120 large-caps; flags symbols that beat by 5%+ within last 3 days for the post-earnings drift play</span><span class="badge-on">AUTO</span></div>' +
      '<div class="timeline-item' + (adOn ? '' : ' off') + '"><span class="time">9:35 AM ET</span><span class="action">Auto-Deployer screens ~12,000 stocks, deploys top picks (breakout / mean reversion / PEAD). Every entry uses a trailing-stop exit.</span><span class="' + (adOn ? 'badge-on' : 'badge-off') + '">' + (adOn ? 'ON' : 'OFF') + '</span></div>' +
      '<div class="timeline-item' + (wheelOn ? '' : ' off') + '"><span class="time">9:40 AM ET</span><span class="action">Wheel Strategy auto-deploy sells cash-secured puts on top wheel candidates ($10-$50 stocks)</span><span class="' + (wheelOn ? 'badge-on' : 'badge-off') + '">' + (wheelOn ? 'ON' : 'OFF') + '</span></div>' +
      '<div class="timeline-item"><span class="time">9:45 AM ET (1st trading day of month)</span><span class="action">Monthly rebalance: closes 60+ day losers to free capital</span><span class="badge-on">AUTO</span></div>' +
      '<div class="timeline-item active' + (monOn ? '' : ' off') + '"><span class="time">Every 60 sec (market hours)</span><span class="action">Strategy Monitor: adjusts stops, checks profit-ladder fills, ratchets trailing stops up</span><span class="' + (monOn ? 'badge-on' : 'badge-off') + '">' + (monOn ? 'ON' : 'OFF') + '</span></div>' +
      '<div class="timeline-item active' + (wheelOn ? '' : ' off') + '"><span class="time">Every 15 min (market hours)</span><span class="action">Wheel Monitor: checks fills, assignments, sells covered calls, buys-to-close at 50% profit</span><span class="' + (wheelOn ? 'badge-on' : 'badge-off') + '">' + (wheelOn ? 'ON' : 'OFF') + '</span></div>' +
      '<div class="timeline-item active' + (adOn ? '' : ' off') + '"><span class="time">Every 30 min (market hours)</span><span class="action">Screener refresh: 12k stocks filtered + scored</span><span class="' + (adOn ? 'badge-on' : 'badge-off') + '">' + (adOn ? 'ON' : 'OFF') + '</span></div>' +
      '<div class="timeline-item"><span class="time">3:45 PM ET (Fridays)</span><span class="action">Weekly risk reduction: trim 50% off winners >20% before weekend gap</span><span class="badge-on">AUTO</span></div>' +
      '<div class="timeline-item"><span class="time">4:05 PM ET</span><span class="action">Daily close summary: scorecard, readiness score, orphan recovery</span><span class="badge-on">AUTO</span></div>' +
      '<div class="timeline-item"><span class="time">5:00 PM ET (Fridays)</span><span class="action">Weekly learning: analyzes trade journal, adjusts strategy weights</span><span class="badge-on">AUTO</span></div>';

    var pendingHtml = '';
    if (orders.length > 0) {
      var items = '';
      orders.forEach(function(o) {
        var sym = o.symbol || '';
        var side = (o.side || '').toUpperCase();
        var qty = o.qty || '';
        var type = o.type || '';
        var price = o.limit_price || o.stop_price || 'market';
        var desc = sym + ' ' + type + ' ' + side.toLowerCase() + ' (' + qty + ' shares)';
        desc += (price !== 'market') ? ' @ $' + price : ' -- will fill at open';
        items += '<li>' + desc + '</li>';
      });
      pendingHtml = '<div class="pending-orders"><h4>Pending Actions</h4><ul>' + items + '</ul></div>';
    } else if (positions.length > 0) {
      var items2 = '';
      positions.forEach(function(p) {
        items2 += '<li>' + (p.symbol || '') + ': holding ' + (p.qty || 0) + ' shares, stop-loss active</li>';
      });
      pendingHtml = '<div class="pending-orders"><h4>Current Status</h4><ul>' + items2 + '</ul></div>';
    } else {
      pendingHtml = '<div class="pending-orders"><h4>Pending Actions</h4><ul><li>No pending orders or positions</li></ul></div>';
    }

    var dailyLimitPct = ((gr.daily_loss_limit_pct || 0.03) * 100).toFixed(0);
    var maxDDPct = ((gr.max_drawdown_pct || 0.10) * 100).toFixed(0);
    var maxPos = gr.max_positions || 5;
    var maxPerStock = ((gr.max_position_pct || 0.10) * 100).toFixed(0);

    var pillsHtml =
      '<span class="guardrail-pill">Daily loss limit: ' + dailyLimitPct + '%</span>' +
      '<span class="guardrail-pill">Max drawdown: ' + maxDDPct + '%</span>' +
      '<span class="guardrail-pill">Max positions: ' + maxPos + '</span>' +
      '<span class="guardrail-pill">Max per stock: ' + maxPerStock + '%</span>';
    if (killSwitchActive) {
      pillsHtml += '<span class="guardrail-pill danger">Kill Switch: ACTIVE</span>';
    }

    return '<div class="next-actions-panel">' +
      '<h3>What Happens at Market Open</h3>' +
      '<div class="timeline">' + timelineHtml + '</div>' +
      pendingHtml +
      '<div class="guardrails-summary"><h4>Safety Limits Active</h4><div class="guardrail-items">' + pillsHtml + '</div></div>' +
    '</div>';
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
    // Section-visibility (localStorage-backed)
    getHiddenSections: getHiddenSections,
    setHiddenSections: setHiddenSections,
    toggleSectionId: toggleSectionId,
    // Section-help button (inline icon → openSectionGuide)
    sectionHelpButton: sectionHelpButton,
    // Panel-render helpers
    buildGuardrailMeters: buildGuardrailMeters,
    buildTodaysClosesPanel: buildTodaysClosesPanel,
    buildShortStrategyCard: buildShortStrategyCard,
    buildComparisonPanel: buildComparisonPanel,
    buildStrategyTemplates: buildStrategyTemplates,
    buildNextActionsPanel: buildNextActionsPanel,
  };
  var g = (typeof window !== 'undefined') ? window :
          (typeof globalThis !== 'undefined') ? globalThis : this;
  Object.keys(api).forEach(function(k) { g[k] = api[k]; });
  g.DashboardRenderCore = api;
})();
