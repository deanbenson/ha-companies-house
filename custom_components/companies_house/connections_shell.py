"""The connections map web page: a fixed shell that loads ``connections.json``.

The page never changes, so the 31 day cache Home Assistant puts on ``/local``
files does not matter: the data is fetched with ``cache: "no-store"`` every
time the page opens. Everything is inline (styles, a small force layout on
SVG) so it works offline, in the companion app and inside a webpage card,
and makes no request to anything but Home Assistant itself.
"""

from __future__ import annotations

SHELL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connections</title>
<style>
  :root { --own:#1f4e79; --watched:#2563eb; --followed:#0f766e; --external:#9ca3af;
          --risk:#b91c1c; --psc:#7c3aed; --charge:#b45309; --ink:#111827; --muted:#6b7280; }
  * { box-sizing:border-box; }
  [hidden] { display:none !important; }
  html, body { margin:0; height:100%; background:#f9fafb; color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; font-size:13px; }
  #app { display:flex; flex-direction:column; height:100%; }
  header { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; padding:8px 12px;
    background:#fff; border-bottom:1px solid #e5e7eb; }
  header h1 { font-size:15px; margin:0 12px 0 0; }
  header label { display:inline-flex; align-items:center; gap:4px; color:var(--muted); cursor:pointer; white-space:nowrap; }
  header input[type=search] { padding:4px 8px; border:1px solid #d1d5db; border-radius:6px; font-size:13px; min-width:160px; }
  .pill { display:inline-block; border-radius:999px; padding:1px 8px; font-size:11px; font-weight:600; }
  .pill.new { background:#fee2e2; color:#991b1b; }
  .pill.quiet { background:#f3f4f6; color:var(--muted); font-weight:400; }
  #stage { flex:1; position:relative; overflow:hidden; }
  svg { width:100%; height:100%; display:block; cursor:grab; }
  svg.panning { cursor:grabbing; }
  .edge { stroke:#9ca3af; stroke-width:1.2; fill:none; }
  .edge.psc { stroke:var(--psc); stroke-width:2; }
  .edge.charge { stroke:var(--charge); stroke-dasharray:2 3; }
  .edge.inactive { stroke-dasharray:5 4; stroke-opacity:.5; }
  .edge.new { stroke-width:2.4; }
  .edge:hover { stroke-width:3; cursor:pointer; }
  .node { cursor:pointer; }
  .node .shape { stroke:#fff; stroke-width:1.5; }
  .node.risk .shape { stroke:var(--risk); stroke-width:3; }
  .node.dissolved { opacity:.45; }
  .node text { font-size:11px; fill:var(--ink); pointer-events:none; paint-order:stroke; stroke:#f9fafb; stroke-width:3px; }
  .node .badge { font-size:9px; font-weight:700; fill:#fff; stroke:none; }
  .dim { opacity:.12; }
  .hit .shape { stroke:#f59e0b; stroke-width:3; }
  #tip { position:absolute; pointer-events:none; background:#111827; color:#fff; padding:8px 10px; border-radius:8px;
    font-size:12px; max-width:280px; line-height:1.4; display:none; z-index:2; }
  #tip b { display:block; font-size:13px; margin-bottom:2px; }
  #tip .m { color:#d1d5db; }
  #legend { position:absolute; left:10px; bottom:10px; background:rgba(255,255,255,.92); border:1px solid #e5e7eb;
    border-radius:8px; padding:6px 10px; font-size:11px; color:var(--muted); line-height:1.6; }
  #legend i { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; vertical-align:-1px; }
  #legend i.sq { border-radius:2px; }
  #legend i.ring { background:#fff; border:2px solid var(--risk); }
  #interesting { max-height:34%; overflow:auto; background:#fff; border-top:1px solid #e5e7eb; padding:6px 12px; }
  #interesting summary { cursor:pointer; color:var(--muted); font-weight:600; }
  #interesting ul { margin:6px 0 0; padding-left:18px; }
  #interesting li { margin:3px 0; }
  #interesting li::marker { color:var(--muted); }
  .sev-high { color:var(--risk); font-weight:600; }
  .sev-medium { color:#b45309; }
  .sev-low { color:var(--muted); }
  #interesting a { color:#1d4ed8; text-decoration:none; }
  #status { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:var(--muted); }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>Connections</h1>
    <input id="search" type="search" placeholder="Find a company or person">
    <label><input id="showResigned" type="checkbox"> Resigned and ceased</label>
    <label><input id="showExternal" type="checkbox" checked> Unwatched companies</label>
    <label><input id="showOthers" type="checkbox" checked> Other officers</label>
    <span id="newBadge" class="pill new" hidden></span>
    <span id="updated" class="pill quiet"></span>
  </header>
  <div id="stage">
    <svg id="svg"><defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="#7c3aed"></path></marker>
      <marker id="arrowCharge" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="#b45309"></path></marker>
    </defs><g id="view"><g id="edges"></g><g id="nodes"></g></g></svg>
    <div id="tip"></div>
    <div id="legend">
      <div><i class="sq" style="background:var(--own)"></i>Your companies (close watch)
           <i class="sq" style="background:var(--watched);margin-left:8px"></i>Watched
           <i class="sq" style="background:var(--external);margin-left:8px"></i>Not watched</div>
      <div><i style="background:var(--followed)"></i>People you follow
           <i style="background:var(--external);margin-left:8px"></i>Other officers
           <i class="ring" style="margin-left:8px"></i>Insolvent or strike-off</div>
      <div><span style="color:var(--psc)">&#9472;&#9654;</span> owns or controls &nbsp;
           <span style="color:var(--charge)">&middot;&middot;&#9654;</span> lends (charge) &nbsp;
           <span>&#9472;</span> officer &nbsp; <span>&#9476;</span> resigned</div>
    </div>
    <div id="status">Loading the map&hellip;</div>
  </div>
  <details id="interesting" open><summary>Worth knowing</summary><ul id="lines"></ul></details>
</div>
<script>
(function () {
  'use strict';
  var svg = document.getElementById('svg'), view = document.getElementById('view');
  var gEdges = document.getElementById('edges'), gNodes = document.getElementById('nodes');
  var tip = document.getElementById('tip'), status = document.getElementById('status');
  var NS = 'http://www.w3.org/2000/svg';
  var data = null, nodes = [], edges = [], byId = {}, shown = {}, alpha = 0, frame = null;
  var scale = 1, tx = 0, ty = 0, dragging = null, panning = null, fitted = false;

  function el(name, attrs, parent) {
    var e = document.createElementNS(NS, name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function prettyDate(iso) {
    if (!iso) return '';
    var d = new Date(iso + (iso.length === 10 ? 'T00:00:00' : ''));
    if (isNaN(d)) return iso;
    return d.getDate() + ' ' + ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()] + ' ' + d.getFullYear();
  }
  function colour(n) {
    if (n.type === 'company') return n.status === 'own' ? 'var(--own)' : n.status === 'watched' ? 'var(--watched)' : 'var(--external)';
    if (n.type === 'person') return n.status === 'followed' ? 'var(--followed)' : 'var(--external)';
    return 'var(--external)';
  }
  function radius(n) { return 6 + Math.min(n.degree || 0, 12) * 0.9; }
  function has(n, flag) { return (n.flags || []).indexOf(flag) >= 0; }

  function visibleNode(n) {
    if (!document.getElementById('showExternal').checked && n.status === 'external' && n.type !== 'person') return false;
    if (!document.getElementById('showOthers').checked && n.type === 'person' && n.status !== 'followed') return false;
    return true;
  }
  function visibleEdge(e) {
    if (!e.active && !document.getElementById('showResigned').checked) return false;
    return shown[e.source] && shown[e.target];
  }

  function build() {
    gEdges.innerHTML = ''; gNodes.innerHTML = '';
    shown = {};
    nodes.forEach(function (n) { shown[n.id] = visibleNode(n); });
    // Nodes only connected by hidden edges are hidden too, except watched companies.
    var touched = {};
    edges.forEach(function (e) { if (visibleEdge(e)) { touched[e.source] = true; touched[e.target] = true; } });
    nodes.forEach(function (n) {
      if (shown[n.id] && !touched[n.id] && !(n.type === 'company' && n.status !== 'external')) shown[n.id] = false;
    });
    edges.forEach(function (e) {
      if (!visibleEdge(e)) { e.el = null; return; }
      var cls = 'edge ' + e.kind + (e.active ? '' : ' inactive') + (e.new ? ' new' : '');
      var line = el('line', { 'class': cls }, gEdges);
      if (e.kind === 'psc') line.setAttribute('marker-end', 'url(#arrow)');
      if (e.kind === 'charge') line.setAttribute('marker-end', 'url(#arrowCharge)');
      line.addEventListener('click', function () { if (e.link) window.open(e.link, '_blank'); });
      line.addEventListener('mouseenter', function (ev) { showTip(ev, edgeTip(e)); });
      line.addEventListener('mouseleave', hideTip);
      e.el = line;
    });
    nodes.forEach(function (n) {
      if (!shown[n.id]) { n.el = null; return; }
      var g = el('g', { 'class': 'node' + (has(n, 'insolvent') || has(n, 'strike_off') ? ' risk' : '') + (has(n, 'dissolved') ? ' dissolved' : '') }, gNodes);
      var r = radius(n);
      if (n.type === 'company') el('rect', { 'class': 'shape', x: -r, y: -r, width: 2 * r, height: 2 * r, rx: 3, fill: colour(n) }, g);
      else if (n.type === 'entity') el('path', { 'class': 'shape', d: 'M0,' + (-r) + ' L' + r + ',0 L0,' + r + ' L' + (-r) + ',0 z', fill: colour(n) }, g);
      else el('circle', { 'class': 'shape', r: r, fill: colour(n) }, g);
      if (has(n, 'disqualified') || has(n, 'sanctioned')) {
        el('circle', { cx: r, cy: -r, r: 5, fill: 'var(--risk)' }, g);
        var mark = el('text', { 'class': 'badge', x: r, y: -r + 3, 'text-anchor': 'middle' }, g); mark.textContent = '!';
      }
      if (n.isNew) {
        el('rect', { x: -14, y: r + 2, width: 28, height: 11, rx: 5, fill: '#b91c1c' }, g);
        var t = el('text', { 'class': 'badge', x: 0, y: r + 10.5, 'text-anchor': 'middle' }, g); t.textContent = 'NEW';
      }
      var label = el('text', { x: r + 4, y: 4 }, g); label.textContent = n.label;
      g.addEventListener('click', function () { if (!n.moved && n.link) window.open(n.link, '_blank'); });
      g.addEventListener('mouseenter', function (ev) { highlight(n); showTip(ev, nodeTip(n)); });
      g.addEventListener('mousemove', function (ev) { moveTip(ev); });
      g.addEventListener('mouseleave', function () { clearHighlight(); hideTip(); });
      g.addEventListener('mousedown', function (ev) { dragging = n; n.moved = false; n.fixed = true; ev.stopPropagation(); ev.preventDefault(); });
      n.el = g;
    });
    applySearch();
    reheat();
  }

  function neighbours(n) {
    var out = {}; out[n.id] = true;
    edges.forEach(function (e) { if (e.el && (e.source === n.id || e.target === n.id)) { out[e.source] = true; out[e.target] = true; } });
    return out;
  }
  function highlight(n) {
    var keep = neighbours(n);
    nodes.forEach(function (m) { if (m.el) m.el.classList.toggle('dim', !keep[m.id]); });
    edges.forEach(function (e) { if (e.el) e.el.classList.toggle('dim', !(e.source === n.id || e.target === n.id)); });
  }
  function clearHighlight() {
    nodes.forEach(function (m) { if (m.el) m.el.classList.remove('dim'); });
    edges.forEach(function (e) { if (e.el) e.el.classList.remove('dim'); });
    applySearch();
  }
  function applySearch() {
    var q = document.getElementById('search').value.trim().toLowerCase();
    nodes.forEach(function (m) {
      if (!m.el) return;
      var hit = q && m.label.toLowerCase().indexOf(q) >= 0;
      m.el.classList.toggle('hit', !!hit);
      m.el.classList.toggle('dim', !!q && !hit);
    });
    edges.forEach(function (e) { if (e.el) e.el.classList.toggle('dim', !!q); });
  }

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function nodeTip(n) {
    var lines = ['<b>' + esc(n.label) + '</b>'];
    var what = n.type === 'company' ? (n.number ? esc(n.number) + ' · ' : '') + (n.company_status ? esc(n.company_status) : '') : n.type === 'person' ? (n.status === 'followed' ? 'Followed' : 'Officer') : 'Outside entity';
    if (n.meta && n.meta.label) what += ' · ' + esc(n.meta.label);
    lines.push('<span class="m">' + what + '</span>');
    (n.flags || []).forEach(function (f) { if (['insolvent', 'strike_off', 'disqualified', 'sanctioned', 'dissolved'].indexOf(f) >= 0) lines.push('<span class="m">&#9888; ' + f.replace('_', '-') + '</span>'); });
    edges.forEach(function (e) {
      if (e.source !== n.id && e.target !== n.id) return;
      var other = byId[e.source === n.id ? e.target : e.source];
      if (!other) return;
      var when = e.since ? ' since ' + prettyDate(e.since) : '';
      if (e.until) when += ' until ' + prettyDate(e.until);
      var word = e.kind === 'psc' ? (e.control_label || 'controls') : e.kind === 'charge' ? 'charge' : (e.role_label || 'officer');
      lines.push('<span class="m">' + esc(word) + ' · ' + esc(other.label) + esc(when) + (e.new ? ' <span class="pill new">new</span>' : '') + '</span>');
    });
    return lines.slice(0, 14).join('<br>');
  }
  function edgeTip(e) {
    var a = byId[e.source], b = byId[e.target];
    var word = e.kind === 'psc' ? (e.control_label || 'controls') : e.kind === 'charge' ? 'has a charge in favour of' : (e.role_label || 'officer') + ' of';
    var line = e.kind === 'charge' ? esc(b.label) + ' ' + word + ' ' + esc(a.label) : esc(a.label) + ' · ' + word + ' · ' + esc(b.label);
    var when = e.since ? 'since ' + prettyDate(e.since) : '';
    if (e.until) when += ' until ' + prettyDate(e.until);
    return '<b>' + line + '</b><span class="m">' + esc(when) + (e.new ? ' · new this week' : '') + '</span>';
  }
  function showTip(ev, html) { tip.innerHTML = html; tip.style.display = 'block'; moveTip(ev); }
  function moveTip(ev) {
    var box = svg.getBoundingClientRect(), y = ev.clientY - box.top;
    tip.style.left = Math.min(ev.clientX - box.left + 14, box.width - 290) + 'px';
    tip.style.top = (y > box.height / 2 ? Math.max(0, y - tip.offsetHeight - 14) : y + 14) + 'px';
  }
  function hideTip() { tip.style.display = 'none'; }

  // ---- force layout
  function reheat() { alpha = 1; if (!frame) frame = requestAnimationFrame(tick); }
  function tick() {
    frame = null;
    var live = nodes.filter(function (n) { return n.el; });
    var w = svg.clientWidth || 800, h = svg.clientHeight || 500;
    var k = Math.sqrt((w * h) / Math.max(live.length, 1)) * 0.6;
    var i, j, a, b, dx, dy, d, f;
    for (i = 0; i < live.length; i++) {
      a = live[i];
      for (j = i + 1; j < live.length; j++) {
        b = live[j];
        dx = b.x - a.x; dy = b.y - a.y; d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        f = Math.min((k * k) / d, k) * alpha * 0.5;
        if (d > 2 * k) f *= 0.15;
        dx = dx / d * f; dy = dy / d * f;
        if (!a.fixed) { a.vx -= dx; a.vy -= dy; }
        if (!b.fixed) { b.vx += dx; b.vy += dy; }
      }
    }
    edges.forEach(function (e) {
      if (!e.el) return;
      a = byId[e.source]; b = byId[e.target];
      dx = b.x - a.x; dy = b.y - a.y; d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      f = (d - k * 0.8) / d * 0.08 * alpha;
      if (!a.fixed) { a.vx += dx * f; a.vy += dy * f; }
      if (!b.fixed) { b.vx -= dx * f; b.vy -= dy * f; }
    });
    live.forEach(function (n) {
      if (n.fixed) { n.vx = 0; n.vy = 0; }
      n.vx += (w / 2 - n.x) * 0.03 * alpha; n.vy += (h / 2 - n.y) * 0.03 * alpha;
      n.vx *= 0.5; n.vy *= 0.5;
      n.x += n.vx; n.y += n.vy;
      n.el.setAttribute('transform', 'translate(' + n.x.toFixed(1) + ',' + n.y.toFixed(1) + ')');
    });
    edges.forEach(function (e) {
      if (!e.el) return;
      a = byId[e.source]; b = byId[e.target];
      dx = b.x - a.x; dy = b.y - a.y; d = Math.sqrt(dx * dx + dy * dy) || 1;
      var rb = radius(b) + (e.kind === 'officer' ? 0 : 4);
      e.el.setAttribute('x1', a.x.toFixed(1)); e.el.setAttribute('y1', a.y.toFixed(1));
      e.el.setAttribute('x2', (b.x - dx / d * rb).toFixed(1)); e.el.setAttribute('y2', (b.y - dy / d * rb).toFixed(1));
    });
    alpha *= 0.97;
    if (alpha > 0.005) frame = requestAnimationFrame(tick);
    else if (!fitted) { fitted = true; fit(); }
  }
  function fit() {
    var live = nodes.filter(function (n) { return n.el; });
    if (!live.length) return;
    var w = svg.clientWidth || 800, h = svg.clientHeight || 500, pad = 60;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    live.forEach(function (n) { minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x); minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y); });
    scale = Math.min(2, Math.max(0.25, Math.min((w - 2 * pad) / Math.max(maxX - minX, 1), (h - 2 * pad) / Math.max(maxY - minY, 1))));
    tx = (w - (minX + maxX) * scale) / 2; ty = (h - (minY + maxY) * scale) / 2;
    applyView();
  }

  // ---- pan, zoom, drag
  function applyView() { view.setAttribute('transform', 'translate(' + tx + ',' + ty + ') scale(' + scale + ')'); }
  svg.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    var box = svg.getBoundingClientRect(), px = ev.clientX - box.left, py = ev.clientY - box.top;
    var next = Math.min(4, Math.max(0.25, scale * (ev.deltaY < 0 ? 1.15 : 0.87)));
    tx = px - (px - tx) * next / scale; ty = py - (py - ty) * next / scale; scale = next; applyView();
  }, { passive: false });
  svg.addEventListener('mousedown', function (ev) { panning = { x: ev.clientX - tx, y: ev.clientY - ty }; svg.classList.add('panning'); });
  window.addEventListener('mousemove', function (ev) {
    if (dragging) {
      var box = svg.getBoundingClientRect();
      dragging.x = (ev.clientX - box.left - tx) / scale; dragging.y = (ev.clientY - box.top - ty) / scale; dragging.moved = true;
      if (alpha < 0.3) alpha = 0.3; if (!frame) frame = requestAnimationFrame(tick);
    } else if (panning) { tx = ev.clientX - panning.x; ty = ev.clientY - panning.y; applyView(); }
  });
  window.addEventListener('mouseup', function () {
    if (dragging) { dragging.fixed = false; setTimeout(function (n) { n.moved = false; }, 0, dragging); dragging = null; }
    panning = null; svg.classList.remove('panning');
  });
  svg.addEventListener('dblclick', function () { fitted = false; reheat(); });

  ['showResigned', 'showExternal', 'showOthers'].forEach(function (id) { document.getElementById(id).addEventListener('change', build); });
  document.getElementById('search').addEventListener('input', applySearch);

  function render(d) {
    data = d;
    var w = svg.clientWidth || 800, h = svg.clientHeight || 500;
    byId = {};
    nodes = (d.nodes || []).map(function (n, i) {
      var angle = i * 2.4, rad = 8 * Math.sqrt(i);
      var m = Object.assign({}, n, { x: w / 2 + Math.cos(angle) * rad, y: h / 2 + Math.sin(angle) * rad, vx: 0, vy: 0, el: null, isNew: false });
      byId[m.id] = m; return m;
    });
    edges = (d.edges || []).map(function (e) { return Object.assign({}, e, { el: null }); });
    var fresh = 0;
    edges.forEach(function (e) { if (e.new && e.active) { fresh++; if (byId[e.source]) byId[e.source].isNew = true; if (byId[e.target]) byId[e.target].isNew = true; } });
    var badge = document.getElementById('newBadge');
    badge.hidden = !fresh; badge.textContent = fresh + ' new this week';
    document.getElementById('updated').textContent = d.generated_at ? 'Updated ' + prettyDate(d.generated_at.slice(0, 10)) : '';
    var ul = document.getElementById('lines'); ul.innerHTML = '';
    (d.interesting || []).forEach(function (line) {
      var li = document.createElement('li');
      li.innerHTML = '<span class="sev-' + esc(line.severity) + '">' + esc(line.text) + '</span>' +
        (line.link ? ' <a href="' + esc(line.link) + '" target="_blank" rel="noopener">open</a>' : '');
      ul.appendChild(li);
    });
    document.getElementById('interesting').hidden = !(d.interesting || []).length;
    status.style.display = nodes.length ? 'none' : 'flex';
    status.textContent = nodes.length ? '' : 'Nothing to draw yet: watch a company or follow a person first.';
    build();
  }

  fetch('connections.json', { cache: 'no-store' })
    .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(render)
    .catch(function (err) { status.textContent = 'Could not load connections.json (' + err.message + '). Run the companies_house.connections action with save on.'; });
})();
</script>
</body>
</html>
"""
