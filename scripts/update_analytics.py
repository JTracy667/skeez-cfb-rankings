#!/usr/bin/env python3
"""
Update analytics.html to add:
1. 'Projections & Odds' tab with multi-factor composite scores + live betting lines
2. Team mascot names in display
3. Summary cards for top projections
"""

path = 'C:/Users/JeffTracy/Desktop/cfb-power-rankings/analytics.html'

with open(path, 'r') as f:
    content = f.read()

# 1) Add new tab button after 'composite'
old_composite_btn = '<button class="tab-btn" data-tab="composite">Composite</button>'
new_tabs = '''<button class="tab-btn" data-tab="composite">Composite</button>
    <button class="tab-btn" data-tab="projections">Projections & Odds</button>'''
content = content.replace(old_composite_btn, new_tabs)

# 2) Add new metric section div after composite div
old_composite_div = '<div id="composite" class="metric-section"></div>'
new_divs = '''<div id="composite" class="metric-section"></div>
    <div id="projections" class="metric-section"></div>'''
content = content.replace(old_composite_div, new_divs)

# 3) Add CSS for new elements (before </style>)
new_css = '''
.projections-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
@media (max-width: 1024px) { .projections-grid { grid-template-columns: 1fr; } }
.proj-card {
  background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 16px;
}
.proj-card h3 {
  font-size: 0.85rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px;
  margin-bottom: 10px; display: flex; align-items: center; gap: 8px;
}
.proj-card h3 .icon { font-size: 1rem; }
.proj-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 0; border-bottom: 1px solid #21262d; font-size: 0.82rem;
}
.proj-row:last-child { border-bottom: none; }
.proj-row .rank { color: #58a6ff; font-weight: 700; width: 30px; text-align: center; }
.proj-row .team { flex: 1; font-weight: 600; padding-left: 8px; }
.proj-row .score { color: #3fb950; font-weight: 700; width: 60px; text-align: right; }
.proj-row .prob { color: #d2a8ff; width: 55px; text-align: right; }
.odds-table { width: 100%; border-collapse: collapse; }
.odds-table th, .odds-table td { padding: 8px 10px; text-align: center; font-size: 0.8rem; border-bottom: 1px solid #21262d; }
.odds-table th { background: #1c2129; color: #8b949e; font-weight: 600; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.5px; }
.odds-table tr:hover td { background: rgba(31,111,235,0.06); }
.spread-val { font-weight: 700; }
.spread-val.negative { color: #f85149; }
.spread-val.positive { color: #3fb950; }
.total-val { color: #f0b232; font-weight: 600; }
.odds-source { color: #484f58; font-size: 0.7rem; }
.mascot-name { color: #c9d1d9; font-weight: 600; }
'''
content = content.replace('</style>', new_css + '</style>')

# 4) Add JavaScript for projections + odds (before the closing </script>)
new_js = '''
// ── Projections & Odds Data ──
let projections = [];
let odds = [];

async function fetchProjectionsAndOdds() {
  try {
    const [projRes, oddsRes] = await Promise.all([
      fetch(`${API}/projections`),
      fetch(`${API}/odds`)
    ]);
    if (projRes.ok) {
      const projData = await projRes.json();
      projections = projData.projections || [];
    }
    if (oddsRes.ok) {
      const oddsData = await oddsRes.json();
      odds = oddsData.odds || [];
    }
    document.getElementById('sourceBadge').textContent = `cfbd · ${teams.length} teams · ${odds.length} odds`;
    renderProjections();
  } catch (e) {
    console.error('Projections/Odds load failed:', e);
  }
}

// Mascot name mapping for display
const MASCOT_NAMES = {};

function getMascotName(name) {
  if (MASCOT_NAMES[name]) return MASCOT_NAMES[name];
  return name;
}

// ── Render Projections & Odds ──
function renderProjections() {
  const container = document.getElementById('projections');
  if (!projections.length && !odds.length) {
    container.innerHTML = '<div class="loading">No projection or odds data available.</div>';
    return;
  }

  let html = '<div class="projections-grid">';

  // Left column: Top 15 Projections
  html += '<div class="proj-card">';
  html += '<h3><span class="icon">🎯</span> Multi-Factor Projections (Top 15)</h3>';
  const sorted = [...projections].sort((a, b) => b.home_projection.composite - a.home_projection.composite);
  sorted.slice(0, 15).forEach((t, i) => {
    const hp = t.home_projection;
    const comp = hp.composite.toFixed(1);
    const proj = hp.projected_score.toFixed(1);
    const winp = hp.win_probability.toFixed(1);
    html += `<div class="proj-row">
      <span class="rank">${i+1}</span>
      <span class="team mascot-name">${getMascotName(t.name)}</span>
      <span class="score" title="Composite">${comp}</span>
      <span class="score" title="Projected Score">${proj}</span>
      <span class="prob" title="Win %">${winp}%</span>
    </div>`;
  });
  html += '</div>';

  // Right column: Bottom 10 Projections
  html += '<div class="proj-card">';
  html += '<h3><span class="icon">📉</span> Projections (Bottom 10)</h3>';
  sorted.slice(-10).reverse().forEach((t, i) => {
    const hp = t.home_projection;
    const comp = hp.composite.toFixed(1);
    const proj = hp.projected_score.toFixed(1);
    const winp = hp.win_probability.toFixed(1);
    html += `<div class="proj-row">
      <span class="rank">${projections.length - i}</span>
      <span class="team mascot-name">${getMascotName(t.name)}</span>
      <span class="score">${comp}</span>
      <span class="score">${proj}</span>
      <span class="prob">${winp}%</span>
    </div>`;
  });
  html += '</div>';

  html += '</div>'; // end grid

  // Full odds table
  if (odds.length) {
    html += '<div class="proj-card" style="margin-top: 16px;">';
    html += '<h3><span class="icon">🏈</span> Live Betting Lines</h3>';
    html += '<table class="odds-table"><thead><tr>';
    html += '<th>Home</th><th>Spread</th><th>Total</th><th>Away</th><th>Source</th>';
    html += '</tr></thead><tbody>';
    odds.forEach(o => {
      const spread = o.spread || '—';
      const total = o.total || '—';
      const spreadClass = typeof spread === 'number' ? (spread < 0 ? 'spread-val negative' : spread > 0 ? 'spread-val positive' : 'spread-val') : '';
      html += `<tr>
        <td class="mascot-name">${getMascotName(o.home)}</td>
        <td class="${spreadClass}">${spread}</td>
        <td class="total-val">${total}</td>
        <td class="mascot-name">${getMascotName(o.away)}</td>
        <td class="odds-source">${o.source || 'the_odds_api'}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
  }

  container.innerHTML = html;
}

// Patch renderAll to also call fetchProjectionsAndOdds
const origRenderAll = renderAll;
renderAll = function() {
  origRenderAll();
  renderProjections();
};

// Patch fetchAnalytics to also fetch projections/odds
const origFetchAnalytics = fetchAnalytics;
fetchAnalytics = async function() {
  await origFetchAnalytics();
  await fetchProjectionsAndOdds();
};
'''

# Insert before </script>
content = content.replace('</script>', new_js + '\n</script>')

with open(path, 'w') as f:
    f.write(content)

print(f"analytics.html updated! Size: {len(content)} chars")
