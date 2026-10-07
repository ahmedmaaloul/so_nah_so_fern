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
    cmp: { fr: 'garches', de: 'kronberg' },
    all: { region: null, origin: null, dir: 'von', dest: null, ind: 'v_eff_popweighted_kmh', tab: 'nah_fern' }
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
  const IND_KEYS = ['t_tc', 'paradox_index', 'time_excess_pct', 'ratio_tc_car', 'ratio_tc_car_peak', 'transfers'];

  function valueOf(ind, r, compare) {
    if (!r) return null;
    switch (ind) {
      case 't_tc': return r.t_tc;
      case 'paradox_index': return compare ? r.paradox_norm : r.paradox_index;
      case 'time_excess_pct': return r.time_excess_pct;
      case 'ratio_tc_car': return r.ratio_tc_car;
      case 'ratio_tc_car_peak': return r.ratio_tc_car_peak;
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
    } else if (ind === 'ratio_tc_car' || ind === 'ratio_tc_car_peak') {
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
      case 'ratio_tc_car':
      case 'ratio_tc_car_peak': return num(v, 1) + '×';
      case 'transfers': return r.transfers >= 4 && r.transfers_censored ? T('legend_trans_4') : String(r.transfers);
      default: return String(v);
    }
  }

  function legendHTML(sc, results, opts) {
    opts = opts || {};
    const anyUn = opts.unreach != null ? opts.unreach : results.some((r) => r && r.t_tc == null);
    const anyNa = opts.na != null ? opts.na : results.some((r) => r && r.t_tc != null && !finite(valueOf(sc.ind, r, sc.compare)));
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
    if (opts.tags !== false) {
      extra += '<span class="lg-item"><i class="dot" style="background:' + TAG_COLORS.nf + '"></i>' + esc(T('list_nf_title')) + '</span>';
      extra += '<span class="lg-item"><i class="dot" style="background:' + TAG_COLORS.fn + '"></i>' + esc(T('list_fn_title')) + '</span>';
    }
    if (opts.extra) extra += opts.extra;
    const clip = sc.clipHi || sc.clipLo ? ' ' + T('clamp_note') : '';
    const norm = sc.ind === 'paradox_index' && sc.compare ? ' ' + T('cmp_norm_note') : '';
    return '<div class="lg-title">' + esc(T(sc.legendKey, sc.legendParams)) + '</div><div class="lg-main">' + body + '</div><div class="lg-extra">' + extra + '</div>' +
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
    ['l-region', 'region_label'], ['t-all-map', 'all_map_title'], ['l-all-ind', 'all_ind_label'], ['l-dir', 'all_dir_label'], ['l-all-search', 'all_search_label'], ['all-clear', 'all_clear'], ['t-all-scatter', 'all_scatter_title'], ['t-all-table', 'all_table_title'],
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
    const lab = { single: 'mode_single', compare: 'mode_compare', all: 'mode_all' };
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
    $('#all-search').placeholder = T('all_search_ph');
    $('#loading-all').textContent = T('loading');
    $('#map-all').setAttribute('aria-label', T('all_map_title'));
    $$('#dir-group button').forEach((b) => { b.textContent = T('all_dir_' + b.dataset.dir); b.setAttribute('aria-pressed', String(b.dataset.dir === state.all.dir)); });
    fillOriginSelects();
    fillAllControls();
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
      (finite(speed) ? '<p class="foot-note">' + esc(T('foot_scale', { v: num(speed, 1) })) + '</p>' : '') +
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

  const mio = (v) => num(v / 1e6, 2) + ' ' + T('unit_mio');

  /** Key facts: core city (centre unit + all units) and population reachable. */
  function keyFactsHTML(s) {
    let h = '';
    if (s.core) {
      const c = s.core;
      h += '<div class="kf"><div class="kf-k">' + esc(T('kf_core', { city: c.city })) + '</div>' +
        '<div class="kf-v">' + esc(num(c.t_tc, 0)) + ' <small>' + T('unit_min') + '</small> · ' + esc(num(c.dist_km, 1)) + ' <small>' + T('unit_km') + '</small></div>' +
        '<div class="kf-s">' + esc(T('kf_core_sub', { unit: c.centre_unit, p25: num(c.t_p25, 0), p75: num(c.t_p75, 0), tr: num(c.transfers, 0), car: num(c.car_time_peak != null ? c.car_time_peak : c.car_time, 0) })) + '</div>' +
        '<div class="kf-s">' + esc(T('kf_core_all', { n: num(c.all_units.n, 0), t: num(c.all_units.t_tc_median, 0), d: num(c.all_units.dist_km_median, 1) })) + '</div></div>';
    }
    if (s.access_population) {
      const a = s.access_population;
      h += '<div class="kf"><div class="kf-k">' + esc(T('kf_access')) + '</div>' +
        '<div class="kf-v">' + esc(mio(a['60'].population)) + ' <small>(' + esc(pct(a['60'].share, 0)) + ')</small></div>' +
        '<div class="kf-s">' + esc(T('kf_access_sub', { p30: mio(a['30'].population), p45: mio(a['45'].population), s45: pct(a['45'].share, 0), p90: mio(a['90'].population), s90: pct(a['90'].share, 0) })) + '</div>' +
        '<div class="kf-s">' + esc(T('kf_radius', { pop: mio(s.population_radius) })) + '</div></div>';
    }
    if (s.outside_core) {
      const o = s.outside_core;
      h += '<div class="kf"><div class="kf-k">' + esc(T('kf_outside')) + '</div>' +
        '<div class="kf-v">' + esc(num(o.t_tc_median, 0)) + ' <small>' + T('unit_min') + '</small> · ' + esc(num(o.dist_km_median, 1)) + ' <small>' + T('unit_km') + '</small></div>' +
        '<div class="kf-s">' + esc(T('kf_outside_sub', { n: num(o.n, 0), tr: num(o.transfers_median, 0), u: num(o.n_unreachable, 0) })) + '</div></div>';
    }
    return h ? '<div class="keyfacts">' + h + '</div>' : '';
  }

  /** Reading guide built from the origin's own figures (capital vs regional metropolis). */
  function contextHTML(ds, s) {
    if (!s.core || !s.access_population || !s.outside_core) return '';
    const a = s.access_population;
    const p = {
      city: s.core.city, origin: ds.meta.name, r: num(ds.meta.radius_km, 0), pop: mio(s.population_radius),
      p60: mio(a['60'].population), s60: pct(a['60'].share, 0), tc: num(s.core.t_tc, 0), dc: num(s.core.dist_km, 1),
      to: num(s.outside_core.t_tc_median, 0), tm: num(s.t_tc_median_min, 1), sh2: pct(s.share_2plus_transfers, 0),
      u: num(s.outside_core.n_unreachable, 0)
    };
    const kind = ds.meta.country === 'FR' ? 'ctx_capital' : 'ctx_regional';
    return '<details class="context" open><summary>' + esc(T('ctx_title')) + '</summary>' +
      '<p>' + esc(T(kind, p)) + '</p><p>' + esc(T('ctx_median', p)) + '</p><p>' + esc(T('ctx_access', p)) + '</p></details>';
  }

  function renderSummary(ds) {
    const s = ds.meta.summary[state.point];
    const o = ds.origin[state.point];
    const stat = (k, v, sub) => '<div class="stat"><div class="k">' + esc(k) + '</div><div class="v">' + v + '</div><div class="s">' + esc(sub || '') + '</div></div>';
    const head = '<div class="sum-head"><h2>' + esc(ds.meta.name) + '</h2><p>' + esc(o[state.lang] || o.de) + ' · ' + esc(T('sum_core')) + ': ' + esc(ds.meta.core_city) + ' · ' + esc(T('sum_core_sub', { r: num(ds.meta.radius_km, 0) })) + '</p></div>';
    const link = INDEX.phase2 ? '<button type="button" class="linkbtn" id="to-all">' + esc(T('link_all')) + '</button>' : '';
    $('#summary').innerHTML = head.replace(/<\/div>$/, link + '</div>') + keyFactsHTML(s) + contextHTML(ds, s) + '<div class="stats">' +
      stat(T('sum_n'), esc(num(s.n_destinations, 0)), T('sum_n_sub', { n: num(s.n_unreachable, 0) })) +
      stat(T('sum_median'), esc(num(s.t_tc_median_min, 1)) + ' <small>' + T('unit_min') + '</small>', '') +
      stat(T('sum_veff'), esc(num(s.v_eff_popweighted_median_kmh, 1)) + ' <small>' + T('unit_kmh') + '</small>', T('sum_veff_sub', { v: num(s.v_eff_median_kmh, 1) })) +
      stat(T('sum_spearman'), esc(num(s.spearman_dist_time, 2, 2)), T('sum_spearman_sub')) +
      stat(T('sum_transfers'), esc(pct(s.share_2plus_transfers, 0)), T('sum_transfers_sub', { n: num(s.median_transfers, 1) })) +
      stat(T('sum_ratio'), esc(num(s.ratio_tc_car_median, 1)) + '<small>×</small>', finite(s.ratio_tc_car_peak_median) ? T('sum_ratio_sub_peak', { p: num(s.ratio_tc_car_peak_median, 1) }) : T('sum_ratio_sub')) +
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
        (ds.meta.itinerary_departure && finite(it.offset_min) ? '<p class="caption">' + esc(T(it.offset_min >= 1 ? 'd_itin_ready_wait' : 'd_itin_ready', { r: ds.meta.itinerary_departure, w: num(it.offset_min, 0) })) + '</p>' : '') +
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
      (finite(r.car_time_peak) ? row(T('d_car_peak'), esc(num(r.car_time_peak, 1)) + ' ' + T('unit_min')) + row(T('d_ratio_peak'), finite(r.ratio_tc_car_peak) ? esc(num(r.ratio_tc_car_peak, 1)) + '×' : '–') : '') +
      row(T('d_veff'), finite(r.v_eff_kmh) ? esc(num(r.v_eff_kmh, 1)) + ' ' + T('unit_kmh') : '–') +
      row(T('d_transfers'), trans) +
      row(T('d_rank'), finite(r.rank_dist) ? esc(T('d_rank_val', { a: num(r.rank_dist, 1), b: num(r.rank_time, 1), n })) : '–') +
      row(T('d_paradox'), finite(r.paradox_index) ? esc(signed(r.paradox_index, 1)) : '–') +
      row(T('d_excess'), finite(r.time_excess_pct) ? esc(signed(r.time_excess_pct, 1)) + ' ' + T('unit_pct') : '–') +
      '</dl>' + itin;
  }

  /** generic scatter: rows = [{id, dist, t, pop, color, cls, data}] */
  function scatterCore(el, rows, o) {
    el.innerHTML = '';
    const W = Math.max(260, el.clientWidth || 360);
    const H = clamp(Math.round(W * 0.78), 250, 360);
    const m = { l: 44, r: 16, t: 10, b: 40 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const xmax = Math.max(5, Math.ceil((d3.max(rows, (r) => r.dist) || 5) / 5) * 5);
    const ymax = Math.max(30, Math.ceil((d3.max(rows, (r) => r.t) || 30) / 30) * 30);
    const x = d3.scaleLinear([0, xmax], [0, iw]);
    const y = d3.scaleLinear([0, ymax], [ih, 0]);
    const rad = d3.scaleSqrt([0, o.maxPop], [2.2, 13]);
    const th = o.th;
    const svg = d3.select(el).append('svg').attr('width', W).attr('height', H).attr('viewBox', [0, 0, W, H]).attr('role', 'img').attr('aria-label', T('scatter_aria'));
    const g = svg.append('g').attr('transform', 'translate(' + m.l + ',' + m.t + ')');
    // dead zone
    const dzx = x(Math.min(th.dead_zone_max_km, xmax));
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
    const dots = rows.slice().sort((a, b) => b.pop - a.pop);
    g.append('g').selectAll('circle').data(dots).join('circle')
      .attr('class', (r) => 'dot' + (r.id === o.selId ? ' sel' : '') + (r.id === o.destId ? ' dest' : '') + (r.cls ? ' ' + r.cls : ''))
      .attr('cx', (r) => x(r.dist)).attr('cy', (r) => y(r.t)).attr('r', (r) => rad(r.pop))
      .attr('fill', (r) => r.color)
      .on('click', (e, r) => o.onClick(r))
      .on('mousemove', (e, r) => o.tip(r, e))
      .on('mouseleave', () => showTip(null));
    g.selectAll('circle.sel').raise();
    g.selectAll('circle.dest').raise();
  }

  function drawScatter(ds, sc) {
    const rs = ds.raw.results[state.point].filter((r) => r.t_tc != null && finite(r.dist_km));
    scatterCore($('#scatter'), rs.map((r) => ({ id: r.unit_id, dist: r.dist_km, t: r.t_tc, pop: r.population, color: classify(sc, r).c, cls: r.top_nah_fern ? 'nf' : r.top_fern_nah ? 'fn' : '', data: r })), {
      maxPop: ds.maxPop, th: ds.meta.thresholds, selId: state.sel,
      onClick: (row) => selectUnit(row.id, true),
      tip: (row, e) => showTip(tipHTML(sc, row.data), e)
    });
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
      cmpRow(T('cmp_core', { a: sa.core ? sa.core.city : '', b: sb.core ? sb.core.city : '' }), ...f((s) => s.core ? esc(num(s.core.t_tc, 0)) + ' ' + T('unit_min') + ' · ' + esc(num(s.core.dist_km, 1)) + ' ' + T('unit_km') + ' <span class="muted">(' + esc(s.core.centre_unit) + ')</span>' : '–')) +
      cmpRow(T('cmp_core_all'), ...f((s) => s.core ? esc(num(s.core.all_units.t_tc_median, 0)) + ' ' + T('unit_min') + ' <span class="muted">(' + esc(num(s.core.all_units.n, 0)) + ')</span>' : '–')) +
      cmpRow(T('cmp_outside'), ...f((s) => s.outside_core ? esc(num(s.outside_core.t_tc_median, 0)) + ' ' + T('unit_min') : '–')) +
      cmpRow(T('cmp_pop_radius'), ...f((s) => finite(s.population_radius) ? esc(mio(s.population_radius)) : '–')) +
      cmpRow(T('cmp_access', { m: 45 }), ...f((s) => s.access_population ? esc(mio(s.access_population['45'].population)) + ' (' + esc(pct(s.access_population['45'].share, 0)) + ')' : '–')) +
      cmpRow(T('cmp_access', { m: 60 }), ...f((s) => s.access_population ? esc(mio(s.access_population['60'].population)) + ' (' + esc(pct(s.access_population['60'].share, 0)) + ')' : '–')) +
      cmpRow(T('cmp_n'), ...f((s) => esc(num(s.n_destinations, 0)))) +
      cmpRow(T('cmp_median'), ...f((s) => esc(num(s.t_tc_median_min, 1)))) +
      cmpRow(T('cmp_veff_pw'), ...f((s) => esc(num(s.v_eff_popweighted_median_kmh, 1)))) +
      cmpRow(T('cmp_spearman'), ...f((s) => esc(num(s.spearman_dist_time, 2, 2)))) +
      cmpRow(T('cmp_share2'), ...f((s) => esc(pct(s.share_2plus_transfers, 0)))) +
      cmpRow(T('cmp_transfers'), ...f((s) => esc(num(s.median_transfers, 1)))) +
      cmpRow(T('cmp_ratio'), ...f((s) => esc(num(s.ratio_tc_car_median, 1)) + '×')) +
      cmpRow(T('cmp_ratio_peak'), ...f((s) => finite(s.ratio_tc_car_peak_median) ? esc(num(s.ratio_tc_car_peak_median, 1)) + '×' : '–')) +
      cmpRow(T('cmp_dead'), ...f((s) => esc(num(s.dead_zones.n, 0)))) +
      cmpRow(T('cmp_unreach'), ...f((s) => esc(num(s.n_unreachable, 0)))) + '</tbody>' +
      (sa.population_radius && sb.population_radius ? '<caption class="cmp-context">' + esc(T('cmp_context', { a: dsA.meta.core_city, b: dsB.meta.core_city, pa: mio(sa.population_radius), pb: mio(sb.population_radius), r: num(dsA.meta.radius_km, 0) })) + '</caption>' : '');
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

  /* ------------------------------------------------------------------ phase 2: all communes of a region */
  const P2_INDS = ['v_eff_popweighted_kmh', 't_p50_median', 'ratio_tc_car_median', 'ratio_tc_car_peak_median'];
  const p2cache = new Map();
  let allView = null;
  let allCur = null; // {p2, sc, o, d}
  let tableToken = 0;

  function preparePhase2(raw) {
    const ids = raw.matrix.ids;
    const N = ids.length;
    const feats = raw.units.features;
    const props = feats.map((f) => f.properties);
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const f of feats) for (const poly of f.geometry.coordinates) for (const ring of poly) for (const c of ring) {
      if (c[0] < x0) x0 = c[0]; if (c[0] > x1) x1 = c[0]; if (c[1] < y0) y0 = c[1]; if (c[1] > y1) y1 = c[1];
    }
    return {
      raw, meta: raw.meta, N, ids, feats, props,
      idIdx: new Map(ids.map((id, i) => [id, i])),
      T: raw.matrix.t_p50, C: raw.matrix.car, CP: raw.matrix.car_peak || null, D: raw.matrix.dist,
      bbox: [[x0, y0], [x1, y1]],
      maxPop: d3.max(props, (p) => p.population) || 1
    };
  }

  function loadPhase2(regionId) {
    if (!p2cache.has(regionId)) {
      const e = INDEX.phase2.find((x) => x.region.id === regionId);
      p2cache.set(regionId, fetch('data/' + e.file).then((r) => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(preparePhase2));
    }
    return p2cache.get(regionId);
  }

  /** matrix values for origin o and destination d (indices); dir 'von' = row o, 'nach' = column o. -1 means no value. */
  function pv(p2, o, d, dir) {
    const k = dir === 'von' ? o * p2.N + d : d * p2.N + o;
    const t = p2.T[k], c = p2.C[k], cp = p2.CP ? p2.CP[k] : -1, dist = p2.D[o * p2.N + d];
    return { t: t < 0 ? null : t, car: c < 0 ? null : c, carp: cp < 0 ? null : cp, dist: dist < 0 ? null : dist };
  }

  function buildAllScale(ind, props) {
    const vals = props.map((p) => p[ind]).filter(finite).sort(d3.ascending);
    const q = (f) => d3.quantileSorted(vals, f);
    const vmin = vals[0], vmax = vals[vals.length - 1];
    let lo, hi, interp, fd = 0;
    if (ind === 'v_eff_popweighted_kmh') {
      lo = Math.floor(q(0.02)); hi = Math.ceil(q(0.98));
      interp = (u) => d3.interpolateYlGnBu(0.08 + 0.92 * u);
    } else if (ind === 't_p50_median') {
      lo = Math.floor(q(0.02) / 10) * 10; hi = Math.ceil(q(0.98) / 10) * 10;
      interp = (u) => d3.interpolateYlOrRd(0.07 + 0.93 * u);
    } else {
      lo = Math.floor(vmin); hi = Math.ceil(q(0.98));
      interp = (u) => d3.interpolateBuPu(0.08 + 0.92 * u);
    }
    if (hi <= lo) hi = lo + 1;
    if (hi - lo < 8) fd = 1;
    const ticks = d3.range(5).map((k) => lo + ((hi - lo) * k) / 4);
    return {
      ind, type: 'seq', domain: [lo, hi], ticks, clipHi: vmax > hi, clipLo: vmin < lo,
      fn: (v) => interp(clamp((v - lo) / (hi - lo), 0, 1)), fmt: (v) => num(v, fd), legendKey: 'legend_all_' + ind
    };
  }

  function dirText(name) {
    return T('all_dir_' + state.all.dir + '_txt', { name });
  }

  class AllView {
    constructor(el) {
      this.el = el;
      this.p2 = null;
      this.marks = [];
      this.onClick = null;
      this.onHover = null;
      this.ready = new Promise((resolve) => {
        this.map = new maplibregl.Map({
          container: el,
          style: { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': BG } }] },
          center: [8.5, 50.1], zoom: 8, minZoom: 5, maxZoom: 13,
          attributionControl: false, dragRotate: false, pitchWithRotate: false, renderWorldCopies: false, fadeDuration: 0
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
      map.addSource('units', { type: 'geojson', data: { type: 'FeatureCollection', features: [] }, promoteId: 'i', tolerance: 0.2 });
      map.addLayer({ id: 'fill', type: 'fill', source: 'units', paint: { 'fill-color': ['coalesce', ['feature-state', 'c'], NA_COLOR], 'fill-opacity': 0.88 } });
      map.addLayer({ id: 'hatch', type: 'fill', source: 'units', paint: { 'fill-pattern': 'hatch', 'fill-opacity': ['case', ['==', ['feature-state', 'un'], true], 0.9, 0] } });
      map.addLayer({ id: 'line', type: 'line', source: 'units', paint: { 'line-color': '#ffffff', 'line-width': 0.6, 'line-opacity': 0.85 } });
      map.addLayer({ id: 'dest-line', type: 'line', source: 'units', filter: ['==', ['get', 'i'], -1], paint: { 'line-color': '#f59e0b', 'line-width': 4 } });
      map.addLayer({ id: 'sel-line', type: 'line', source: 'units', filter: ['==', ['get', 'i'], -1], paint: { 'line-color': '#111827', 'line-width': 3 } });
      map.on('click', (e) => {
        const f = map.queryRenderedFeatures(e.point, { layers: ['fill'] })[0];
        if (this.onClick) this.onClick(f ? f.properties.i : null);
      });
      map.on('mousemove', (e) => {
        const f = map.queryRenderedFeatures(e.point, { layers: ['fill'] })[0];
        map.getCanvas().style.cursor = f ? 'pointer' : '';
        if (this.onHover) this.onHover(f ? f.properties.i : null, e.originalEvent);
      });
      map.on('mouseout', () => { if (this.onHover) this.onHover(null, null); });
    }
    fit(duration) {
      if (!this.p2) return;
      this.map.resize();
      this.map.fitBounds(this.p2.bbox, { padding: 18, duration: duration || 0 });
    }
    setRegion(p2) {
      this.p2 = p2;
      this.map.removeFeatureState({ source: 'units' });
      this.map.getSource('units').setData({ type: 'FeatureCollection', features: p2.feats });
      this.fit(0);
    }
    paint(colors) {
      const map = this.map;
      for (let i = 0; i < colors.length; i++) map.setFeatureState({ source: 'units', id: i }, { c: colors[i].c, un: !!colors[i].un });
    }
    setSel(o, d) {
      const f = (i) => ['==', ['get', 'i'], i == null ? -1 : i];
      this.map.setFilter('sel-line', f(o));
      this.map.setFilter('dest-line', f(d));
      this.marks.forEach((m) => m.remove());
      this.marks = [];
      const add = (i, cls) => {
        if (i == null) return;
        const p = this.p2.props[i];
        const el = document.createElement('div');
        el.className = 'maplbl sel ' + cls;
        el.textContent = p.name;
        this.marks.push(new maplibregl.Marker({ element: el, anchor: 'bottom', offset: [0, -4] }).setLngLat([p.lon, p.lat]).addTo(this.map));
      };
      add(o, 'o');
      if (d != null && d !== o) add(d, 'd');
    }
  }

  function fillAllControls() {
    if (!INDEX || !INDEX.phase2) return;
    $('#sel-region').innerHTML = INDEX.phase2.map((e) => '<option value="' + e.region.id + '"' + (e.region.id === state.all.region ? ' selected' : '') + '>' + esc(e.region[state.lang] + ' (' + e.country + ')') + '</option>').join('');
    $('#sel-all-ind').innerHTML = P2_INDS.map((k) => '<option value="' + k + '"' + (k === state.all.ind ? ' selected' : '') + '>' + esc(T('all_ind_' + k)) + '</option>').join('');
  }

  function levelTxt(p) { return T(p.level === 'district' ? 'd_level_district' : 'd_level_commune'); }

  function allTipHTML(p2, i) {
    const a = state.all;
    const p = p2.props[i];
    const lvl = p.level === 'district' ? ' <span class="lvl">' + esc(T('d_level_district')) + '</span>' : '';
    const o = allCur ? allCur.o : null;
    let body;
    if (o == null) {
      const v = p[a.ind];
      const unit = a.ind === 'v_eff_popweighted_kmh' ? ' ' + T('unit_kmh') : a.ind === 't_p50_median' ? ' ' + T('unit_min') : '×';
      body = '<div>' + esc(T('all_ind_' + a.ind)) + ': ' + esc(finite(v) ? num(v, 1) + unit : '–') + '</div><div>' + esc(num(p.population, 0)) + ' ' + esc(T('unit_persons')) + '</div>';
    } else if (i === o) {
      body = '<div>' + esc(T('d_origin_commune')) + '</div>';
    } else {
      const v = pv(p2, o, i, a.dir);
      body = '<div>' + esc(T('tip_dist')) + ': ' + esc(num(v.dist, 1)) + ' ' + T('unit_km') + '</div>' +
        '<div>' + esc(T('tip_time')) + ' ' + esc(T('tc')) + ': ' + esc(v.t == null ? T('a_no_value') : num(v.t, 0) + ' ' + T('unit_min')) + '</div>' +
        '<div>' + esc(T('tip_car')) + ': ' + esc(v.car == null ? '–' : num(v.car, 1) + ' ' + T('unit_min')) + '</div>' +
        '<div>' + esc(T('tip_ratio')) + ' ' + esc(T('tc')) + '/' + esc(T('tip_car')) + ': ' + esc(v.t != null && v.car ? num(v.t / v.car, 1) + '×' : '–') + '</div>' +
        (v.carp != null ? '<div>' + esc(T('tip_car_peak')) + ': ' + esc(num(v.carp, 1) + ' ' + T('unit_min')) + (v.t != null ? ' · ' + esc(num(v.t / v.carp, 1)) + '×' : '') + '</div>' : '') +
        '<div class="muted">' + esc(dirText(p2.props[o].name)) + '</div>';
    }
    return '<strong>' + esc(p.name) + '</strong>' + lvl + body;
  }

  function renderAllSummary(p2) {
    const m = p2.meta, s = m.summary;
    const stat = (k, v, sub) => '<div class="stat"><div class="k">' + esc(k) + '</div><div class="v">' + v + '</div><div class="s">' + esc(sub || '') + '</div></div>';
    $('#all-summary').innerHTML =
      '<div class="sum-head"><h2>' + esc(m.region[state.lang]) + '</h2><p>' + esc(T('all_sum_sub_core', { c: m.core_city, r: num(m.radius_km, 0) })) + '</p></div><div class="stats">' +
      stat(T('all_sum_n_units'), esc(num(s.n_units, 0)), '') +
      stat(T('all_sum_n_pairs'), esc(num(s.n_pairs, 0)), T('all_sum_n_pairs_sub', { n: num(s.n_pairs_unreached, 0) })) +
      stat(T('all_sum_t'), esc(num(s.t_p50_median, 1)) + ' <small>' + T('unit_min') + '</small>', '') +
      stat(T('all_sum_v'), esc(num(s.v_eff_median_kmh, 1)) + ' <small>' + T('unit_kmh') + '</small>', '') +
      stat(T('all_sum_spearman'), esc(num(s.spearman_dist_time, 2, 2)), T('sum_spearman_sub')) +
      stat(T('all_sum_ratio'), esc(num(s.ratio_tc_car_median, 1)) + '<small>×</small>', finite(s.ratio_tc_car_peak_median) ? T('sum_ratio_sub_peak', { p: num(s.ratio_tc_car_peak_median, 1) }) : T('sum_ratio_sub')) +
      stat(T('all_sum_dead'), esc(pct(s.share_dead_zone_pairs_below_dist, 1)), T('all_sum_dead_sub', { d: num(m.thresholds.dead_zone_max_km, 0) })) +
      '</div>';
  }

  function renderAllDetail(p2, o, d) {
    const el = $('#all-detail');
    if (o == null) {
      el.innerHTML = '<h2>' + esc(T('all_card_title')) + '</h2><p class="caption">' + esc(T('all_card_empty')) + '</p>';
      return;
    }
    const p = p2.props[o];
    const row = (k, v) => '<div class="row"><dt>' + esc(k) + '</dt><dd>' + v + '</dd></div>';
    let pair = '';
    if (d != null && d !== o) {
      const q = p2.props[d];
      const v = pv(p2, o, d, state.all.dir);
      pair = '<h3>' + esc(T('a_pair', { a: p.name, b: q.name })) + '</h3><p class="caption">' + esc(T('a_pair_dir_' + state.all.dir, { a: p.name, b: q.name })) + '</p><dl id="all-pair-dl">' +
        row(T('a_dist'), '<span data-k="dist">' + esc(num(v.dist, 2)) + '</span> ' + T('unit_km')) +
        row(T('a_time'), v.t == null ? esc(T('a_no_value')) : '<strong data-k="t">' + esc(num(v.t, 0)) + '</strong> ' + T('unit_min')) +
        row(T('a_car'), v.car == null ? '–' : '<span data-k="car">' + esc(num(v.car, 1)) + '</span> ' + T('unit_min')) +
        row(T('a_ratio_pair'), v.t != null && v.car ? esc(num(v.t / v.car, 1)) + '×' : '–') +
        (v.carp != null ? row(T('a_car_peak'), '<span data-k="carp">' + esc(num(v.carp, 1)) + '</span> ' + T('unit_min')) + row(T('a_ratio_pair_peak'), v.t != null ? esc(num(v.t / v.carp, 1)) + '×' : '–') : '') +
        row(T('a_veff_pair'), v.t ? esc(num((v.dist / v.t) * 60, 1)) + ' ' + T('unit_kmh') : '–') +
        '</dl><button type="button" class="reset-btn" id="all-clear-dest">' + esc(T('a_clear_dest')) + '</button>';
    }
    el.innerHTML =
      '<div class="card-head"><h2>' + esc(p.name) + '</h2><button type="button" class="x" id="all-detail-close" aria-label="' + esc(T('all_clear')) + '" title="' + esc(T('all_clear')) + '">×</button></div>' +
      '<p class="caption">' + esc(levelTxt(p)) + ' · ' + esc(num(p.population, 0)) + ' ' + esc(T('d_pop')) + '</p><dl>' +
      row(T('a_n_reached'), esc(num(p.n_reached, 0))) +
      row(T('a_n_unreached'), esc(num(p.n_unreached, 0))) +
      row(T('a_t_median'), esc(num(p.t_p50_median, 0)) + ' ' + T('unit_min')) +
      row(T('a_v_median'), esc(num(p.v_eff_median_kmh, 1)) + ' ' + T('unit_kmh')) +
      row(T('a_v_pw'), esc(num(p.v_eff_popweighted_kmh, 1)) + ' ' + T('unit_kmh')) +
      row(T('a_spearman'), finite(p.spearman_dist_time) ? esc(num(p.spearman_dist_time, 2, 2)) : '–') +
      row(T('a_ratio'), esc(num(p.ratio_tc_car_median, 1)) + '×') +
      (finite(p.ratio_tc_car_peak_median) ? row(T('a_ratio_peak'), esc(num(p.ratio_tc_car_peak_median, 1)) + '×') : '') +
      row(T('a_dead'), esc(num(p.n_dead_zones, 0))) +
      '</dl>' + pair;
  }

  function renderAllScatter(p2, o, sc) {
    const el = $('#scatter-all');
    const note = $('#all-scatter-note');
    if (o == null) {
      el.innerHTML = '<p class="caption pad">' + esc(T('all_scatter_empty')) + '</p>';
      note.textContent = '';
      return;
    }
    const a = state.all;
    const rows = [];
    let unr = 0;
    for (let d = 0; d < p2.N; d++) {
      if (d === o) continue;
      const v = pv(p2, o, d, a.dir);
      if (v.t == null || v.dist == null) { unr++; continue; }
      rows.push({ id: d, dist: v.dist, t: v.t, pop: p2.props[d].population, color: sc.fn(v.t), cls: '', data: d });
    }
    note.textContent = T('all_scatter_note', { n: num(unr, 0) });
    scatterCore(el, rows, {
      maxPop: p2.maxPop, th: p2.meta.thresholds, selId: null, destId: a.dest != null ? a.dest : null,
      onClick: (row) => setAllDest(row.id === state.all.dest ? null : row.id),
      tip: (row, e) => showTip(allTipHTML(p2, row.data), e)
    });
  }

  function renderAllPairs(p2) {
    const a = state.all;
    const rows = p2.raw.pairs_top.filter((r) => r.kind === a.tab);
    const tab = (k) => '<button type="button" role="tab" data-tab="' + k + '" aria-selected="' + (a.tab === k) + '" class="tab' + (a.tab === k ? ' on' : '') + '">' + esc(T(k === 'nah_fern' ? 'pairs_nf' : 'pairs_fn')) + '</button>';
    $('#all-pairs').innerHTML = '<h2>' + esc(T('pairs_title')) + '</h2><div class="tabs" role="tablist">' + tab('nah_fern') + tab('fern_nah') + '</div>' +
      '<p class="caption">' + esc(T(a.tab === 'nah_fern' ? 'pairs_nf_sub' : 'pairs_fn_sub')) + '. ' + esc(T('pairs_hint')) + '</p>' +
      '<ol class="pairlist">' + rows.map((r, i) =>
        '<li><button type="button" class="pairbtn" data-from="' + esc(r.from_id) + '" data-to="' + esc(r.to_id) + '">' +
        '<span class="rk">' + (i + 1) + '</span><span class="nm">' + esc(r.from_name) + ' → ' + esc(r.to_name) + '</span>' +
        '<span class="mt">' + esc(num(r.dist_km, 1)) + ' ' + T('unit_km') + ' · ' + esc(num(r.t_p50, 0)) + ' ' + T('unit_min') + ' · ' + esc(num(r.v_eff_kmh, 1)) + ' ' + T('unit_kmh') + '</span></button></li>').join('') + '</ol>';
  }

  async function renderAllTable() {
    const el = $('#all-table');
    const my = ++tableToken;
    el.innerHTML = '<p class="caption">' + esc(T('all_table_loading')) + '</p>';
    let list;
    try { list = await Promise.all(INDEX.phase2.map((e) => loadPhase2(e.region.id))); } catch (e) { console.error(e); return; }
    if (my !== tableToken) return;
    const rn = (p2) => esc(p2.meta.region[state.lang]);
    const sumRow = (label, fn) => '<tr><th scope="row">' + esc(label) + '</th>' + list.map((p2) => '<td>' + fn(p2.meta.summary, p2.meta) + '</td>').join('') + '</tr>';
    let h = '<table class="cmp-table"><thead><tr><th scope="col">' + esc(T('all_table_metric')) + '</th>' + list.map((p2) => '<th scope="col">' + rn(p2) + '</th>').join('') + '</tr></thead><tbody>' +
      sumRow(T('all_sum_n_units'), (s) => esc(num(s.n_units, 0))) +
      sumRow(T('all_sum_n_pairs'), (s) => esc(num(s.n_pairs, 0))) +
      sumRow(T('a_n_unreached') + ' (' + T('all_sum_n_pairs') + ')', (s) => esc(num(s.n_pairs_unreached, 0))) +
      sumRow(T('all_sum_t') + ' (' + T('unit_min') + ')', (s) => esc(num(s.t_p50_median, 1))) +
      sumRow(T('all_sum_v') + ' (' + T('unit_kmh') + ')', (s) => esc(num(s.v_eff_median_kmh, 1))) +
      sumRow(T('all_sum_spearman'), (s) => esc(num(s.spearman_dist_time, 2, 2))) +
      sumRow(T('all_sum_ratio'), (s) => esc(num(s.ratio_tc_car_median, 1)) + '×') +
      sumRow(T('all_sum_ratio_peak'), (s) => finite(s.ratio_tc_car_peak_median) ? esc(num(s.ratio_tc_car_peak_median, 1)) + '×' : '–') +
      sumRow(T('all_sum_dead') + ' (' + T('all_sum_dead_sub', { d: num(list[0].meta.thresholds.dead_zone_max_km, 0) }) + ')', (s) => esc(pct(s.share_dead_zone_pairs_below_dist, 1))) +
      '</tbody></table>';
    // by distance band
    const keys = [];
    for (const p2 of list) for (const k of Object.keys(p2.raw.by_band)) if (!keys.includes(k)) keys.push(k);
    const lo = (k) => { const m = /(\d+)/.exec(k); return m ? +m[1] : 0; };
    keys.sort((x, y) => lo(x) - lo(y));
    const label = (k) => { const m = /^\[(\d+),\s*(\d+)\)$/.exec(k); return m ? T('all_band_label', { a: m[1], b: m[2] }) : k; };
    h += '<h3>' + esc(T('all_table_bands')) + '</h3><table class="cmp-table bands"><thead><tr><th rowspan="2" scope="col">' + esc(T('all_table_metric')) + '</th>' +
      list.map((p2) => '<th colspan="5" scope="colgroup" class="grp">' + rn(p2) + '</th>').join('') + '</tr><tr>' +
      list.map(() => ['n', 't', 'v', 'r', 'rp'].map((c) => '<th scope="col" class="sub">' + esc(T('all_band_col_' + c)) + '</th>').join('')).join('') + '</tr></thead><tbody>' +
      keys.map((k) => '<tr><th scope="row">' + esc(label(k)) + '</th>' + list.map((p2) => {
        const b = p2.raw.by_band[k];
        if (!b) return '<td>–</td><td>–</td><td>–</td><td>–</td><td>–</td>';
        return '<td>' + esc(num(b.n, 0)) + '</td><td>' + esc(num(b.t_p50_median, 0)) + '</td><td>' + esc(num(b.v_eff_median, 1)) + '</td><td>' + esc(num(b.ratio_tc_car_median, 1)) + '×</td><td>' + (finite(b.ratio_tc_car_peak_median) ? esc(num(b.ratio_tc_car_peak_median, 1)) + '×' : '–') + '</td>';
      }).join('') + '</tr>').join('') + '</tbody></table><p class="caption">' + esc(T('all_bands_note')) + '</p>';
    el.innerHTML = h;
    renderFooter(list);
  }

  async function renderAll(opts) {
    opts = opts || {};
    const my = ++token;
    const a = state.all;
    if (!a.region) a.region = INDEX.phase2[0].region.id;
    const loading = $('#loading-all');
    loading.hidden = false;
    loading.textContent = T('loading');
    let p2;
    try { p2 = await loadPhase2(a.region); } catch (e) { loading.textContent = T('load_error') + ' ' + e.message; throw e; }
    if (my !== token) return;
    if (!allView) {
      allView = new AllView($('#map-all'));
      allView.onClick = (i) => {
        if (i == null || !allCur) return;
        const id = allCur.p2.ids[i];
        if (state.all.origin === id) setAllOrigin(null); else setAllOrigin(id);
      };
      allView.onHover = (i, evt) => {
        if (i == null || !evt || !allCur) return showTip(null);
        showTip(allTipHTML(allCur.p2, i), evt);
      };
    }
    await allView.ready;
    if (my !== token) return;
    loading.hidden = true;
    if (allView.p2 !== p2) allView.setRegion(p2); else if (opts.camera) allView.fit(0);
    const o = a.origin != null && p2.idIdx.has(a.origin) ? p2.idIdx.get(a.origin) : null;
    if (o == null) { a.origin = null; a.dest = null; }
    let d = o != null && a.dest != null && p2.idIdx.has(a.dest) ? p2.idIdx.get(a.dest) : null;
    if (d == null) a.dest = null;
    // colours
    let sc, colors, anyUn = false;
    if (o == null) {
      sc = buildAllScale(a.ind, p2.props);
      colors = p2.props.map((p) => (finite(p[a.ind]) ? { c: sc.fn(p[a.ind]) } : { c: NA_COLOR }));
    } else {
      sc = buildScale('t_tc', [{ t_tc: 180 }], false);
      sc.legendKey = 'legend_all_time';
      sc.legendParams = { dir: T('all_dir_' + a.dir + '_short') };
      colors = new Array(p2.N);
      for (let i = 0; i < p2.N; i++) {
        if (i === o) { colors[i] = { c: NA_COLOR }; continue; }
        const t = pv(p2, o, i, a.dir).t;
        if (t == null) { colors[i] = { c: UNREACH_COLOR, un: true }; anyUn = true; } else colors[i] = { c: sc.fn(t) };
      }
    }
    allCur = { p2, sc, o, d, colors };
    showTip(null);
    allView.paint(colors);
    allView.setSel(o, d);
    // legend, hint, controls
    let extra = '';
    if (o != null) extra += '<span class="lg-item"><i class="sw origin"></i>' + esc(T('legend_origin')) + '</span>';
    if (d != null) extra += '<span class="lg-item"><i class="sw dest"></i>' + esc(T('legend_dest')) + '</span>';
    $('#legend-all').innerHTML = legendHTML(sc, [], { unreach: anyUn, na: false, tags: false, extra });
    $('#hint-all').textContent = o == null ? T('hint_all_' + a.ind) : T('hint_all_origin', { dir: dirText(p2.props[o].name) });
    $('#ctl-all-ind').hidden = o != null;
    $('#ctl-dir').hidden = o == null;
    $('#all-clear').hidden = o == null;
    $$('#dir-group button').forEach((b) => { b.textContent = T('all_dir_' + b.dataset.dir); b.setAttribute('aria-pressed', String(b.dataset.dir === a.dir)); });
    $('#sel-all-ind').value = a.ind;
    $('#sel-region').value = a.region;
    $('#dl-all').innerHTML = p2.props.map((p) => '<option value="' + esc(p.name) + '"></option>').join('');
    renderAllSummary(p2);
    renderAllDetail(p2, o, d);
    renderAllScatter(p2, o, sc);
    renderAllPairs(p2);
    $('#announce').textContent = o != null ? T('selected_announce', { name: p2.props[o].name }) : '';
    writeUrl();
    if (opts.table !== false) renderAllTable();
  }

  function setAllOrigin(id, dest, dir) {
    state.all.origin = id;
    state.all.dest = dest || null;
    if (dir) state.all.dir = dir;
    $('#all-search').value = '';
    renderAll({ table: false });
  }
  function setAllDest(id) {
    state.all.dest = id == null ? null : allCur.p2.ids[id];
    renderAll({ table: false });
  }

  function openAll(regionId, unitId) {
    const e = INDEX.phase2.find((x) => x.region.id === regionId) || INDEX.phase2[0];
    state.all.region = e.region.id;
    state.all.origin = unitId || null;
    state.all.dest = null;
    if (state.mode === 'all') renderAll(); else setMode('all');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /* ------------------------------------------------------------------ top-level actions */
  async function refresh(opts) {
    try {
      if (state.mode === 'single') await renderSingle(opts);
      else if (state.mode === 'all') await renderAll(opts);
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
    const is = (k) => mode === k;
    $('#single').hidden = !is('single');
    $('#compare').hidden = !is('compare');
    $('#all').hidden = !is('all');
    $('#ctl-origin').hidden = !is('single');
    $('#ctl-cmp').hidden = !is('compare');
    $('#ctl-cmp2').hidden = !is('compare');
    $('#ctl-region').hidden = !is('all');
    for (const id of ['ctl-point', 'ctl-ind', 'ctl-view', 'ctl-ov']) $('#' + id).hidden = is('all');
    $('#tooltip').hidden = true;
    if (is('compare')) ensureCompareViews();
    // views created before the mode switch keep the current progress
    refresh({ reload: true, camera: true });
    if (is('single')) setTimeout(() => mainView.map.resize(), 0);
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
      if (state.mode === 'single') q.set('o', state.slug);
      else if (state.mode === 'compare') { q.set('fr', state.cmp.fr); q.set('de', state.cmp.de); }
      else {
        q.set('reg', state.all.region || '');
        if (state.all.origin) { q.set('u', state.all.origin); q.set('dir', state.all.dir); }
        if (state.all.dest) q.set('d', state.all.dest);
        q.set('aind', state.all.ind);
      }
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
    if (q.get('mode') === 'all' && INDEX.phase2) state.mode = 'all';
    if (INDEX.phase2) {
      const rid = q.get('reg');
      if (INDEX.phase2.some((e) => e.region.id === rid)) state.all.region = rid;
      if (q.get('u')) state.all.origin = q.get('u');
      if (q.get('d')) state.all.dest = q.get('d');
      if (q.get('dir') === 'nach') state.all.dir = 'nach';
      if (P2_INDS.includes(q.get('aind'))) state.all.ind = q.get('aind');
    }
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
    $('#sel-region').addEventListener('change', (e) => { state.all.region = e.target.value; state.all.origin = null; state.all.dest = null; renderAll({ camera: true }); });
    $('#sel-all-ind').addEventListener('change', (e) => { state.all.ind = e.target.value; renderAll({ table: false }); });
    $$('#dir-group button').forEach((b) => b.addEventListener('click', () => { state.all.dir = b.dataset.dir; renderAll({ table: false }); }));
    $('#all-clear').addEventListener('click', () => setAllOrigin(null));
    $('#all-search').addEventListener('change', (e) => {
      if (!allCur) return;
      const v = e.target.value.trim().toLowerCase();
      const hit = allCur.p2.props.find((p) => p.name.toLowerCase() === v);
      if (hit) setAllOrigin(hit.unit_id);
    });
    document.addEventListener('click', (e) => {
      const t = e.target.closest ? e.target : null;
      if (!t) return;
      const pb = t.closest('.pairbtn');
      if (pb) {
        setAllOrigin(pb.dataset.from, pb.dataset.to, 'von');
        const r = $('#map-all').getBoundingClientRect();
        if (r.top < 0 || r.bottom > window.innerHeight) $('#all .grid').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
      const tab = t.closest('#all-pairs .tab');
      if (tab && allCur) { state.all.tab = tab.dataset.tab; renderAllPairs(allCur.p2); }
      if (t.closest('#all-detail-close')) setAllOrigin(null);
      if (t.closest('#all-clear-dest') && allCur) setAllDest(null);
      if (t.closest('#to-all') && cur) {
        const r = cur.ds.raw.results[state.point].find((x) => x.is_origin_commune);
        openAll(cur.ds.meta.region.id, r ? r.unit_id : null);
      }
    });
    let rt = null;
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        clearTimeout(rt);
        rt = setTimeout(() => {
          if (cur && state.mode === 'single') drawScatter(cur.ds, cur.scale);
          if (allCur && state.mode === 'all') renderAllScatter(allCur.p2, allCur.o, allCur.sc);
        }, 120);
      }).observe($('#scatter'));
    if (window.ResizeObserver) new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => { if (allCur && state.mode === 'all') renderAllScatter(allCur.p2, allCur.o, allCur.sc); }, 120); }).observe($('#scatter-all'));
    }
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      showTip(null);
      if (state.mode === 'single' && state.sel) selectUnit(null);
      else if (state.mode === 'all' && state.all.origin) setAllOrigin(null);
    });
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
    if (!INDEX.phase2) $('#mode-group button[data-mode=all]').hidden = true;
    else state.all.region = INDEX.phase2[0].region.id;
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
    if (startMode !== 'single') setMode(startMode);
    else await refresh({ reload: true, camera: true });
    document.body.dataset.progress = String(progress);
    document.body.dataset.ready = '1';
  }

  window.snf = { state, get all() { return { view: allView, cur: allCur, p2cache }; }, get progress() { return progress; }, get views() { return { mainView, cmpA, cmpB }; }, get cur() { return cur; }, buildScale, valueOf, geodesicCircle };
  boot().catch((e) => console.error(e));
})();
