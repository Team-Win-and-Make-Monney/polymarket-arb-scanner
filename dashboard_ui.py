"""Dashboard HTML template — single-page trading dashboard.

The HTML is served by dashboard.py at GET /. It fetches data from the
JSON API endpoints every REFRESH_SECONDS and renders charts + tables.
"""


def get_dashboard_html(refresh_seconds: int = 15) -> str:
    """Return the complete dashboard HTML as a string.

    Args:
        refresh_seconds: Auto-refresh interval for data polling.

    Returns:
        Full HTML document string with embedded CSS and JS.
    """
    return _TEMPLATE.replace("__REFRESH_SECONDS__", str(refresh_seconds))


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Arb Scanner</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js" integrity="sha384-vsrfeLOOY6KuIYKDlmVH5UiBmgIdB1oEf7p01YgWHuqmOHfZr374+odEv96n9tNC" crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<style>
/* ---------------------------------------------------------------------------
   Reset & base
   --------------------------------------------------------------------------- */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0f1117;
  --surface: #1a1d27;
  --surface2: #232735;
  --border: #2d3148;
  --text: #e4e6f0;
  --text-muted: #8b8fa3;
  --accent: #6366f1;
  --accent-dim: #4f46e5;
  --green: #22c55e;
  --green-dim: rgba(34,197,94,0.15);
  --red: #ef4444;
  --red-dim: rgba(239,68,68,0.15);
  --yellow: #eab308;
  --yellow-dim: rgba(234,179,8,0.15);
  --blue: #3b82f6;
  --blue-dim: rgba(59,130,246,0.15);
  --font: 'Segoe UI', system-ui, -apple-system, sans-serif;
  --mono: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
  --radius: 8px;
}
html { font-size: 14px; }
body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  min-height: 100vh;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ---------------------------------------------------------------------------
   Layout
   --------------------------------------------------------------------------- */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 100;
}
.header h1 { font-size: 1.15rem; font-weight: 600; letter-spacing: -0.01em; }
.header-right { display: flex; align-items: center; gap: 16px; font-size: 0.85rem; }
.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.badge-live { background: var(--green-dim); color: var(--green); }
.badge-dry { background: var(--yellow-dim); color: var(--yellow); }
.badge-error { background: var(--red-dim); color: var(--red); }
.dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.dot-green { background: var(--green); }
.dot-yellow { background: var(--yellow); }
.dot-red { background: var(--red); }
.dot-pulse { animation: pulse 2s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

.main { padding: 20px 24px; max-width: 1440px; margin: 0 auto; }

/* ---------------------------------------------------------------------------
   Cards grid
   --------------------------------------------------------------------------- */
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
}
.card-label { font-size: 0.78rem; color: var(--text-muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.05em; }
.card-value { font-size: 1.65rem; font-weight: 700; font-family: var(--mono); }
.card-sub { font-size: 0.78rem; color: var(--text-muted); margin-top: 4px; }
.positive { color: var(--green); }
.negative { color: var(--red); }

/* ---------------------------------------------------------------------------
   Sections
   --------------------------------------------------------------------------- */
.section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 20px;
  overflow: hidden;
}
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
  font-size: 0.92rem;
}
.section-body { padding: 16px 18px; }
.section-body.no-pad { padding: 0; }

/* ---------------------------------------------------------------------------
   Tables
   --------------------------------------------------------------------------- */
.tbl { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
.tbl th {
  text-align: left;
  padding: 10px 14px;
  color: var(--text-muted);
  font-weight: 500;
  font-size: 0.76rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.tbl td {
  padding: 9px 14px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.tbl tr:last-child td { border-bottom: none; }
.tbl tr:hover td { background: var(--surface2); }
.tbl .mono { font-family: var(--mono); font-size: 0.82rem; }
.tbl .right { text-align: right; }

.status-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.72rem;
  font-weight: 600;
  text-transform: uppercase;
}
.status-filled { background: var(--green-dim); color: var(--green); }
.status-pending { background: var(--yellow-dim); color: var(--yellow); }
.status-failed { background: var(--red-dim); color: var(--red); }
.status-open { background: var(--blue-dim); color: var(--blue); }
.status-dry_run { background: var(--yellow-dim); color: var(--yellow); }

/* ---------------------------------------------------------------------------
   Two-column layout
   --------------------------------------------------------------------------- */
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
@media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

/* ---------------------------------------------------------------------------
   Chart
   --------------------------------------------------------------------------- */
.chart-container { position: relative; height: 220px; width: 100%; }

/* ---------------------------------------------------------------------------
   Alerts
   --------------------------------------------------------------------------- */
.alert-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
  font-size: 0.84rem;
}
.alert-item:last-child { border-bottom: none; }
.alert-sev {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 0.7rem;
  font-weight: 700;
  text-transform: uppercase;
  flex-shrink: 0;
}
.sev-INFO { background: var(--blue-dim); color: var(--blue); }
.sev-WARNING { background: var(--yellow-dim); color: var(--yellow); }
.sev-CRITICAL { background: var(--red-dim); color: var(--red); }
.alert-time { color: var(--text-muted); font-size: 0.76rem; flex-shrink: 0; min-width: 70px; }

/* ---------------------------------------------------------------------------
   Empty states
   --------------------------------------------------------------------------- */
.empty { color: var(--text-muted); text-align: center; padding: 32px 16px; font-size: 0.88rem; }

/* ---------------------------------------------------------------------------
   Spinner
   --------------------------------------------------------------------------- */
.spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Footer */
.footer { text-align: center; padding: 16px; color: var(--text-muted); font-size: 0.76rem; }

/* Kill switch button */
.kill-btn {
  padding: 5px 14px;
  border: none;
  border-radius: 6px;
  font-size: 0.78rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.kill-btn-pause {
  background: var(--red);
  color: #fff;
}
.kill-btn-pause:hover { background: #dc2626; }
.kill-btn-resume {
  background: var(--green);
  color: #fff;
}
.kill-btn-resume:hover { background: #16a34a; }
.kill-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.paused-banner {
  background: var(--red-dim);
  color: var(--red);
  text-align: center;
  padding: 8px 16px;
  font-weight: 600;
  font-size: 0.85rem;
  border-bottom: 1px solid var(--red);
  display: none;
}
</style>
</head>
<body>

<!-- ====================================================================== -->
<!-- Header                                                                  -->
<!-- ====================================================================== -->
<div class="header">
  <h1>Polymarket Arb Scanner</h1>
  <div class="header-right">
    <span id="mode-badge" class="badge badge-dry"><span class="dot dot-yellow dot-pulse"></span> DRY RUN</span>
    <button id="kill-btn" class="kill-btn kill-btn-pause" onclick="toggleKillSwitch()">Pause Trading</button>
    <span id="uptime" style="color:var(--text-muted)"></span>
    <span id="last-scan" style="color:var(--text-muted)"></span>
    <span class="spinner" id="refresh-spinner" style="display:none"></span>
  </div>
</div>
<div class="paused-banner" id="paused-banner">TRADING PAUSED — Kill switch is engaged. Click Resume to re-enable trading.</div>

<!-- ====================================================================== -->
<!-- Main content                                                            -->
<!-- ====================================================================== -->
<div class="main">

  <!-- KPI Cards -->
  <div class="cards" id="kpi-cards">
    <div class="card">
      <div class="card-label">Daily P&L</div>
      <div class="card-value" id="kpi-daily-pnl">$0.00</div>
      <div class="card-sub" id="kpi-cumulative-pnl">Cumulative: $0.00</div>
    </div>
    <div class="card">
      <div class="card-label">Open Positions</div>
      <div class="card-value" id="kpi-positions">0</div>
      <div class="card-sub" id="kpi-positions-sub">&nbsp;</div>
    </div>
    <div class="card">
      <div class="card-label">Opportunities Found</div>
      <div class="card-value" id="kpi-opps">0</div>
      <div class="card-sub" id="kpi-scans">Scans: 0</div>
    </div>
    <div class="card">
      <div class="card-label">Trades Executed</div>
      <div class="card-value" id="kpi-trades">0</div>
      <div class="card-sub" id="kpi-trades-failed">Failed: 0</div>
    </div>
    <div class="card">
      <div class="card-label">Avg Slippage</div>
      <div class="card-value" id="kpi-slippage">--</div>
      <div class="card-sub" id="kpi-scan-latency">Scan latency: --</div>
    </div>
    <div class="card">
      <div class="card-label">WebSocket</div>
      <div class="card-value" id="kpi-ws">--</div>
      <div class="card-sub" id="kpi-ws-msgs">Messages: 0</div>
    </div>
  </div>

  <!-- Layer 2-5 Strategy Cards -->
  <div class="cards" id="layer-cards">
    <div class="card">
      <div class="card-label">Market Making</div>
      <div class="card-value" id="kpi-mm-markets">0</div>
      <div class="card-sub" id="kpi-mm-sub">Orders: 0 | Exposure: $0</div>
    </div>
    <div class="card">
      <div class="card-label">Stale Prices</div>
      <div class="card-value" id="kpi-stale">0</div>
      <div class="card-sub">Detected this scan</div>
    </div>
    <div class="card">
      <div class="card-label">Resolution Snipes</div>
      <div class="card-value" id="kpi-resolution">0</div>
      <div class="card-sub">Near-certain outcomes</div>
    </div>
    <div class="card">
      <div class="card-label">Convergence</div>
      <div class="card-value" id="kpi-convergence">0</div>
      <div class="card-sub">Outlier platforms</div>
    </div>
  </div>

  <!-- P&L Chart + Strategy Breakdown -->
  <div class="grid-2">
    <div class="section">
      <div class="section-header">P&L History (30 days)</div>
      <div class="section-body">
        <div class="chart-container"><canvas id="pnl-chart"></canvas></div>
      </div>
    </div>
    <div class="section">
      <div class="section-header">Strategy Breakdown</div>
      <div class="section-body no-pad">
        <table class="tbl" id="strategy-table">
          <thead><tr><th>Strategy</th><th class="right">Count</th><th class="right">Avg ROI</th><th class="right">Avg Profit</th><th class="right">Total Profit</th></tr></thead>
          <tbody id="strategy-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Per-Strategy P&L (MONITOR-01) + Platform Capital Balances (OPTIMIZE-04/05) -->
  <div class="grid-2">
    <div class="section">
      <div class="section-header">Per-Strategy P&amp;L</div>
      <div class="section-body">
        <div class="chart-container"><canvas id="strategyPnlChart"></canvas></div>
      </div>
      <div class="section-body no-pad">
        <table class="tbl" id="strategy-pnl-table">
          <thead><tr><th>Strategy</th><th class="right">Trades</th><th class="right">Win Rate</th><th class="right">Total P&amp;L</th><th class="right">Avg Profit</th></tr></thead>
          <tbody id="strategy-pnl-tbody"></tbody>
        </table>
      </div>
    </div>
    <div class="section">
      <div class="section-header">
        <span>Platform Capital Balances</span>
        <span id="balances-updated" style="color:var(--text-muted);font-size:0.76rem"></span>
      </div>
      <div class="section-body">
        <div class="chart-container"><canvas id="balancesChart"></canvas></div>
      </div>
      <div class="section-body no-pad">
        <table class="tbl" id="balances-table">
          <thead><tr><th>Platform</th><th class="right">Balance</th><th class="right">Current %</th><th class="right">Target %</th><th>Action</th></tr></thead>
          <tbody id="balances-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Open Positions -->
  <div class="section">
    <div class="section-header">
      <span>Open Positions</span>
      <span id="positions-count" style="color:var(--text-muted);font-size:0.82rem"></span>
    </div>
    <div class="section-body no-pad">
      <table class="tbl" id="positions-table">
        <thead><tr><th>Market</th><th>Platform</th><th class="right">Expected P&L</th><th>Entry Time</th><th>Status</th></tr></thead>
        <tbody id="positions-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Positions by Platform chart + Recent Trades -->
  <div class="grid-2">
    <div class="section">
      <div class="section-header">Positions by Platform</div>
      <div class="section-body">
        <div class="chart-container"><canvas id="platform-chart"></canvas></div>
      </div>
    </div>
    <div class="section">
      <div class="section-header">
        <span>Recent Trades</span>
        <span id="trades-count" style="color:var(--text-muted);font-size:0.82rem"></span>
      </div>
      <div class="section-body no-pad" style="max-height:320px;overflow-y:auto">
        <table class="tbl" id="trades-table">
          <thead><tr><th>Time</th><th>Platform</th><th>Side</th><th class="right">Price</th><th class="right">Fill</th><th class="right">Slippage</th><th>Status</th></tr></thead>
          <tbody id="trades-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Trade Failures -->
  <div class="grid-2">
    <div class="section">
      <div class="section-header">
        <span>Failure Rate (24h)</span>
        <span id="failure-rate-badge" style="color:var(--text-muted);font-size:0.82rem"></span>
      </div>
      <div class="section-body">
        <div class="chart-container"><canvas id="failure-chart"></canvas></div>
      </div>
    </div>
    <div class="section">
      <div class="section-header">
        <span>Failed Trades</span>
        <span id="failures-count" style="color:var(--text-muted);font-size:0.82rem"></span>
      </div>
      <div class="section-body no-pad" style="max-height:320px;overflow-y:auto">
        <table class="tbl" id="failures-table">
          <thead><tr><th>Time</th><th>Platform</th><th>Type</th><th>Market</th><th class="right">Price</th><th class="right">Size</th></tr></thead>
          <tbody id="failures-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Opportunity Feed -->
  <div class="section">
    <div class="section-header">
      <span>Recent Opportunities</span>
      <span id="opps-count" style="color:var(--text-muted);font-size:0.82rem"></span>
    </div>
    <div class="section-body no-pad" style="max-height:400px;overflow-y:auto">
      <table class="tbl" id="opps-table">
        <thead><tr><th>Time</th><th>Type</th><th>Market</th><th>Prices</th><th class="right">Cost</th><th class="right">Profit</th><th class="right">ROI</th><th class="right">Depth</th><th>Action</th></tr></thead>
        <tbody id="opps-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Alerts -->
  <div class="section">
    <div class="section-header">
      <span>Alerts</span>
      <span id="alerts-count" style="color:var(--text-muted);font-size:0.82rem"></span>
    </div>
    <div class="section-body" id="alerts-body">
      <div class="empty">No alerts</div>
    </div>
  </div>

  <!-- Strategy Leaderboard (MON-02) -->
  <div class="section">
    <div class="section-header">Strategy Leaderboard (7-Day Rolling)</div>
    <div class="section-body">
      <div id="leaderboard-container">
        <p id="leaderboard-loading">Loading strategy metrics...</p>
        <table id="leaderboard-table" class="tbl" style="display:none;">
          <thead>
            <tr>
              <th>Strategy</th>
              <th class="right">Trades</th>
              <th class="right">Wins</th>
              <th class="right">Win Rate</th>
              <th class="right">Total P&L</th>
              <th class="right">Avg P&L</th>
              <th class="right">Annual Sharpe</th>
              <th class="right">Max Drawdown</th>
            </tr>
          </thead>
          <tbody id="leaderboard-body">
            <!-- Strategy rows populated by JavaScript -->
          </tbody>
        </table>
        <!-- Rewards strategy row (separate table for visual distinction) -->
        <table class="tbl" style="margin-top:12px;">
          <thead>
            <tr>
              <th>Strategy</th>
              <th class="right">Trading P&L</th>
              <th class="right">Daily Yield</th>
              <th class="right">Total P&L</th>
              <th class="right">Resting Orders</th>
              <th class="right">Status</th>
            </tr>
          </thead>
          <tbody>
            <tr id="rewards-row" data-strategy="Rewards" style="background-color:var(--blue-dim);">
              <td>Liquidity Rewards</td>
              <td id="rewards-trading-pnl" class="mono right">$0.00</td>
              <td id="rewards-yield-daily" class="mono right">$0.00/day</td>
              <td id="rewards-total-pnl" class="mono right">$0.00</td>
              <td id="rewards-resting-count" class="right">0</td>
              <td id="rewards-status" class="right">—</td>
            </tr>
          </tbody>
        </table>
        <p id="leaderboard-empty" style="display:none;" class="empty">No strategy data available</p>
      </div>
      <p id="leaderboard-timestamp" style="font-size:0.85rem; color:var(--text-muted); margin-top:8px;">—</p>
    </div>
  </div>

</div>

<div class="footer">
  Auto-refreshing every <span id="refresh-interval">__REFRESH_SECONDS__</span>s
  &middot; Last updated: <span id="last-updated">--</span>
</div>

<!-- ====================================================================== -->
<!-- JavaScript                                                              -->
<!-- ====================================================================== -->
<script>
const REFRESH = __REFRESH_SECONDS__ * 1000;
let pnlChart = null;
let platformChart = null;
let strategyPnlChart = null;
let balancesChart = null;

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------
async function api(path) {
  try {
    const r = await fetch(path);
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

function $(id) { return document.getElementById(id); }

function fmtUSD(v) {
  if (v == null) return '--';
  const n = parseFloat(v);
  const sign = n >= 0 ? '' : '-';
  return sign + '$' + Math.abs(n).toFixed(4);
}
function fmtPct(v) {
  if (v == null) return '--';
  return (parseFloat(v) * 100).toFixed(2) + '%';
}
function fmtTime(iso) {
  if (!iso) return '--';
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function fmtDate(iso) {
  if (!iso) return '--';
  return iso.substring(0, 10);
}
function fmtDuration(sec) {
  if (!sec && sec !== 0) return '--';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h > 0) return h + 'h ' + m + 'm';
  if (m > 0) return m + 'm';
  return Math.floor(sec) + 's';
}
function pnlClass(v) { return parseFloat(v) >= 0 ? 'positive' : 'negative'; }
function statusClass(s) {
  s = (s || '').toLowerCase();
  if (s === 'filled') return 'status-filled';
  if (s === 'pending') return 'status-pending';
  if (s === 'failed' || s === 'orphaned') return 'status-failed';
  if (s === 'open') return 'status-open';
  if (s === 'dry_run') return 'status-dry_run';
  return '';
}
function truncate(s, n) { return s && s.length > n ? s.substring(0, n) + '...' : s; }

// ---------------------------------------------------------------------------
// DOM helpers — safe construction (createElement / textContent for XSS safety)
// ---------------------------------------------------------------------------
function makeCell(text, opts) {
  const td = document.createElement('td');
  if (opts && opts.className) td.className = opts.className;
  if (opts && opts.title != null) td.title = String(opts.title);
  td.textContent = text == null ? '--' : String(text);
  return td;
}
function makeStatusCell(statusText, klass) {
  const td = document.createElement('td');
  const span = document.createElement('span');
  span.className = 'status-badge ' + (klass || '');
  span.textContent = statusText == null ? '--' : String(statusText);
  td.appendChild(span);
  return td;
}
function setEmpty(container, colspan, msg) {
  const tr = document.createElement('tr');
  const td = document.createElement('td');
  td.colSpan = colspan;
  td.className = 'empty';
  td.textContent = msg;
  tr.appendChild(td);
  container.replaceChildren(tr);
}
function setEmptyDiv(container, msg) {
  const div = document.createElement('div');
  div.className = 'empty';
  div.textContent = msg;
  container.replaceChildren(div);
}

// ---------------------------------------------------------------------------
// P&L chart
// ---------------------------------------------------------------------------
function initPnlChart() {
  const ctx = $('pnl-chart').getContext('2d');
  pnlChart = new Chart(ctx, {
    type: 'bar',
    data: { labels: [], datasets: [{
      label: 'Daily P&L',
      data: [],
      backgroundColor: [],
      borderRadius: 3,
      barPercentage: 0.7,
    }]},
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#8b8fa3', font: { size: 11 } }, grid: { display: false } },
        y: {
          ticks: { color: '#8b8fa3', font: { size: 11 }, callback: v => '$' + v.toFixed(2) },
          grid: { color: '#2d3148' },
        },
      },
    },
  });
}

function updatePnlChart(history) {
  if (!pnlChart || !history) return;
  pnlChart.data.labels = history.map(d => d.date.substring(5)); // MM-DD
  pnlChart.data.datasets[0].data = history.map(d => d.pnl);
  pnlChart.data.datasets[0].backgroundColor = history.map(d => d.pnl >= 0 ? '#22c55e' : '#ef4444');
  pnlChart.update('none');
}

// ---------------------------------------------------------------------------
// Platform chart
// ---------------------------------------------------------------------------
function initPlatformChart() {
  const ctx = $('platform-chart').getContext('2d');
  const colors = ['#6366f1','#3b82f6','#22c55e','#eab308','#ef4444','#ec4899','#f97316','#14b8a6'];
  platformChart = new Chart(ctx, {
    type: 'doughnut',
    data: { labels: [], datasets: [{ data: [], backgroundColor: colors, borderWidth: 0 }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '65%',
      plugins: {
        legend: { position: 'right', labels: { color: '#e4e6f0', font: { size: 12 }, padding: 12 } },
      },
    },
  });
}

function updatePlatformChart(platforms) {
  if (!platformChart || !platforms || platforms.length === 0) {
    if (platformChart) {
      platformChart.data.labels = ['No positions'];
      platformChart.data.datasets[0].data = [1];
      platformChart.data.datasets[0].backgroundColor = ['#2d3148'];
      platformChart.update('none');
    }
    return;
  }
  platformChart.data.labels = platforms.map(p => p.platform);
  platformChart.data.datasets[0].data = platforms.map(p => p.count);
  platformChart.update('none');
}

// ---------------------------------------------------------------------------
// Per-Strategy P&L chart (MONITOR-01)
// ---------------------------------------------------------------------------
function initStrategyPnlChart() {
  const ctx = $('strategyPnlChart').getContext('2d');
  strategyPnlChart = new Chart(ctx, {
    type: 'bar',
    data: { labels: [], datasets: [{
      label: 'Total P&L',
      data: [],
      backgroundColor: [],
      borderRadius: 3,
      barPercentage: 0.7,
    }]},
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: {
          ticks: { color: '#8b8fa3', font: { size: 11 }, callback: v => '$' + v.toFixed(2) },
          grid: { color: '#2d3148' },
        },
        y: { ticks: { color: '#8b8fa3', font: { size: 11 } }, grid: { display: false } },
      },
    },
  });
}

function updateStrategyPnlChart(data) {
  if (!strategyPnlChart || !data) return;
  const strategies = data.strategies || [];
  strategyPnlChart.data.labels = strategies.map(s => s.strategy);
  strategyPnlChart.data.datasets[0].data = strategies.map(s => s.total_pnl);
  strategyPnlChart.data.datasets[0].backgroundColor = strategies.map(s =>
    s.total_pnl >= 0 ? '#22c55e' : '#ef4444'
  );
  strategyPnlChart.update('none');
}

function renderStrategyPnlTable(data) {
  const tbody = $('strategy-pnl-tbody');
  const strategies = (data && data.strategies) ? data.strategies : [];
  if (strategies.length === 0) {
    setEmpty(tbody, 5, 'No trade data yet');
    return;
  }
  const rows = strategies.map(s => {
    const winRate = s.trade_count > 0 ? ((s.win_count / s.trade_count) * 100).toFixed(1) + '%' : '--';
    const tr = document.createElement('tr');
    tr.appendChild(makeCell(s.strategy));
    tr.appendChild(makeCell(s.trade_count, { className: 'mono right' }));
    tr.appendChild(makeCell(winRate, { className: 'mono right' }));
    tr.appendChild(makeCell(fmtUSD(s.total_pnl), { className: 'mono right ' + pnlClass(s.total_pnl) }));
    tr.appendChild(makeCell(fmtUSD(s.avg_profit), { className: 'mono right ' + pnlClass(s.avg_profit) }));
    return tr;
  });
  tbody.replaceChildren(...rows);
}

// ---------------------------------------------------------------------------
// Platform Balances chart (OPTIMIZE-04, OPTIMIZE-05)
// ---------------------------------------------------------------------------
function initBalancesChart() {
  const ctx = $('balancesChart').getContext('2d');
  const colors = ['#6366f1','#3b82f6','#22c55e','#eab308','#ef4444','#ec4899','#f97316','#14b8a6'];
  balancesChart = new Chart(ctx, {
    type: 'doughnut',
    data: { labels: [], datasets: [{ data: [], backgroundColor: colors, borderWidth: 0 }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '65%',
      plugins: {
        legend: { position: 'right', labels: { color: '#e4e6f0', font: { size: 12 }, padding: 12 } },
      },
    },
  });
}

function updateBalancesChart(data) {
  if (!balancesChart || !data) return;
  const balances = data.balances || {};
  const platforms = Object.keys(balances);
  if (platforms.length === 0) {
    balancesChart.data.labels = ['No data'];
    balancesChart.data.datasets[0].data = [1];
    balancesChart.data.datasets[0].backgroundColor = ['#2d3148'];
    balancesChart.update('none');
    return;
  }
  balancesChart.data.labels = platforms;
  balancesChart.data.datasets[0].data = platforms.map(p => balances[p]);
  balancesChart.update('none');
  const ts = $('balances-updated');
  if (ts) ts.textContent = data.last_updated ? 'Updated: ' + fmtTime(data.last_updated) : '';
}

function renderBalancesTable(balancesData, rebalanceData) {
  const tbody = $('balances-tbody');
  const balances = (balancesData && balancesData.balances) ? balancesData.balances : {};
  const total = (balancesData && balancesData.total) ? balancesData.total : 0;
  const recs = (rebalanceData && rebalanceData.recommendations) ? rebalanceData.recommendations : [];
  const recsByPlatform = {};
  recs.forEach(r => { recsByPlatform[r.platform] = r; });

  const platforms = Object.keys(balances);
  if (platforms.length === 0) {
    setEmpty(tbody, 5, 'No balance data');
    return;
  }
  const rows = platforms.map(p => {
    const bal = balances[p];
    const pct = total > 0 ? ((bal / total) * 100).toFixed(1) + '%' : '--';
    const rec = recsByPlatform[p];
    const recPct = rec ? (rec.recommended_pct * 100).toFixed(1) + '%' : pct;
    const tr = document.createElement('tr');
    tr.appendChild(makeCell(p));
    tr.appendChild(makeCell('$' + parseFloat(bal).toFixed(2), { className: 'mono right' }));
    tr.appendChild(makeCell(pct, { className: 'mono right' }));
    tr.appendChild(makeCell(recPct, { className: 'mono right' }));
    const actionTd = document.createElement('td');
    if (rec) {
      const amt = rec.transfer_amount;
      const span = document.createElement('span');
      if (amt > 0) {
        span.className = 'positive';
        span.textContent = '↑ Receive $' + Math.abs(amt).toFixed(2);
      } else {
        span.className = 'negative';
        span.textContent = '↓ Send $' + Math.abs(amt).toFixed(2);
      }
      actionTd.appendChild(span);
    } else {
      actionTd.textContent = '--';
    }
    tr.appendChild(actionTd);
    return tr;
  });
  tbody.replaceChildren(...rows);
}

// ---------------------------------------------------------------------------
// Render functions
// ---------------------------------------------------------------------------
function renderStatus(data) {
  if (!data) return;
  $('kpi-daily-pnl').textContent = fmtUSD(data.daily_pnl);
  $('kpi-daily-pnl').className = 'card-value ' + pnlClass(data.daily_pnl);
  $('kpi-positions').textContent = data.open_positions;
  $('kpi-opps').textContent = data.opportunities_found;
  $('kpi-scans').textContent = 'Scans: ' + data.scan_count;
  $('last-scan').textContent = data.last_scan_time ? 'Last scan: ' + fmtTime(data.last_scan_time) : '';
  const wsEl = $('kpi-ws');
  while (wsEl.firstChild) wsEl.removeChild(wsEl.firstChild);
  const wsSpan = document.createElement('span');
  wsSpan.className = data.ws_connections > 0 ? 'positive' : 'negative';
  wsSpan.textContent = data.ws_connections > 0 ? 'Connected' : 'Disconnected';
  wsEl.appendChild(wsSpan);
  // Layer 2-5 cards
  $('kpi-mm-markets').textContent = data.mm_active_markets || 0;
  $('kpi-mm-sub').textContent = 'Orders: ' + (data.mm_active_orders || 0) + ' | Exposure: $' + (data.mm_total_exposure || 0).toFixed(0);
  $('kpi-stale').textContent = data.stale_detections || 0;
  $('kpi-resolution').textContent = data.resolution_snipes || 0;
  $('kpi-convergence').textContent = data.convergence_signals || 0;
}

function renderHealth(data) {
  if (!data) return;
  // Mode badge
  const badge = $('mode-badge');
  const dotClass = data.dry_run ? 'dot-yellow' : 'dot-green';
  const label = data.dry_run ? 'DRY RUN' : 'LIVE';
  badge.className = 'badge ' + (data.dry_run ? 'badge-dry' : 'badge-live');
  const dot = document.createElement('span');
  dot.className = 'dot ' + dotClass + ' dot-pulse';
  badge.replaceChildren(dot, document.createTextNode(' ' + label));
  // Uptime
  $('uptime').textContent = 'Up ' + fmtDuration(data.uptime_seconds);
  // Metrics
  const m = data.metrics || {};
  const counters = m.counters || {};
  const gauges = m.gauges || {};
  $('kpi-trades').textContent = counters.trades_executed || 0;
  $('kpi-trades-failed').textContent = 'Failed: ' + (counters.trades_failed || 0);
  $('kpi-ws-msgs').textContent = 'Messages: ' + (counters.ws_messages_received || 0);
  const latency = gauges.scan_cycle_duration_seconds;
  $('kpi-scan-latency').textContent = 'Scan latency: ' + (latency != null ? latency.toFixed(2) + 's' : '--');
}

function renderSlippage(data) {
  if (!data) return;
  const avg = data.avg_slippage;
  $('kpi-slippage').textContent = avg != null && avg !== 0 ? (avg * 100).toFixed(3) + '%' : '--';
}

function renderCumulative(data) {
  if (data == null) return;
  const v = data.cumulative_pnl;
  const cumEl = $('kpi-cumulative-pnl');
  const cumSpan = document.createElement('span');
  cumSpan.className = pnlClass(v);
  cumSpan.textContent = fmtUSD(v);
  cumEl.replaceChildren(document.createTextNode('Cumulative: '), cumSpan);
}

function renderStrategies(data) {
  const tbody = $('strategy-tbody');
  if (!data || data.length === 0) {
    setEmpty(tbody, 5, 'No data yet');
    return;
  }
  const rows = data.map(s => {
    const tr = document.createElement('tr');
    tr.appendChild(makeCell(s.type));
    tr.appendChild(makeCell(s.count, { className: 'mono right' }));
    tr.appendChild(makeCell(fmtPct(s.avg_roi), { className: 'mono right' }));
    tr.appendChild(makeCell(fmtUSD(s.avg_profit), { className: 'mono right' }));
    tr.appendChild(makeCell(fmtUSD(s.total_profit), { className: 'mono right ' + pnlClass(s.total_profit) }));
    return tr;
  });
  tbody.replaceChildren(...rows);
}

function renderPositions(data) {
  const tbody = $('positions-tbody');
  if (!data || data.length === 0) {
    setEmpty(tbody, 5, 'No open positions');
    $('positions-count').textContent = '';
    return;
  }
  $('positions-count').textContent = data.length + ' position' + (data.length !== 1 ? 's' : '');
  const rows = data.map(p => {
    const tr = document.createElement('tr');
    tr.appendChild(makeCell(truncate(p.market_identifier || '--', 60), { title: p.market_identifier || '' }));
    tr.appendChild(makeCell(p.platform || '--'));
    tr.appendChild(makeCell(fmtUSD(p.expected_pnl), { className: 'mono right ' + pnlClass(p.expected_pnl) }));
    tr.appendChild(makeCell(fmtTime(p.entry_timestamp)));
    tr.appendChild(makeStatusCell(p.status || '--', statusClass(p.status)));
    return tr;
  });
  tbody.replaceChildren(...rows);
}

function renderTrades(data) {
  const tbody = $('trades-tbody');
  if (!data || data.length === 0) {
    setEmpty(tbody, 7, 'No trades yet');
    $('trades-count').textContent = '';
    return;
  }
  $('trades-count').textContent = data.length + ' recent';
  const rows = data.slice(0, 50).map(t => {
    const slip = t.slippage != null ? (t.slippage * 100).toFixed(3) + '%' : '--';
    const tr = document.createElement('tr');
    tr.appendChild(makeCell(fmtTime(t.timestamp)));
    tr.appendChild(makeCell(t.platform));
    tr.appendChild(makeCell(t.side));
    tr.appendChild(makeCell(t.price != null ? t.price.toFixed(4) : '--', { className: 'mono right' }));
    tr.appendChild(makeCell(t.fill_price != null ? t.fill_price.toFixed(4) : '--', { className: 'mono right' }));
    tr.appendChild(makeCell(slip, { className: 'mono right' }));
    tr.appendChild(makeStatusCell(t.status, statusClass(t.status)));
    return tr;
  });
  tbody.replaceChildren(...rows);
}

function renderOpportunities(data) {
  const tbody = $('opps-tbody');
  if (!data || data.length === 0) {
    setEmpty(tbody, 9, 'No opportunities detected yet');
    $('opps-count').textContent = '';
    return;
  }
  $('opps-count').textContent = data.length + ' recent';
  const rows = data.slice(0, 100).map(o => {
    const tr = document.createElement('tr');
    tr.appendChild(makeCell(fmtTime(o.timestamp)));
    tr.appendChild(makeCell(o.type || '--'));
    tr.appendChild(makeCell(truncate(o.market || '--', 45), { title: o.market || '' }));
    tr.appendChild(makeCell(truncate(o.prices || '--', 30), { className: 'mono', title: o.prices || '' }));
    tr.appendChild(makeCell(fmtUSD(o.total_cost), { className: 'mono right' }));
    tr.appendChild(makeCell(fmtUSD(o.net_profit), { className: 'mono right ' + pnlClass(o.net_profit) }));
    tr.appendChild(makeCell(fmtPct(o.net_roi), { className: 'mono right' }));
    tr.appendChild(makeCell(o.depth != null ? '$' + parseFloat(o.depth).toFixed(2) : '--', { className: 'mono right' }));
    tr.appendChild(makeStatusCell(o.action || '--', statusClass(o.action)));
    return tr;
  });
  tbody.replaceChildren(...rows);
}

function renderAlerts(data) {
  const body = $('alerts-body');
  if (!data || data.length === 0) {
    setEmptyDiv(body, 'No alerts');
    $('alerts-count').textContent = '';
    return;
  }
  $('alerts-count').textContent = data.length + ' alert' + (data.length !== 1 ? 's' : '');
  const items = data.slice(0, 30).map(a => {
    const div = document.createElement('div');
    div.className = 'alert-item';
    const sev = document.createElement('span');
    sev.className = 'alert-sev sev-' + a.severity;
    sev.textContent = a.severity;
    div.appendChild(sev);
    const time = document.createElement('span');
    time.className = 'alert-time';
    time.textContent = fmtTime(a.timestamp);
    div.appendChild(time);
    const msg = document.createElement('span');
    msg.textContent = a.message || a.type;
    div.appendChild(msg);
    return div;
  });
  body.replaceChildren(...items);
}

// ---------------------------------------------------------------------------
// Failure chart + render
// ---------------------------------------------------------------------------
let failureChart = null;

function initFailureChart() {
  const ctx = $('failure-chart').getContext('2d');
  failureChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: 'Failed',
        data: [],
        borderColor: '#ef4444',
        backgroundColor: 'rgba(239,68,68,0.15)',
        fill: true,
        tension: 0.3,
        pointRadius: 3,
        pointBackgroundColor: '#ef4444',
      }, {
        label: 'Total',
        data: [],
        borderColor: '#6366f1',
        backgroundColor: 'transparent',
        borderDash: [4, 4],
        tension: 0.3,
        pointRadius: 2,
        pointBackgroundColor: '#6366f1',
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#8b8fa3', font: { size: 11 } } },
      },
      scales: {
        x: { ticks: { color: '#8b8fa3', font: { size: 11 } }, grid: { display: false } },
        y: {
          beginAtZero: true,
          ticks: { color: '#8b8fa3', font: { size: 11 }, stepSize: 1 },
          grid: { color: '#2d3148' },
        },
      },
    },
  });
}

function updateFailureChart(byHour) {
  if (!failureChart || !byHour) return;
  failureChart.data.labels = byHour.map(h => {
    const d = new Date(h.hour);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  });
  failureChart.data.datasets[0].data = byHour.map(h => h.failed);
  failureChart.data.datasets[1].data = byHour.map(h => h.total);
  failureChart.update('none');
}

function renderFailures(data) {
  if (!data) return;
  const stats = data.stats || {};
  const trades = data.trades || [];

  // Update rate badge
  const rate = stats.failure_rate != null ? (stats.failure_rate * 100).toFixed(1) + '%' : '--';
  const badge = $('failure-rate-badge');
  badge.textContent = 'Overall: ' + (stats.total_failed || 0) + '/' + (stats.total_trades || 0) + ' (' + rate + ')';
  if (stats.failure_rate > 0.1) badge.style.color = 'var(--red)';
  else if (stats.failure_rate > 0.05) badge.style.color = 'var(--yellow)';
  else badge.style.color = 'var(--text-muted)';

  // Update chart
  updateFailureChart(stats.by_hour || []);

  // Update table
  const tbody = $('failures-tbody');
  if (trades.length === 0) {
    setEmpty(tbody, 6, 'No failed trades');
    $('failures-count').textContent = '';
    return;
  }
  $('failures-count').textContent = trades.length + ' recent';
  const rows = trades.slice(0, 50).map(t => {
    const tr = document.createElement('tr');
    tr.appendChild(makeCell(fmtTime(t.timestamp)));
    tr.appendChild(makeCell(t.platform || '--'));
    tr.appendChild(makeCell(t.opp_type || '--'));
    tr.appendChild(makeCell(truncate(t.opp_market || '--', 35), { title: t.opp_market || '' }));
    tr.appendChild(makeCell(t.price != null ? t.price.toFixed(4) : '--', { className: 'mono right' }));
    tr.appendChild(makeCell(t.size != null ? t.size.toFixed(2) : '--', { className: 'mono right' }));
    return tr;
  });
  tbody.replaceChildren(...rows);
}

// ---------------------------------------------------------------------------
// Kill switch
// ---------------------------------------------------------------------------
let _killSwitchState = { paused: false };

function renderKillSwitch(data) {
  if (!data) return;
  _killSwitchState = data;
  const btn = $('kill-btn');
  const banner = $('paused-banner');
  if (data.paused) {
    btn.className = 'kill-btn kill-btn-resume';
    btn.textContent = 'Resume Trading';
    banner.style.display = 'block';
  } else {
    btn.className = 'kill-btn kill-btn-pause';
    btn.textContent = 'Pause Trading';
    banner.style.display = 'none';
  }
}

async function toggleKillSwitch() {
  const btn = $('kill-btn');
  btn.disabled = true;
  try {
    const endpoint = _killSwitchState.paused ? '/api/resume' : '/api/pause';
    const r = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (r.ok) {
      const data = await r.json();
      renderKillSwitch(data);
    }
  } catch (e) {
    console.error('Kill switch toggle failed:', e);
  }
  btn.disabled = false;
}

// ---------------------------------------------------------------------------
// Strategy Leaderboard (MON-02)
// ---------------------------------------------------------------------------
function formatPercent(val) {
  return val === null || val === undefined || val === 'N/A' ? 'N/A' : (parseFloat(val) * 100).toFixed(1) + '%';
}

function formatCurrency(val) {
  return val === null || val === undefined || val === 'N/A' ? 'N/A' : '$' + parseFloat(val).toFixed(4);
}

function formatSharpe(val) {
  return val === null || val === undefined || val === 'N/A' ? 'N/A' : parseFloat(val).toFixed(3);
}

function renderLeaderboard(data) {
  const tbody = $('leaderboard-body');
  const table = $('leaderboard-table');
  const loading = $('leaderboard-loading');
  const empty = $('leaderboard-empty');
  const timestamp = $('leaderboard-timestamp');

  // Clear tbody safely
  while (tbody.firstChild) {
    tbody.removeChild(tbody.firstChild);
  }

  if (!data || !data.strategies || data.strategies.length === 0) {
    empty.style.display = 'block';
    table.style.display = 'none';
    loading.style.display = 'none';
    timestamp.textContent = '—';
    return;
  }

  data.strategies.forEach(strategy => {
    const row = document.createElement('tr');

    const cells = [
      strategy.strategy || 'unknown',
      String(strategy.trade_count || 0),
      String(strategy.wins || 0),
      formatPercent(strategy.win_rate),
      formatCurrency(strategy.total_pnl),
      formatCurrency(strategy.avg_pnl),
      formatSharpe(strategy.annual_sharpe),
      formatCurrency(strategy.max_drawdown)
    ];

    cells.forEach(cellText => {
      const cell = document.createElement('td');
      cell.textContent = cellText;
      if (cellText.startsWith('$') || cellText.endsWith('%')) {
        cell.className = 'mono right';
      }
      row.appendChild(cell);
    });

    tbody.appendChild(row);
  });

  table.style.display = 'table';
  loading.style.display = 'none';
  empty.style.display = 'none';

  // Update timestamp
  if (data.timestamp) {
    const lastUpdated = new Date(data.timestamp * 1000).toLocaleTimeString();
    timestamp.textContent = 'Last updated: ' + lastUpdated;
  }
}

// ---------------------------------------------------------------------------
// Rewards row update
// ---------------------------------------------------------------------------
function updateRewardsRow(status) {
  if (!status || !status.rewards) return;

  const rewards = status.rewards;
  $('rewards-trading-pnl').textContent = fmtUSD(rewards.trading_pnl || 0);
  $('rewards-yield-daily').textContent = fmtUSD(rewards.estimated_daily_yield_usdc || 0) + '/day';
  $('rewards-total-pnl').textContent = fmtUSD((rewards.trading_pnl || 0) + (rewards.estimated_daily_yield_usdc || 0));
  $('rewards-resting-count').textContent = rewards.resting_order_count || 0;
  $('rewards-status').textContent = (rewards.resting_order_count || 0) > 0 ? 'Active' : '—';
}

// ---------------------------------------------------------------------------
// Main refresh loop
// ---------------------------------------------------------------------------
async function refresh() {
  const spinner = $('refresh-spinner');
  spinner.style.display = 'inline-block';

  const [status, health, slippage, history, strategies, positions,
         platforms, trades, opportunities, alerts, pauseState, failures,
         strategyPnl, balancesData, rebalanceData, leaderboardData] = await Promise.all([
    api('/status'),
    api('/api/health'),
    api('/api/slippage'),
    api('/api/history'),
    api('/api/strategies'),
    api('/api/positions'),
    api('/api/platforms'),
    api('/api/trades'),
    api('/api/opportunities'),
    api('/alerts'),
    api('/api/pause'),
    api('/api/failures'),
    api('/api/strategy-pnl'),
    api('/api/balances'),
    api('/api/rebalance'),
    api('/api/strategy-leaderboard'),
  ]);

  renderStatus(status);
  updateRewardsRow(status);
  renderHealth(health);
  renderSlippage(slippage);
  renderCumulative(health);
  renderStrategies(strategies);
  renderPositions(positions);
  updatePlatformChart(platforms);
  renderTrades(trades);
  renderOpportunities(opportunities);
  renderAlerts(alerts);
  updatePnlChart(history);
  renderKillSwitch(pauseState);
  renderFailures(failures);
  updateStrategyPnlChart(strategyPnl);
  renderStrategyPnlTable(strategyPnl);
  updateBalancesChart(balancesData);
  renderBalancesTable(balancesData, rebalanceData);
  renderLeaderboard(leaderboardData);

  // Positions subtitle
  if (platforms && platforms.length > 0) {
    $('kpi-positions-sub').textContent = platforms.map(p => p.platform + ': ' + p.count).join(', ');
  }

  $('last-updated').textContent = new Date().toLocaleTimeString();
  spinner.style.display = 'none';
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  initPnlChart();
  initPlatformChart();
  initFailureChart();
  initStrategyPnlChart();
  initBalancesChart();
  refresh();
  setInterval(refresh, REFRESH);
});
</script>
</body>
</html>"""
