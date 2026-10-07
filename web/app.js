/* So nah, so fern: static viewer (MapLibre GL + D3). All numbers come from data/*.json. */
(function () {
  'use strict';

  /* ------------------------------------------------------------------ helpers */
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const finite = (v) => typeof v === 'number' && isFinite(v);

  const BG = '#e4eaef';
  const NA_COLOR = '#f1f3f5';
  const UNREACH_COLOR = '#d5dae0';
  const MODE_COLORS = { WALK: '#111827', BUS: '#7e22ce', RAIL: '#047857', TRAM: '#be185d', SUBWAY: '#1d4ed8', FERRY: '#0f766e', CABLE_CAR: '#92400e' };
  const modeColor = (m) => MODE_COLORS[m] || '#0f766e';
  const TAG_COLORS = { nf: '#c2410c', fn: '#1d4ed8' };
  const REF_SPEEDS = [10, 20, 30]; // km/h reference lines of the scatter plot
  const ANIM_MS = 1500;
  const EARTH_R_KM = 6371.0088;
  const MPP0 = 78271.517; // metres per pixel at zoom 0, equator (512 px tiles)

  /* ------------------------------------------------------------------ state */
  const state = {
    lang: 'de',
    mode: 'single',
    slug: 'kronberg',
    point: 'gare',
    ind: 't_tc',
    view: 'geo',
    overlays: { iso: false, rings: true, origin: true },
    sel: null,
    cmp: { fr: 'garches', de: 'kronberg' }
  };
  let progress = 0; // 0 = geographic map, 1 = time cartogram
  let INDEX = null;
  const cache = new Map();
  let token = 0;

  /* ------------------------------------------------------------------ i18n + formatting */
  const T = (key, params) => {
    const dict = window.I18N[state.lang] || window.I18N.de;
    let s = dict[key];
    if (s == null) s = window.I18N.de[key];
    if (s == null) return key;
    if (params) s = s.replace(/\{(\w+)\}/g, (m, k) => (params[k] != null ? params[k] : m));
    return s;
  };
  const nfCache = {};
  function num(v, maxFD, minFD) {
    if (!finite(v)) return '–';
    maxFD = maxFD == null ? 1 : maxFD;
    minFD = minFD == null ? 0 : minFD;
    const key = state.lang + maxFD + '_' + minFD;
    if (!nfCache[key]) nfCache[key] = new Intl.NumberFormat(T('locale'), { maximumFractionDigits: maxFD, minimumFractionDigits: minFD });
    return nfCache[key].format(v);
  }
  const pct = (v, d) => (finite(v) ? num(v * 100, d == null ? 0 : d) + ' %' : '–');
  const signed = (v, d) => (finite(v) ? (v > 0 ? '+' : v < 0 ? '−' : '') + num(Math.abs(v), d) : '–');
  function fmtDate(iso) {
    const d = new Date(iso + 'T12:00:00Z');
    if (isNaN(d)) return iso;
    return new Intl.DateTimeFormat(T('locale'), { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' }).format(d);
  }

  /* ------------------------------------------------------------------ data */
  function flatten(features) {
    let n = 0;
    for (const f of features) for (const poly of f.geometry.coordinates) for (const ring of poly) n += ring.length;
    const a = new Float64Array(n * 2);
    let k = 0;
    for (const f of features) for (const poly of f.geometry.coordinates) for (const ring of poly) for (const c of ring) { a[k++] = c[0]; a[k++] = c[1]; }
    return a;
  }

  function prepare(raw) {
    const meta = raw.meta;
    const ds = { slug: meta.slug, meta, raw, units: raw.units.features, res: {}, resArr: {}, timeFlat: {}, origin: {} };
    ds.geoFlat = flatten(ds.units);
    ds.unitIdx = new Map(ds.units.map((f, j) => [f.properties.unit_id, j]));
    for (const o of meta.origins) {
      ds.origin[o.id] = o;
      const arr = raw.results[o.id];
      ds.res[o.id] = new Map(arr.map((r) => [r.unit_id, r]));
      ds.resArr[o.id] = ds.units.map((f) => ds.res[o.id].get(f.properties.unit_id));
      ds.timeFlat[o.id] = flatten(raw.units_time[o.id].features);
    }
    // example itineraries, indexed by origin and destination
    ds.itin = new Map();
    ds.segs = new Map();
    for (const s of raw.itineraries.summary) ds.itin.set(s.from_id + '|' + s.to_id, s);
    for (const f of raw.itineraries.segments.features) {
      const k = f.properties.from_id + '|' + f.properties.to_id;
      if (!ds.segs.has(k)) ds.segs.set(k, []);
      ds.segs.get(k).push(f);
    }
    for (const arr of ds.segs.values()) arr.sort((a, b) => a.properties.segment - b.properties.segment);
    // radius (km) that contains the geographic polygons and the cartogram, per origin
    ds.radius = 0;
    for (const o of meta.origins) {
      const k = Math.cos((o.lat * Math.PI) / 180);
      for (const flat of [ds.geoFlat, ds.timeFlat[o.id]]) {
        for (let i = 0; i < flat.length; i += 2) {
          const r = Math.hypot((flat[i] - o.lon) * 111.32 * k, (flat[i + 1] - o.lat) * 110.574);
          if (r > ds.radius) ds.radius = r;
        }
      }
    }
    ds.maxPop = d3.max(raw.results[meta.origins[0].id], (r) => r.population) || 1;
    return ds;
  }

  function loadDataset(slug) {
    if (!cache.has(slug)) {
      const o = INDEX.origins.find((x) => x.slug === slug);
      cache.set(slug, fetch('data/' + o.file).then((r) => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(prepare));
    }
    return cache.get(slug);
  }

  /* ------------------------------------------------------------------ colour scales */
  const IND_KEYS = ['t_tc', 'paradox_index', 'time_excess_pct', 'ratio_tc_car', 'transfers'];

  function valueOf(ind, r, compare) {
    if (!r) return null;
    switch (ind) {
      case 't_tc': return r.t_tc;
      case 'paradox_index': return compare ? r.paradox_norm : r.paradox_index;
      case 'time_excess_pct': return r.time_excess_pct;
      case 'ratio_tc_car': return r.ratio_tc_car;
      case 'transfers': return r.t_tc == null ? null : r.transfers;
      default: return null;
    }
  }

  function quantileAbs(vals, q) {
    const a = vals.map(Math.abs).sort((x, y) => x - y);
    if (!a.length) return 1;
    return a[Math.min(a.length - 1, Math.floor(q * a.length))];
  }

  /** results: array of result objects used to fix the domain (all displayed datasets). */
  function buildScale(ind, results, compare) {
    const vals = results.map((r) => valueOf(ind, r, compare)).filter(finite);
    const sc = { ind, compare, type: 'seq', domain: [0, 1], clipHi: false, clipLo: false, ticks: [], fn: null };
    const maxV = vals.length ? Math.max.apply(null, vals) : 1;
    const minV = vals.length ? Math.min.apply(null, vals) : 0;
    if (ind === 't_tc') {
      const hi = Math.max(30, Math.ceil(maxV / 30) * 30);
      sc.domain = [0, hi];
      sc.ticks = d3.range(0, hi + 1, 30);
      sc.fn = (v) => d3.interpolateYlOrRd(0.07 + 0.93 * clamp(v / hi, 0, 1));
      sc.fmt = (v) => num(v, 0);
    } else if (ind === 'paradox_index' || ind === 'time_excess_pct') {
      const step = ind === 'paradox_index' ? (compare ? 0.1 : 10) : 10;
      const m = Math.max(step, Math.ceil(quantileAbs(vals, 0.98) / step) * step);
      sc.type = 'div';
      sc.domain = [-m, m];
      sc.ticks = [-m, -m / 2, 0, m / 2, m];
      sc.clipHi = maxV > m;
      sc.clipLo = minV < -m;
      sc.fn = (v) => d3.interpolateRdBu(1 - (clamp(v, -m, m) + m) / (2 * m));
      sc.fmt = (v) => (ind === 'paradox_index' && compare ? signed(v, 2) : signed(v, 0)).replace('+', '+');
    } else if (ind === 'ratio_tc_car') {
      const lo = Math.max(0, Math.floor(minV));
      let hi = Math.ceil(quantileAbs(vals, 0.98));
      if (hi <= lo) hi = lo + 1;
      sc.domain = [lo, hi];
      sc.ticks = d3.range(lo, hi + 1, Math.max(1, Math.ceil((hi - lo) / 6)));
      sc.clipHi = maxV > hi;
      sc.fn = (v) => d3.interpolateBuPu(0.08 + 0.92 * clamp((v - lo) / (hi - lo), 0, 1));
      sc.fmt = (v) => num(v, 0);
    } else {
      sc.type = 'ord';
      const cols = d3.quantize(d3.interpolateYlGnBu, 6).slice(1);
      sc.classes = [0, 1, 2, 3, 4];
      sc.fn = (v) => cols[clamp(Math.round(v), 0, 4)];
      sc.fmt = (v) => (v >= 4 ? T('legend_trans_4') : String(v));
    }
    sc.legendKey = ind === 'paradox_index' && compare ? 'legend_paradox_norm' : 'legend_' + ind;
    return sc;
  }

  function classify(sc, r) {
    if (!r) return { kind: 'na', c: NA_COLOR };
    if (r.t_tc == null) return { kind: 'unreach', c: UNREACH_COLOR };
    const v = valueOf(sc.ind, r, sc.compare);
    if (!finite(v)) return { kind: 'na', c: NA_COLOR };
    return { kind: 'val', c: sc.fn(v), v };
  }

  function valueText(sc, r) {
    if (!r) return '–';
    if (r.t_tc == null) return T('legend_unreach');
    const v = valueOf(sc.ind, r, sc.compare);
    if (!finite(v)) return T('legend_na');
    switch (sc.ind) {
      case 't_tc': return num(v, 1) + ' ' + T('unit_min');
      case 'paradox_index': return sc.compare ? signed(v, 2) : signed(v, 1);
      case 'time_excess_pct': return signed(v, 1) + ' ' + T('unit_pct');
      case 'ratio_tc_car': return num(v, 1) + '×';
      case 'transfers': return r.transfers >= 4 && r.transfers_censored ? T('legend_trans_4') : String(r.transfers);
      default: return String(v);
    }
  }

  function legendHTML(sc, results) {
    const anyUn = results.some((r) => r && r.t_tc == null);
    const anyNa = results.some((r) => r && r.t_tc != null && !finite(valueOf(sc.ind, r, sc.compare)));
    let body = '';
    if (sc.type === 'ord') {
      body = '<div class="lg-ord">' + sc.classes.map((c) => '<span class="lg-chip"><i style="background:' + sc.fn(c) + '"></i>' + esc(c >= 4 ? T('legend_trans_4') : c) + '</span>').join('') + '</div>';
    } else {
      const n = 14;
      const stops = d3.range(n).map((i) => {
        const v = sc.domain[0] + ((sc.domain[1] - sc.domain[0]) * i) / (n - 1);
        return sc.fn(v) + ' ' + ((i / (n - 1)) * 100).toFixed(1) + '%';
      });
      const span = sc.domain[1] - sc.domain[0];
      const ticks = sc.ticks.map((t) => {
        let label = sc.fmt(t);
        if (t === sc.domain[1] && sc.clipHi) label = '≥ ' + label;
        if (t === sc.domain[0] && sc.clipLo) label = '≤ ' + label;
        return '<span style="left:' + (((t - sc.domain[0]) / span) * 100).toFixed(1) + '%">' + esc(label) + '</span>';
      }).join('');
      body = '<div class="lg-bar" style="background:linear-gradient(to right,' + stops.join(',') + ')"></div><div class="lg-ticks">' + ticks + '</div>';
    }
    let extra = '';
    if (anyUn) extra += '<span class="lg-item"><i class="sw hatch"></i>' + esc(T('legend_unreach')) + '</span>';
    if (anyNa) extra += '<span class="lg-item"><i class="sw na"></i>' + esc(T('legend_na')) + '</span>';
    extra += '<span class="lg-item"><i class="dot" style="background:' + TAG_COLORS.nf + '"></i>' + esc(T('list_nf_title')) + '</span>';
    extra += '<span class="lg-item"><i class="dot" style="background:' + TAG_COLORS.fn + '"></i>' + esc(T('list_fn_title')) + '</span>';
    const clip = sc.clipHi || sc.clipLo ? ' ' + T('clamp_note') : '';
    const norm = sc.ind === 'paradox_index' && sc.compare ? ' ' + T('cmp_norm_note') : '';
    return '<div class="lg-title">' + esc(T(sc.legendKey)) + '</div><div class="lg-main">' + body + '</div><div class="lg-extra">' + extra + '</div>' +
      (clip || norm ? '<div class="lg-note">' + esc((clip + norm).trim()) + '</div>' : '');
  }

  /* ------------------------------------------------------------------ geometry helpers */
  function geodesicCircle(lon, lat, km, n) {
    n = n || 180;
    const d = km / EARTH_R_KM;
    const p1 = (lat * Math.PI) / 180;
    const l1 = (lon * Math.PI) / 180;
    const out = [];
    for (let i = 0; i <= n; i++) {
      const th = (i / n) * 2 * Math.PI;
      const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(th));
      const l2 = l1 + Math.atan2(Math.sin(th) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
      out.push([(((l2 * 180) / Math.PI + 540) % 360) - 180, (p2 * 180) / Math.PI]);
    }
    return out;
  }
  function extremeVertex(geom, pick) {
    let best = null;
    const polys = geom.type === 'Polygon' ? [geom.coordinates] : geom.coordinates;
    for (const poly of polys) for (const c of poly[0]) if (!best || pick(c, best)) best = c;
    return best;
  }

  function makeHatch() {
    const c = document.createElement('canvas');
    c.width = c.height = 16;
    const x = c.getContext('2d');
    x.strokeStyle = '#8b949e';
    x.lineWidth = 1.5;
    x.beginPath();
    for (const k of [-16, 0, 16]) { x.moveTo(k, 16); x.lineTo(k + 16, 0); }
    x.stroke();
    return x.getImageData(0, 0, 16, 16);
  }

  /* ------------------------------------------------------------------ MapView */
  class MapView {
    constructor(el) {
      this.el = el;
      this.ds = null;
      this.originId = null;
      this.p = 0;
      this.fc = null;
      this.pairs = [];
      this.ptsFC = null;
      this.ptsData = [];
      this.sel = null;
      this.flags = { iso: true, rings: true, origin: true };
      this.markers = { iso: [], ring: [], time: [], origin: null, sel: null };
      this.itinOn = false;
      this.onSelect = null;
      this.onHover = null;
      this.ready = new Promise((resolve) => {
        this.map = new maplibregl.Map({
          container: el,
          style: { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': BG } }] },
          center: [8.5, 50.1],
          zoom: 8,
          minZoom: 5,
          maxZoom: 13,
          attributionControl: false,
          dragRotate: false,
          pitchWithRotate: false,
          renderWorldCopies: false,
          fadeDuration: 0
        });
        this.map.touchZoomRotate.disableRotation();
        this.map.keyboard.disableRotation();
        this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
        this.map.on('load', () => { this._init(); resolve(); });
      });
    }

    _init() {
      const map = this.map;
      map.addImage('hatch', makeHatch(), { pixelRatio: 2 });
      const empty = { type: 'FeatureCollection', features: [] };
      for (const id of ['units', 'points', 'iso', 'rings', 'timerings', 'itin']) map.addSource(id, { type: 'geojson', data: empty, tolerance: 0.2 });
      map.addLayer({ id: 'units-fill', type: 'fill', source: 'units', paint: { 'fill-color': ['get', 'c'], 'fill-opacity': 0.85 } });
      map.addLayer({ id: 'units-hatch', type: 'fill', source: 'units', filter: ['==', ['get', 'kind'], 'unreach'], paint: { 'fill-pattern': 'hatch', 'fill-opacity': 0.9 } });
      map.addLayer({ id: 'units-line', type: 'line', source: 'units', paint: { 'line-color': '#ffffff', 'line-width': 0.7, 'line-opacity': 0.85 } });
      map.addLayer({ id: 'sel-halo', type: 'line', source: 'units', filter: ['==', ['get', 'unit_id'], ''], paint: { 'line-color': '#ffffff', 'line-width': 6 } });
      map.addLayer({ id: 'sel-line', type: 'line', source: 'units', filter: ['==', ['get', 'unit_id'], ''], paint: { 'line-color': '#111827', 'line-width': 3 } });
      map.addLayer({ id: 'iso-line', type: 'line', source: 'iso', layout: { 'line-join': 'round' }, paint: { 'line-color': '#1f2937', 'line-width': 1.1, 'line-opacity': 0.75 } });
      map.addLayer({ id: 'ring-line', type: 'line', source: 'rings', paint: { 'line-color': '#4b5563', 'line-width': 1.3, 'line-dasharray': [3, 3], 'line-opacity': 0.9 } });
      map.addLayer({ id: 'time-line', type: 'line', source: 'timerings', paint: { 'line-color': '#4b5563', 'line-width': 1.3, 'line-dasharray': [3, 3], 'line-opacity': 0 } });
      map.addLayer({ id: 'itin-case', type: 'line', source: 'itin', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#ffffff', 'line-width': 7, 'line-opacity': 0.9 } });
      const modeMatch = ['match', ['get', 'mode']];
      for (const m of Object.keys(MODE_COLORS)) modeMatch.push(m, MODE_COLORS[m]);
      modeMatch.push('#0f766e');
      map.addLayer({ id: 'itin-walk', type: 'line', source: 'itin', filter: ['==', ['get', 'mode'], 'WALK'], layout: { 'line-cap': 'butt', 'line-join': 'round' }, paint: { 'line-color': '#111827', 'line-width': 3, 'line-dasharray': [1.2, 1.4] } });
      map.addLayer({ id: 'itin-transit', type: 'line', source: 'itin', filter: ['!=', ['get', 'mode'], 'WALK'], layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': modeMatch, 'line-width': 4.5 } });
      map.addLayer({
        id: 'points', type: 'circle', source: 'points',
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, ['case', ['==', ['get', 'tag'], ''], 1.8, 4], 11, ['case', ['==', ['get', 'tag'], ''], 3.4, 6]],
          'circle-color': ['match', ['get', 'tag'], 'nf', TAG_COLORS.nf, 'fn', TAG_COLORS.fn, '#1f2937'],
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': ['case', ['==', ['get', 'tag'], ''], 0.7, 1.4],
          'circle-opacity': 0.9
        }
      });
      map.addLayer({ id: 'sel-point', type: 'circle', source: 'points', filter: ['==', ['get', 'unit_id'], ''], paint: { 'circle-radius': 10, 'circle-color': 'rgba(255,255,255,0)', 'circle-stroke-color': '#111827', 'circle-stroke-width': 2.5 } });

      map.on('click', (e) => {
        const f = map.queryRenderedFeatures(e.point, { layers: ['points', 'units-fill'] })[0];
        if (this.onSelect) this.onSelect(f ? f.properties.unit_id : null, this);
      });
      map.on('mousemove', (e) => {
        const f = map.queryRenderedFeatures(e.point, { layers: ['points', 'units-fill'] })[0];
        map.getCanvas().style.cursor = f ? 'pointer' : '';
        if (this.onHover) this.onHover(f ? f.properties.unit_id : null, e.originalEvent, this);
      });
      map.on('mouseout', () => { if (this.onHover) this.onHover(null, null, this); });
      map.on('zoom', () => this._timeLabelDensity());
    }

    get originLL() {
      const o = this.ds.origin[this.originId];
      return [o.lon, o.lat];
    }

    /** zoom level at which the circle of radius R km fits into the shorter side of the map */
    zoomFor(R) {
      const half = Math.max(100, Math.min(this.el.clientWidth, this.el.clientHeight)) / 2;
      const lat = this.originLL[1];
      return Math.log2((MPP0 * Math.cos((lat * Math.PI) / 180) * half) / (R * 1000));
    }

    async load(ds, originId, p) {
      await this.ready;
      this.ds = ds;
      this.originId = originId;
      this.p = p;
      // polygons: own copy of the vertices, mutated in place for the transition
      this.pairs = [];
      const pairs = this.pairs;
      const feats = ds.units.map((f) => ({
        type: 'Feature',
        properties: { unit_id: f.properties.unit_id, name: f.properties.name, c: NA_COLOR, kind: 'na' },
        geometry: {
          type: 'MultiPolygon',
          coordinates: f.geometry.coordinates.map((poly) => poly.map((ring) => ring.map((c) => { const q = [c[0], c[1]]; pairs.push(q); return q; })))
        }
      }));
      this.fc = { type: 'FeatureCollection', features: feats };
      this.geo = ds.geoFlat;
      this.tim = ds.timeFlat[originId];
      // points
      this.ptsData = ds.raw.results[originId].filter((r) => r.t_tc != null).map((r) => ({ r, f: { type: 'Feature', properties: { unit_id: r.unit_id, name: r.name, tag: r.top_nah_fern ? 'nf' : r.top_fern_nah ? 'fn' : '' }, geometry: { type: 'Point', coordinates: [r.lon, r.lat] } } }));
      this.ptsFC = { type: 'FeatureCollection', features: this.ptsData.map((x) => x.f) };
      // overlays
      const isoF = ds.raw.isochrones.features.filter((f) => f.properties.origin_id === originId);
      const ringF = ds.raw.rings.features.filter((f) => f.properties.origin_id === originId);
      this.map.getSource('iso').setData({ type: 'FeatureCollection', features: isoF });
      this.map.getSource('rings').setData({ type: 'FeatureCollection', features: ringF });
      this._buildTimeRings();
      this._buildLabels(isoF, ringF);
      this.refreshOrigin();
      this._applyProgress(p);
      this.select(this.sel && ds.unitIdx.has(this.sel) ? this.sel : null);
    }

    _clearMarkers(list) { list.forEach((m) => m.marker.remove()); list.length = 0; }

    _label(cls, text, lngLat, anchor) {
      const el = document.createElement('div');
      el.className = 'maplbl ' + cls;
      el.textContent = text;
      const marker = new maplibregl.Marker({ element: el, anchor: anchor || 'bottom' }).setLngLat(lngLat).addTo(this.map);
      return { marker, el };
    }

    _buildLabels(isoF, ringF) {
      this._clearMarkers(this.markers.iso);
      this._clearMarkers(this.markers.ring);
      for (const f of isoF) {
        const v = extremeVertex(f.geometry, (c, b) => c[0] > b[0]);
        if (!v) continue;
        const m = this._label('iso', f.properties.minutes + ' ' + T('unit_min'), v, 'left');
        m.minutes = f.properties.minutes;
        this.markers.iso.push(m);
      }
      for (const f of ringF) {
        const v = extremeVertex(f.geometry, (c, b) => c[1] > b[1]);
        if (!v) continue;
        const m = this._label('ring', f.properties.km + ' ' + T('ring_label_km'), v, 'bottom');
        this.markers.ring.push(m);
      }
    }

    _buildTimeRings() {
      const ds = this.ds;
      const [lon, lat] = this.originLL;
      const speed = ds.meta.cartogram_speed_kmh;
      const step = 15;
      const maxMin = Math.floor(((ds.radius * 1.05) / speed) * 60 / step) * step;
      const feats = [];
      this._clearMarkers(this.markers.time);
      for (let m = step; m <= maxMin; m += step) {
        const ring = geodesicCircle(lon, lat, (m / 60) * speed);
        feats.push({ type: 'Feature', properties: { minutes: m }, geometry: { type: 'LineString', coordinates: ring } });
        const lab = this._label('time', T('time_ring', { m }), ring[0], 'bottom');
        lab.minutes = m;
        this.markers.time.push(lab);
      }
      this.map.getSource('timerings').setData({ type: 'FeatureCollection', features: feats });
      this._timeLabelDensity();
    }

    _timeLabelDensity() {
      if (!this.ds || !this.markers.time.length) return;
      const mpp = (MPP0 * Math.cos((this.originLL[1] * Math.PI) / 180)) / Math.pow(2, this.map.getZoom());
      const px = ((15 * this.ds.meta.cartogram_speed_kmh) / 60 * 1000) / mpp;
      const dense = px < 16;
      for (const m of this.markers.time) m.el.classList.toggle('thin', dense && m.minutes % 30 !== 0);
    }

    refreshOrigin() {
      if (!this.ds) return;
      if (this.markers.origin) this.markers.origin.marker.remove();
      const o = this.ds.origin[this.originId];
      const el = document.createElement('div');
      el.className = 'origin-marker';
      el.innerHTML = '<span class="om-dot"></span>';
      el.title = o[state.lang] || o.de;
      el.setAttribute('role', 'img');
      el.setAttribute('aria-label', o[state.lang] || o.de);
      const marker = new maplibregl.Marker({ element: el, anchor: 'center' }).setLngLat([o.lon, o.lat]).addTo(this.map);
      this.markers.origin = { marker, el };
      this._overlayVisibility();
    }

    recolor(sc) {
      const arr = this.ds.resArr[this.originId];
      const feats = this.fc.features;
      for (let j = 0; j < feats.length; j++) {
        const c = classify(sc, arr[j]);
        feats[j].properties.c = c.c;
        feats[j].properties.kind = c.kind;
      }
      this.map.getSource('units').setData(this.fc);
    }

    /** t: 0 = geographic, 1 = time cartogram */
    setProgress(p) {
      this.p = p;
      this._applyProgress(p);
    }

    _applyProgress(p) {
      if (!this.fc) return;
      const g = this.geo, t = this.tim, pairs = this.pairs;
      for (let i = 0, n = pairs.length; i < n; i++) {
        const a = g[2 * i], b = g[2 * i + 1];
        pairs[i][0] = a + (t[2 * i] - a) * p;
        pairs[i][1] = b + (t[2 * i + 1] - b) * p;
      }
      this.map.getSource('units').setData(this.fc);
      for (const x of this.ptsData) {
        const r = x.r;
        const c = x.f.geometry.coordinates;
        c[0] = r.lon + (r.tlon - r.lon) * p;
        c[1] = r.lat + (r.tlat - r.lat) * p;
      }
      this.map.getSource('points').setData(this.ptsFC);
      this.map.setPaintProperty('units-fill', 'fill-opacity', 0.86 - 0.3 * p);
      this._overlayVisibility();
      this._moveSel();
    }

    setFlags(flags) {
      this.flags = Object.assign({}, flags);
      this._overlayVisibility();
    }

    _overlayVisibility() {
      if (!this.map.getLayer || !this.map.getLayer('iso-line')) return;
      const p = this.p, geoOp = 1 - p, timeOp = p;
      const vis = (on) => (on ? 'visible' : 'none');
      const map = this.map;
      map.setLayoutProperty('iso-line', 'visibility', vis(this.flags.iso && geoOp > 0));
      map.setLayoutProperty('ring-line', 'visibility', vis(this.flags.rings && geoOp > 0));
      map.setLayoutProperty('time-line', 'visibility', vis(this.flags.rings && timeOp > 0));
      map.setLayoutProperty('itin-case', 'visibility', vis(this.itinOn && geoOp > 0));
      map.setLayoutProperty('itin-walk', 'visibility', vis(this.itinOn && geoOp > 0));
      map.setLayoutProperty('itin-transit', 'visibility', vis(this.itinOn && geoOp > 0));
      map.setPaintProperty('iso-line', 'line-opacity', 0.75 * geoOp);
      map.setPaintProperty('ring-line', 'line-opacity', 0.9 * geoOp);
      map.setPaintProperty('time-line', 'line-opacity', 0.9 * timeOp);
      map.setPaintProperty('itin-case', 'line-opacity', 0.9 * geoOp);
      map.setPaintProperty('itin-walk', 'line-opacity', geoOp);
      map.setPaintProperty('itin-transit', 'line-opacity', geoOp);
      const setLbl = (list, on, op) => list.forEach((m) => { m.el.style.opacity = op; m.el.classList.toggle('off', !on || op <= 0.02); });
      setLbl(this.markers.iso, this.flags.iso, geoOp);
      setLbl(this.markers.ring, this.flags.rings, geoOp);
      setLbl(this.markers.time, this.flags.rings, timeOp);
      if (this.markers.origin) this.markers.origin.el.classList.toggle('off', !this.flags.origin);
    }

    select(id) {
      this.sel = id || null;
      const f = ['==', ['get', 'unit_id'], this.sel || ''];
      if (this.map.getLayer('sel-line')) {
        this.map.setFilter('sel-line', f);
        this.map.setFilter('sel-halo', f);
        this.map.setFilter('sel-point', f);
      }
      if (this.markers.sel) { this.markers.sel.remove(); this.markers.sel = null; }
      if (this.sel && this.ds) {
        const r = this.ds.res[this.originId].get(this.sel);
        if (r) {
          const el = document.createElement('div');
          el.className = 'maplbl sel';
          el.textContent = r.name;
          this.markers.sel = new maplibregl.Marker({ element: el, anchor: 'bottom', offset: [0, -12] }).setLngLat(this._pt(r)).addTo(this.map);
        }
      }
    }
    _pt(r) { return [r.lon + (r.tlon - r.lon) * this.p, r.lat + (r.tlat - r.lat) * this.p]; }
    _moveSel() {
      if (this.markers.sel && this.sel) {
        const r = this.ds.res[this.originId].get(this.sel);
        if (r) this.markers.sel.setLngLat(this._pt(r));
      }
    }

    setItinerary(feats) {
      this.itinOn = !!(feats && feats.length);
      this.map.getSource('itin').setData({ type: 'FeatureCollection', features: feats || [] });
      this._overlayVisibility();
    }
  }

  /* ------------------------------------------------------------------ views */
  let mainView = null;
  let cmpA = null;
  let cmpB = null;
  let syncing = false;

  function linkViews(a, b) {
    const sync = (src, dst) => () => {
      if (syncing || !src.ds || !dst.ds) return;
      syncing = true;
      try {
        const MC = maplibregl.MercatorCoordinate;
        const sl = src.originLL, dl = dst.originLL;
        const cs = Math.cos((sl[1] * Math.PI) / 180), cd = Math.cos((dl[1] * Math.PI) / 180);
        const c = MC.fromLngLat(src.map.getCenter()), o = MC.fromLngLat(sl), od = MC.fromLngLat(dl);
        const f = cs / cd;
        const nc = new MC(od.x + (c.x - o.x) * f, od.y + (c.y - o.y) * f, 0).toLngLat();
        dst.map.jumpTo({ center: nc, zoom: src.map.getZoom() + Math.log2(cd / cs) });
      } finally { syncing = false; }
    };
    a.map.on('move', sync(a, b));
    b.map.on('move', sync(b, a));
  }

  function activeViews() {
    return state.mode === 'single' ? [mainView] : [cmpA, cmpB];
  }

  /* ------------------------------------------------------------------ rendering: shared bits */
  const STATIC_TEXT = [
    ['skip', 'skip'], ['h-sub', 'subtitle'], ['l-mode', 'mode_label'], ['l-origin', 'origin_label'], ['l-cmp-fr', 'cmp_fr_label'], ['l-cmp-de', 'cmp_de_label'],
    ['l-point', 'point_label'], ['l-ind', 'indicator_label'], ['l-view', 'view_label'], ['l-ov', 'overlays_label'],
    ['t-ov-iso', 'ov_iso'], ['t-ov-rings', 'ov_rings'], ['t-ov-origin', 'ov_origin'], ['t-scatter', 'scatter_title'], ['l-search', 'search_label'],
    ['t-cmp', 'cmp_title'], ['cmp-note', 'cmp_note'], ['t-cmp-table', 'cmp_table_title'], ['reset-single', 'reset_view'], ['reset-cmp', 'reset_view']
  ];

  function applyStaticTexts() {
    document.documentElement.lang = state.lang;
    document.title = T('title');
    for (const [id, key] of STATIC_TEXT) { const el = document.getElementById(id); if (el) el.textContent = T(key); }
    $('#h-title').textContent = T('title');
    $('#search').placeholder = T('search_ph');
    $('#loading').textContent = T('loading');
    $('#lang-group').setAttribute('aria-label', T('lang_label'));
    $$('#lang-group button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.lang === state.lang)));
    const lab = { single: 'mode_single', compare: 'mode_compare' };
    $$('#mode-group button').forEach((b) => { b.textContent = T(lab[b.dataset.mode]); b.setAttribute('aria-pressed', String(b.dataset.mode === state.mode)); });
    $$('#point-group button').forEach((b) => { b.textContent = T('point_' + b.dataset.point); b.setAttribute('aria-pressed', String(b.dataset.point === state.point)); });
    $$('#view-group button').forEach((b) => { b.textContent = T('view_' + b.dataset.view); b.setAttribute('aria-pressed', String(b.dataset.view === state.view)); });
    const sel = $('#sel-ind');
    sel.innerHTML = IND_KEYS.map((k) => '<option value="' + k + '">' + esc(T('ind_' + k)) + '</option>').join('');
    sel.value = state.ind;
    $('#ov-iso').checked = state.overlays.iso;
    $('#ov-rings').checked = state.overlays.rings;
    $('#ov-origin').checked = state.overlays.origin;
    $('#map').setAttribute('aria-label', T(progress > 0.5 ? 'map_aria_time' : 'map_aria'));
    $('#map-a').setAttribute('aria-label', T('map_aria'));
    $('#map-b').setAttribute('aria-label', T('map_aria'));
    $('#scatter').setAttribute('aria-label', T('scatter_aria'));
    fillOriginSelects();
  }

  function originLabelFor(slug) {
    const o = INDEX.origins.find((x) => x.slug === slug);
    return o.name + ' (' + o.country + ')';
  }
  function fillOriginSelects() {
    const opts = (list, cur) => list.map((o) => '<option value="' + o.slug + '"' + (o.slug === cur ? ' selected' : '') + '>' + esc(originLabelFor(o.slug)) + '</option>').join('');
    $('#sel-origin').innerHTML = opts(INDEX.origins, state.slug);
    $('#sel-cmp-fr').innerHTML = opts(INDEX.origins.filter((o) => o.country === 'FR'), state.cmp.fr);
    $('#sel-cmp-de').innerHTML = opts(INDEX.origins.filter((o) => o.country === 'DE'), state.cmp.de);
  }

  function renderFooter(dsList) {
    const seen = new Set();
    const items = [];
    for (const ds of dsList) for (const s of ds.meta.sources) { const key = s.attribution + '|' + s.licence; if (!seen.has(key)) { seen.add(key); items.push(s); } }
    const m0 = dsList[0].meta;
    const dates = Array.from(new Set(dsList.map((d) => fmtDate(d.meta.date)))).join(' / ');
    const gen = Array.from(new Set(dsList.map((d) => (d.meta.generated_utc || '').slice(0, 10)).filter(Boolean).map(fmtDate))).join(' / ');
    const speed = m0.cartogram_speed_kmh;
    $('#footer').innerHTML =
      '<p class="foot-note">' + esc(T('foot_note', { w1: m0.window[0], w2: m0.window[1], date: dates })) + '</p>' +
      '<p class="foot-note">' + esc(T('foot_scale', { v: num(speed, 1) })) + '</p>' +
      '<h2>' + esc(T('foot_sources')) + '</h2><ul>' + items.map((s) => '<li>' + esc(s.attribution) + ' <span class="lic">(' + esc(s.licence) + ')</span></li>').join('') + '</ul>' +
      (gen ? '<p class="foot-gen">' + esc(T('foot_generated')) + ' ' + esc(gen) + '</p>' : '');
  }

  const tip = () => $('#tooltip');
  function showTip(html, evt) {
    const t = tip();
    if (!html) { t.hidden = true; return; }
    t.innerHTML = html;
    t.hidden = false;
    const pad = 14;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    const w = t.offsetWidth, h = t.offsetHeight;
    if (x + w > window.innerWidth - 6) x = evt.clientX - w - pad;
    if (y + h > window.innerHeight - 6) y = evt.clientY - h - pad;
    t.style.left = Math.max(4, x) + 'px';
    t.style.top = Math.max(4, y) + 'px';
  }
  function tipHTML(sc, r) {
    const lvl = r.level === 'district' ? ' <span class="lvl">' + esc(T('d_level_district')) + '</span>' : '';
    return '<strong>' + esc(r.name) + '</strong>' + lvl +
      '<div>' + esc(T('tip_dist')) + ': ' + esc(num(r.dist_km, 1)) + ' ' + T('unit_km') + '</div>' +
      '<div>' + esc(T('tip_time')) + ': ' + esc(r.t_tc == null ? T('legend_unreach') : num(r.t_tc, 1) + ' ' + T('unit_min')) + '</div>' +
      '<div>' + esc(T('ind_' + sc.ind)) + ': ' + esc(valueText(sc, r)) + '</div>';
  }

  /* ------------------------------------------------------------------ single view: summary, lists, detail, scatter */
  let cur = null; // {ds, scale, results}

  function renderSummary(ds) {
    const s = ds.meta.summary[state.point];
    const o = ds.origin[state.point];
    const stat = (k, v, sub) => '<div class="stat"><div class="k">' + esc(k) + '</div><div class="v">' + v + '</div><div class="s">' + esc(sub || '') + '</div></div>';
    const head = '<div class="sum-head"><h2>' + esc(ds.meta.name) + '</h2><p>' + esc(o[state.lang] || o.de) + ' · ' + esc(T('sum_core')) + ': ' + esc(ds.meta.core_city) + ' · ' + esc(T('sum_core_sub', { r: num(ds.meta.radius_km, 0) })) + '</p></div>';
    $('#summary').innerHTML = head + '<div class="stats">' +
      stat(T('sum_n'), esc(num(s.n_destinations, 0)), T('sum_n_sub', { n: num(s.n_unreachable, 0) })) +
      stat(T('sum_median'), esc(num(s.t_tc_median_min, 1)) + ' <small>' + T('unit_min') + '</small>', '') +
      stat(T('sum_veff'), esc(num(s.v_eff_popweighted_median_kmh, 1)) + ' <small>' + T('unit_kmh') + '</small>', T('sum_veff_sub', { v: num(s.v_eff_median_kmh, 1) })) +
      stat(T('sum_spearman'), esc(num(s.spearman_dist_time, 2, 2)), T('sum_spearman_sub')) +
      stat(T('sum_transfers'), esc(pct(s.share_2plus_transfers, 0)), T('sum_transfers_sub', { n: num(s.median_transfers, 1) })) +
      stat(T('sum_ratio'), esc(num(s.ratio_tc_car_median, 1)) + '<small>×</small>', T('sum_ratio_sub')) +
      stat(T('sum_dead'), esc(num(s.dead_zones.n, 0)), T('sum_dead_sub', { pop: num(s.dead_zones.population, 0) })) +
      '</div>';
  }

  function renderList(el, titleKey, subParams, subKey, rows) {
    const items = rows.map((r, i) => {
      const lvl = r.level === 'district' ? ' <em>' + esc(T('d_level_district')) + '</em>' : '';
      const rank = r._rank != null ? r._rank : i + 1;
      return '<li><button type="button" data-id="' + esc(r.unit_id) + '"' + (state.sel === r.unit_id ? ' aria-current="true"' : '') + '>' +
        '<span class="rk">' + esc(rank) + '</span><span class="nm">' + esc(r.name) + lvl + '</span>' +
        '<span class="mt">' + esc(num(r.dist_km, 1)) + ' ' + T('unit_km') + ' · ' + esc(r.t_tc == null ? '–' : num(r.t_tc, 0)) + ' ' + T('unit_min') + '</span></button></li>';
    }).join('');
    el.innerHTML = '<h2>' + esc(T(titleKey)) + '</h2><p class="caption">' + esc(T(subKey, subParams)) + '</p>' +
      (rows.length ? '<ol>' + items + '</ol>' : '<p class="caption">' + esc(T('list_empty')) + '</p>');
  }

  function renderLists(ds) {
    const rs = ds.raw.results[state.point];
    const th = ds.meta.thresholds;
    const nf = rs.filter((r) => r.top_nah_fern).sort((a, b) => a.top_nah_fern - b.top_nah_fern);
    const fn = rs.filter((r) => r.top_fern_nah).sort((a, b) => a.top_fern_nah - b.top_fern_nah);
    const dz = rs.filter((r) => r.dead_zone).sort((a, b) => a.dist_km - b.dist_km);
    nf.forEach((r) => { r._rank = r.top_nah_fern; });
    fn.forEach((r) => { r._rank = r.top_fern_nah; });
    dz.forEach((r, i) => { r._rank = i + 1; });
    renderList($('#list-nf'), 'list_nf_title', null, 'list_nf_sub', nf);
    renderList($('#list-fn'), 'list_fn_title', null, 'list_fn_sub', fn);
    renderList($('#list-dz'), 'list_dz_title', { d: num(th.dead_zone_max_km, 0), t: num(th.dead_zone_min_min, 0) }, 'list_dz_sub', dz);
  }

  function chips(entry) {
    if (!entry) return '';
    const names = String(entry.chain || '').split(' › ');
    const modes = String(entry.mode_chain || '').split(' › ');
    return names.map((n, i) => '<span class="chip" style="background:' + modeColor(modes[i]) + '">' + esc(n) + '</span>').join('<span class="arrow" aria-hidden="true">›</span>');
  }

  function renderDetail(ds) {
    const el = $('#detail');
    const r = state.sel ? ds.res[state.point].get(state.sel) : null;
    if (!r) {
      el.innerHTML = '<h2>' + esc(T('detail_title')) + '</h2><p class="caption">' + esc(T('detail_empty')) + '</p>';
      return;
    }
    const row = (k, v) => '<div class="row"><dt>' + esc(k) + '</dt><dd>' + v + '</dd></div>';
    const n = ds.meta.summary[state.point].n_reference;
    const trans = r.transfers == null ? '–' : r.transfers_censored ? esc(T('d_trans_plus', { n: r.transfers })) : esc(r.transfers);
    const flags = [];
    if (r.is_origin_commune) flags.push(T('d_origin_commune'));
    if (r.t_tc == null) flags.push(T('d_unreach'));
    if (r.walk_only) flags.push(T('d_walk_only'));
    if (r.dead_zone) flags.push(T('d_dead'));
    if (r.top_nah_fern) flags.push(T('d_top_nf', { r: r.top_nah_fern }));
    if (r.top_fern_nah) flags.push(T('d_top_fn', { r: r.top_fern_nah }));
    const rng = finite(r.travel_time_p25) && finite(r.travel_time_p75) ? ' <span class="muted">(' + esc(T('d_range')) + ': ' + esc(num(r.travel_time_p25, 1)) + '–' + esc(num(r.travel_time_p75, 1)) + ' ' + T('unit_min') + ')</span>' : '';
    const key = state.point + '|' + r.unit_id;
    const it = ds.itin.get(key);
    const segs = ds.segs.get(key);
    let itin = '';
    if (it) {
      const dep = segs && segs[0] ? segs[0].properties.departure_time.slice(11, 16) : null;
      const segHTML = (segs || []).map((f) => {
        const p = f.properties;
        const label = p.mode === 'WALK' ? T('d_itin_walk') : (p.route || p.mode);
        const wait = p.wait_min > 0.5 ? ' <span class="muted">· ' + esc(T('d_itin_wait', { w: num(p.wait_min, 0) })) + '</span>' : '';
        return '<li><span class="sw-mode" style="background:' + modeColor(p.mode) + '"></span>' + esc(label) + ' <span class="muted">· ' + esc(num(p.travel_min, 0)) + ' ' + T('unit_min') + '</span>' + wait + '</li>';
      }).join('');
      itin = '<h3>' + esc(T('d_itin')) + '</h3><div class="chain">' + (it.chain ? chips(it) : '<span class="chip" style="background:' + modeColor('WALK') + '">' + esc(T('d_itin_walk')) + '</span>') + '</div>' +
        '<p class="itin-total"><strong>' + esc(T('d_itin_total', { t: num(it.total_min, 0) })) + '</strong>' + (dep ? ' <span class="muted">· ' + esc(T('d_itin_dep', { t: dep })) + '</span>' : '') + '</p>' +
        (segHTML ? '<ul class="segs">' + segHTML + '</ul>' : '');
    } else {
      itin = '<h3>' + esc(T('d_itin')) + '</h3><p class="caption">' + esc(T('d_itin_none')) + '</p>';
    }
    el.innerHTML =
      '<div class="card-head"><h2>' + esc(r.name) + '</h2><button type="button" class="x" id="detail-close" aria-label="' + esc(T('detail_close')) + '" title="' + esc(T('detail_close')) + '">×</button></div>' +
      '<p class="caption">' + esc(T(r.level === 'district' ? 'd_level_district' : 'd_level_commune')) + ' · ' + esc(num(r.population, 0)) + ' ' + esc(T('d_pop')) + '</p>' +
      (flags.length ? '<p class="flags">' + flags.map((f) => '<span class="flag">' + esc(f) + '</span>').join('') + '</p>' : '') +
      '<dl>' +
      row(T('d_dist'), esc(num(r.dist_km, 1)) + ' ' + T('unit_km')) +
      row(T('d_time'), (r.t_tc == null ? esc(T('d_unreach')) : '<strong>' + esc(num(r.t_tc, 1)) + ' ' + T('unit_min') + '</strong>' + rng)) +
      row(T('d_walk'), finite(r.walk_time) ? esc(num(r.walk_time, 0)) + ' ' + T('unit_min') : '–') +
      row(T('d_car'), finite(r.car_time) ? esc(num(r.car_time, 1)) + ' ' + T('unit_min') : '–') +
      row(T('d_ratio'), finite(r.ratio_tc_car) ? esc(num(r.ratio_tc_car, 1)) + '×' : '–') +
      row(T('d_veff'), finite(r.v_eff_kmh) ? esc(num(r.v_eff_kmh, 1)) + ' ' + T('unit_kmh') : '–') +
      row(T('d_transfers'), trans) +
      row(T('d_rank'), finite(r.rank_dist) ? esc(T('d_rank_val', { a: num(r.rank_dist, 1), b: num(r.rank_time, 1), n })) : '–') +
      row(T('d_paradox'), finite(r.paradox_index) ? esc(signed(r.paradox_index, 1)) : '–') +
      row(T('d_excess'), finite(r.time_excess_pct) ? esc(signed(r.time_excess_pct, 1)) + ' ' + T('unit_pct') : '–') +
      '</dl>' + itin;
  }

  function drawScatter(ds, sc) {
    const el = $('#scatter');
    el.innerHTML = '';
    const rs = ds.raw.results[state.point].filter((r) => r.t_tc != null && finite(r.dist_km));
    const W = Math.max(260, el.clientWidth || 360);
    const H = clamp(Math.round(W * 0.78), 250, 360);
    const m = { l: 44, r: 16, t: 10, b: 40 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const xmax = Math.max(5, Math.ceil(d3.max(rs, (r) => r.dist_km) / 5) * 5);
    const ymax = Math.max(30, Math.ceil(d3.max(rs, (r) => r.t_tc) / 30) * 30);
    const x = d3.scaleLinear([0, xmax], [0, iw]);
    const y = d3.scaleLinear([0, ymax], [ih, 0]);
    const rad = d3.scaleSqrt([0, ds.maxPop], [2.2, 13]);
    const th = ds.meta.thresholds;
    const svg = d3.select(el).append('svg').attr('width', W).attr('height', H).attr('viewBox', [0, 0, W, H]).attr('role', 'img').attr('aria-label', T('scatter_aria'));
    const g = svg.append('g').attr('transform', 'translate(' + m.l + ',' + m.t + ')');
    // dead zone
    const dzx = x(Math.min(th.dead_zone_max_km, xmax)), dzy = y(Math.min(ymax, ymax));
    g.append('rect').attr('class', 'sc-dead').attr('x', 0).attr('y', 0).attr('width', dzx).attr('height', y(th.dead_zone_min_min));
    g.append('text').attr('class', 'sc-dead-lbl').attr('x', 4).attr('y', 12).text(T('scatter_dead'));
    // grid + axes
    g.append('g').attr('class', 'sc-grid').call(d3.axisLeft(y).ticks(Math.min(6, ymax / 30)).tickSize(-iw).tickFormat(''));
    g.append('g').attr('class', 'sc-axis').attr('transform', 'translate(0,' + ih + ')').call(d3.axisBottom(x).ticks(6));
    g.append('g').attr('class', 'sc-axis').call(d3.axisLeft(y).ticks(Math.min(6, ymax / 30)));
    g.append('text').attr('class', 'sc-lbl').attr('x', iw / 2).attr('y', ih + 34).attr('text-anchor', 'middle').text(T('scatter_x'));
    g.append('text').attr('class', 'sc-lbl').attr('transform', 'rotate(-90)').attr('x', -ih / 2).attr('y', -32).attr('text-anchor', 'middle').text(T('scatter_y'));
    // reference speeds: t[min] = 60 * d[km] / v
    for (const v of REF_SPEEDS) {
      let x2 = xmax, y2 = (60 * xmax) / v;
      if (y2 > ymax) { y2 = ymax; x2 = (ymax * v) / 60; }
      g.append('line').attr('class', 'sc-ref').attr('x1', 0).attr('y1', y(0)).attr('x2', x(x2)).attr('y2', y(y2));
      g.append('text').attr('class', 'sc-ref-lbl').attr('x', x(x2) - 3).attr('y', y(y2) + 11).attr('text-anchor', 'end').text(v + ' ' + T('unit_kmh'));
    }
    // dots, biggest first
    const dots = rs.slice().sort((a, b) => b.population - a.population);
    g.append('g').selectAll('circle').data(dots).join('circle')
      .attr('class', (r) => 'dot' + (r.unit_id === state.sel ? ' sel' : '') + (r.top_nah_fern ? ' nf' : r.top_fern_nah ? ' fn' : ''))
      .attr('cx', (r) => x(r.dist_km)).attr('cy', (r) => y(r.t_tc)).attr('r', (r) => rad(r.population))
      .attr('fill', (r) => classify(sc, r).c)
      .on('click', (e, r) => selectUnit(r.unit_id, true))
      .on('mousemove', (e, r) => showTip(tipHTML(sc, r), e))
      .on('mouseleave', () => showTip(null));
    g.selectAll('circle.sel').raise();
  }

  /* ------------------------------------------------------------------ selection */
  function itineraryFeatures(ds, originId, unitId) {
    if (!unitId) return null;
    return ds.segs.get(originId + '|' + unitId) || null;
  }

  function updateHintItin() {
    const el = $('#hint-itin');
    const has = cur && state.sel && itineraryFeatures(cur.ds, state.point, state.sel);
    const show = !!has && state.view === 'time' && state.mode === 'single';
    el.hidden = !show;
    el.textContent = show ? T('itin_hidden_time') : '';
  }

  function selectUnit(id, scroll) {
    if (state.mode !== 'single' || !cur) return;
    state.sel = id && cur.ds.res[state.point].has(id) ? id : null;
    mainView.select(state.sel);
    mainView.setItinerary(itineraryFeatures(cur.ds, state.point, state.sel));
    renderDetail(cur.ds);
    drawScatter(cur.ds, cur.scale);
    renderLists(cur.ds);
    updateHintItin();
    const r = state.sel ? cur.ds.res[state.point].get(state.sel) : null;
    $('#announce').textContent = r ? T('selected_announce', { name: r.name }) : '';
    $('#search').value = r ? r.name : '';
    if (scroll && state.sel) {
      const rect = $('#map').getBoundingClientRect();
      if (rect.top < 0 || rect.bottom > window.innerHeight) $('.grid').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    writeUrl();
  }

  /* ------------------------------------------------------------------ render: single */
  async function renderSingle(opts) {
    opts = opts || {};
    const my = ++token;
    const loading = $('#loading');
    const needLoad = !cache.has(state.slug);
    if (needLoad) loading.hidden = false;
    let ds;
    try { ds = await loadDataset(state.slug); } catch (e) { loading.hidden = false; loading.textContent = T('load_error') + ' ' + e.message; throw e; }
    if (my !== token) return;
    loading.hidden = true;
    const results = [];
    for (const o of ds.meta.origins) for (const r of ds.raw.results[o.id]) results.push(r);
    const scale = buildScale(state.ind, results, false);
    const pointResults = ds.raw.results[state.point];
    cur = { ds, scale };
    if (opts.reload || mainView.ds !== ds || mainView.originId !== state.point) {
      await mainView.load(ds, state.point, progress);
      if (my !== token) return;
      if (opts.camera !== false) mainView.map.jumpTo({ center: mainView.originLL, zoom: mainView.zoomFor(ds.radius * 1.03) });
    } else {
      mainView.refreshOrigin();
    }
    mainView.setFlags(state.overlays);
    mainView.recolor(scale);
    $('#legend').innerHTML = legendHTML(scale, pointResults);
    $('#hint').textContent = T('hint_' + state.ind);
    renderSummary(ds);
    renderFooter([ds]);
    $('#scatter-note').textContent = T('scatter_note', { n: num(ds.meta.summary[state.point].n_unreachable, 0) });
    $('#dl-units').innerHTML = pointResults.map((r) => '<option value="' + esc(r.name) + '"></option>').join('');
    // origin labels on the toggle buttons: full label as tooltip
    $$('#point-group button').forEach((b) => { const o = ds.origin[b.dataset.point]; b.title = o ? (o[state.lang] || o.de) : ''; });
    if (state.sel && !ds.res[state.point].has(state.sel)) state.sel = null;
    mainView.select(state.sel);
    mainView.setItinerary(itineraryFeatures(ds, state.point, state.sel));
    renderLists(ds);
    renderDetail(ds);
    drawScatter(ds, scale);
    updateHintItin();
    $('#map').setAttribute('aria-label', T(progress > 0.5 ? 'map_aria_time' : 'map_aria'));
  }

  /* ------------------------------------------------------------------ render: compare */
  function cmpRow(label, a, b) { return '<tr><th scope="row">' + esc(label) + '</th><td>' + a + '</td><td>' + b + '</td></tr>'; }

  async function renderCompare(opts) {
    opts = opts || {};
    const my = ++token;
    const [dsA, dsB] = await Promise.all([loadDataset(state.cmp.fr), loadDataset(state.cmp.de)]);
    if (my !== token) return;
    const all = [];
    for (const ds of [dsA, dsB]) for (const r of ds.raw.results[state.point]) all.push(r);
    const scale = buildScale(state.ind, all, true);
    const R = Math.max(dsA.radius, dsB.radius) * 1.03;
    if (opts.reload || cmpA.ds !== dsA || cmpB.ds !== dsB || cmpA.originId !== state.point || cmpB.originId !== state.point) {
      await Promise.all([cmpA.load(dsA, state.point, progress), cmpB.load(dsB, state.point, progress)]);
      if (my !== token) return;
      opts.camera = true;
    }
    for (const v of [cmpA, cmpB]) { v.setFlags(state.overlays); v.refreshOrigin(); }
    cmpA.recolor(scale);
    cmpB.recolor(scale);
    if (opts.camera) resetCompareCamera(R);
    cmpA.zoomR = R;
    $('#legend-cmp').innerHTML = legendHTML(scale, all);
    const cap = (ds) => esc(ds.meta.name) + ' <span class="muted">· ' + esc(ds.origin[state.point][state.lang] || ds.origin[state.point].de) + '</span>';
    $('#cmp-cap-a').innerHTML = cap(dsA);
    $('#cmp-cap-b').innerHTML = cap(dsB);
    const sa = dsA.meta.summary[state.point], sb = dsB.meta.summary[state.point];
    const head = '<thead><tr><th scope="col">' + esc(T('cmp_metric')) + '</th><th scope="col">' + esc(dsA.meta.name) + '</th><th scope="col">' + esc(dsB.meta.name) + '</th></tr></thead>';
    const f = (fn) => [fn(sa), fn(sb)];
    $('#cmp-table').innerHTML = head + '<tbody>' +
      cmpRow(T('cmp_origin'), esc(dsA.origin[state.point][state.lang] || ''), esc(dsB.origin[state.point][state.lang] || '')) +
      cmpRow(T('cmp_n'), ...f((s) => esc(num(s.n_destinations, 0)))) +
      cmpRow(T('cmp_median'), ...f((s) => esc(num(s.t_tc_median_min, 1)))) +
      cmpRow(T('cmp_veff_pw'), ...f((s) => esc(num(s.v_eff_popweighted_median_kmh, 1)))) +
      cmpRow(T('cmp_spearman'), ...f((s) => esc(num(s.spearman_dist_time, 2, 2)))) +
      cmpRow(T('cmp_share2'), ...f((s) => esc(pct(s.share_2plus_transfers, 0)))) +
      cmpRow(T('cmp_transfers'), ...f((s) => esc(num(s.median_transfers, 1)))) +
      cmpRow(T('cmp_ratio'), ...f((s) => esc(num(s.ratio_tc_car_median, 1)) + '×')) +
      cmpRow(T('cmp_dead'), ...f((s) => esc(num(s.dead_zones.n, 0)))) +
      cmpRow(T('cmp_unreach'), ...f((s) => esc(num(s.n_unreachable, 0)))) + '</tbody>';
    $('#hint').textContent = T('hint_' + state.ind);
    renderFooter([dsA, dsB]);
    for (const v of [cmpA, cmpB]) v.select(v.sel);
  }

  function resetCompareCamera(R) {
    R = R || cmpA.zoomR || 40;
    cmpA.map.resize();
    cmpB.map.resize();
    // identical metres per pixel: zoom of B follows A through the link (log2 cos ratio)
    cmpA.map.jumpTo({ center: cmpA.originLL, zoom: cmpA.zoomFor(R) });
    // enforce the link even if the zoom did not change
    const z = cmpA.map.getZoom();
    cmpB.map.jumpTo({ center: cmpB.originLL, zoom: z + Math.log2(Math.cos((cmpB.originLL[1] * Math.PI) / 180) / Math.cos((cmpA.originLL[1] * Math.PI) / 180)) });
  }

  /* ------------------------------------------------------------------ top-level actions */
  async function refresh(opts) {
    try {
      if (state.mode === 'single') await renderSingle(opts);
      else await renderCompare(opts);
    } catch (e) { console.error(e); }
    writeUrl();
  }

  function animateTo(target) {
    const views = activeViews();
    if (animTimer) animTimer.stop();
    const from = progress;
    if (from === target) { finishAnim(); return; }
    animTimer = d3.timer((elapsed) => {
      const k = Math.min(1, elapsed / ANIM_MS);
      progress = from + (target - from) * d3.easeCubicInOut(k);
      views.forEach((v) => v.setProgress(progress));
      if (k >= 1) { progress = target; animTimer.stop(); animTimer = null; finishAnim(); }
    });
  }
  let animTimer = null;
  function finishAnim() {
    $('#map').setAttribute('aria-label', T(progress > 0.5 ? 'map_aria_time' : 'map_aria'));
    updateHintItin();
    document.body.dataset.progress = String(progress);
  }

  function setMode(mode) {
    if (state.mode === mode) return;
    state.mode = mode;
    $$('#mode-group button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
    const cmp = mode === 'compare';
    $('#single').hidden = cmp;
    $('#compare').hidden = !cmp;
    $('#ctl-origin').hidden = cmp;
    $('#ctl-cmp').hidden = !cmp;
    $('#ctl-cmp2').hidden = !cmp;
    $('#tooltip').hidden = true;
    if (cmp) ensureCompareViews();
    // views created before the mode switch keep the current progress
    refresh({ reload: true, camera: true });
    if (!cmp) setTimeout(() => mainView.map.resize(), 0);
  }

  function ensureCompareViews() {
    if (cmpA) return;
    cmpA = new MapView($('#map-a'));
    cmpB = new MapView($('#map-b'));
    linkViews(cmpA, cmpB);
    for (const v of [cmpA, cmpB]) {
      v.onSelect = (id, view) => view.select(id);
      v.onHover = (id, evt, view) => {
        if (!id || !evt) return showTip(null);
        const r = view.ds.res[view.originId].get(id);
        const sc = buildScaleForView();
        showTip(tipHTML(sc, r), evt);
      };
    }
  }
  function buildScaleForView() {
    const all = [];
    for (const v of [cmpA, cmpB]) if (v.ds) for (const r of v.ds.raw.results[state.point]) all.push(r);
    return buildScale(state.ind, all, true);
  }

  /* ------------------------------------------------------------------ URL state (optional deep links) */
  function writeUrl() {
    try {
      const q = new URLSearchParams();
      q.set('lang', state.lang);
      q.set('mode', state.mode);
      if (state.mode === 'single') q.set('o', state.slug); else { q.set('fr', state.cmp.fr); q.set('de', state.cmp.de); }
      q.set('p', state.point);
      q.set('ind', state.ind);
      q.set('view', state.view);
      history.replaceState(null, '', '?' + q.toString());
    } catch (e) { /* ignore */ }
  }
  function readUrl() {
    let q;
    try { q = new URLSearchParams(location.search); } catch (e) { return; }
    let lang = q.get('lang');
    if (!lang) { try { lang = localStorage.getItem('snf_lang'); } catch (e) { lang = null; } }
    if (lang === 'de' || lang === 'fr') state.lang = lang;
    const slugs = INDEX.origins.map((o) => o.slug);
    if (slugs.includes(q.get('o'))) state.slug = q.get('o');
    const fr = INDEX.origins.filter((o) => o.country === 'FR').map((o) => o.slug);
    const de = INDEX.origins.filter((o) => o.country === 'DE').map((o) => o.slug);
    if (fr.includes(q.get('fr'))) state.cmp.fr = q.get('fr');
    if (de.includes(q.get('de'))) state.cmp.de = q.get('de');
    if (['gare', 'centre'].includes(q.get('p'))) state.point = q.get('p');
    if (IND_KEYS.includes(q.get('ind'))) state.ind = q.get('ind');
    if (q.get('view') === 'time') { state.view = 'time'; progress = 1; }
    if (q.get('mode') === 'compare') state.mode = 'compare';
  }

  /* ------------------------------------------------------------------ events */
  function bind() {
    $$('#lang-group button').forEach((b) => b.addEventListener('click', () => {
      state.lang = b.dataset.lang;
      try { localStorage.setItem('snf_lang', state.lang); } catch (e) { /* ignore */ }
      applyStaticTexts();
      for (const v of [mainView, cmpA, cmpB]) if (v) v.refreshOrigin();
      refresh({ camera: false });
    }));
    $$('#mode-group button').forEach((b) => b.addEventListener('click', () => setMode(b.dataset.mode)));
    $$('#point-group button').forEach((b) => b.addEventListener('click', () => {
      state.point = b.dataset.point;
      $$('#point-group button').forEach((x) => x.setAttribute('aria-pressed', String(x.dataset.point === state.point)));
      state.sel = null;
      refresh({ reload: true, camera: true });
    }));
    $$('#view-group button').forEach((b) => b.addEventListener('click', () => {
      if (state.view === b.dataset.view) return;
      state.view = b.dataset.view;
      $$('#view-group button').forEach((x) => x.setAttribute('aria-pressed', String(x.dataset.view === state.view)));
      animateTo(state.view === 'time' ? 1 : 0);
      updateHintItin();
      writeUrl();
    }));
    $('#sel-origin').addEventListener('change', (e) => { state.slug = e.target.value; state.sel = null; refresh({ reload: true, camera: true }); });
    $('#sel-cmp-fr').addEventListener('change', (e) => { state.cmp.fr = e.target.value; refresh({ reload: true, camera: true }); });
    $('#sel-cmp-de').addEventListener('change', (e) => { state.cmp.de = e.target.value; refresh({ reload: true, camera: true }); });
    $('#sel-ind').addEventListener('change', (e) => { state.ind = e.target.value; refresh({ camera: false }); });
    for (const k of ['iso', 'rings', 'origin']) {
      $('#ov-' + k).addEventListener('change', (e) => {
        state.overlays[k] = e.target.checked;
        for (const v of [mainView, cmpA, cmpB]) if (v && v.ds) v.setFlags(state.overlays);
      });
    }
    $('#reset-single').addEventListener('click', () => {
      if (!cur) return;
      mainView.map.easeTo({ center: mainView.originLL, zoom: mainView.zoomFor(cur.ds.radius * 1.03), duration: 500 });
    });
    $('#reset-cmp').addEventListener('click', () => resetCompareCamera());
    $('#search').addEventListener('change', (e) => {
      if (!cur) return;
      const v = e.target.value.trim().toLowerCase();
      const hit = cur.ds.raw.results[state.point].find((r) => r.name.toLowerCase() === v);
      if (hit) selectUnit(hit.unit_id, true);
    });
    document.addEventListener('click', (e) => {
      const b = e.target.closest && e.target.closest('.list button[data-id]');
      if (b) selectUnit(b.dataset.id, true);
      if (e.target.closest && e.target.closest('#detail-close')) selectUnit(null);
    });
    mainView.onSelect = (id) => selectUnit(id, false);
    mainView.onHover = (id, evt) => {
      if (!id || !evt || !cur) return showTip(null);
      showTip(tipHTML(cur.scale, cur.ds.res[state.point].get(id)), evt);
    };
    let rt = null;
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        clearTimeout(rt);
        rt = setTimeout(() => { if (cur && state.mode === 'single') drawScatter(cur.ds, cur.scale); }, 120);
      }).observe($('#scatter'));
    }
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { showTip(null); if (state.sel) selectUnit(null); } });
  }

  /* ------------------------------------------------------------------ boot */
  async function boot() {
    try {
      INDEX = await (await fetch('data/index.json')).json();
    } catch (e) {
      document.body.insertAdjacentHTML('afterbegin', '<p class="fatal">Daten / données : ' + esc(e.message) + '</p>');
      throw e;
    }
    // DE first, then FR
    INDEX.origins.sort((a, b) => (a.country === b.country ? 0 : a.country === 'DE' ? -1 : 1));
    const k = INDEX.origins.find((o) => o.slug === 'kronberg');
    state.slug = k ? k.slug : INDEX.origins[0].slug;
    state.cmp.fr = (INDEX.origins.find((o) => o.country === 'FR') || {}).slug || state.slug;
    state.cmp.de = state.slug;
    readUrl();
    applyStaticTexts();
    mainView = new MapView($('#map'));
    $('#view-group').querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === state.view)));
    bind();
    const startMode = state.mode;
    state.mode = 'single';
    if (startMode === 'compare') setMode('compare');
    else await refresh({ reload: true, camera: true });
    document.body.dataset.progress = String(progress);
    document.body.dataset.ready = '1';
  }

  window.snf = { state, get progress() { return progress; }, get views() { return { mainView, cmpA, cmpB }; }, get cur() { return cur; }, buildScale, valueOf, geodesicCircle };
  boot().catch((e) => console.error(e));
})();
