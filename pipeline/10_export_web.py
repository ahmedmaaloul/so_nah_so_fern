"""
Étape 10 — Export des données du site web (web/data/<slug>.json).

    uv run python pipeline/10_export_web.py --config config/garches.yaml

Rassemble, pour une origine, tout ce que le site affiche dans UN fichier JSON
autonome (pas de serveur, pas d'appel réseau) :

  meta          nom, région, pays, date, fenêtre, seuils, échelles, points d'origine
                (id, libellés DE/FR, lon/lat), synthèse de 07, sources (attributions)
  units         FeatureCollection des contours simplifiés (couche units_geo de 09)
                propriétés : unit_id, name, level, population
  units_time    {origin_id: FeatureCollection} contours du cartogramme, MÊMES sommets
                dans le même ordre que `units` (animation sommet par sommet)
  results       {origin_id: [ {unit_id, name, lon, lat, tlon, tlat, indicateurs…} ]}
                tlon/tlat = position du point dans le cartogramme temporel
  isochrones    FeatureCollection (origin_id, minutes, area_km2)
  rings         FeatureCollection (origin_id, km)
  itineraries   {summary: [...], segments: FeatureCollection} (05 --itineraries)

Coordonnées arrondies à web.coord_decimals (repli : 5, ~1 m) ; isochrones et
itinéraires simplifiés à web.simplify_m (repli : 25 m) dans le CRS métrique.
Met aussi à jour web/data/index.json (liste des origines exportées).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from common import ROOT, cli, get_logger, load_config, outputs_dir, processed_dir

LOG = get_logger("10_export_web")

DEFAULT_DECIMALS = 5
DEFAULT_SIMPLIFY_M = 25
WEB_DATA = ROOT / "web" / "data"

RESULT_COLS = ["unit_id", "name", "level", "parent_id", "population", "lon", "lat", "dist_km", "dist_km_core",
               "travel_time_p25", "t_tc", "travel_time_p75", "walk_time", "car_time", "v_eff_kmh", "rank_dist",
               "rank_time", "paradox_index", "paradox_norm", "time_excess_pct", "ratio_tc_car", "transfers",
               "transfers_censored", "walk_only", "dead_zone", "top_nah_fern", "top_fern_nah", "is_origin_commune"]


def clean(v):
    """Valeur JSON : NaN → None, numpy → python, flottants arrondis à 3 décimales."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) else (int(f) if f.is_integer() else round(f, 3))
    return v


def fc(gdf: gpd.GeoDataFrame, props: list[str], decimals: int) -> dict:
    """GeoDataFrame (EPSG:4326) → FeatureCollection, coordonnées arrondies."""
    geoms = shapely.set_precision(gdf.geometry.to_numpy(), 10 ** -decimals, mode="pointwise")
    feats = []
    for (_, row), g in zip(gdf.iterrows(), geoms):
        feats.append({"type": "Feature", "properties": {p: clean(row[p]) for p in props},
                      "geometry": shapely.geometry.mapping(g) if g is not None and not g.is_empty else None})
    return {"type": "FeatureCollection", "features": feats}


def simplified(gdf: gpd.GeoDataFrame, metric: str, tol: float) -> gpd.GeoDataFrame:
    g = gdf.to_crs(metric)
    g["geometry"] = g.geometry.simplify(tol, preserve_topology=True)
    return g.to_crs("EPSG:4326")


def main() -> None:
    args = cli("Étape 10 : export des données du site web.").parse_args()
    cfg = load_config(args.config)
    web = cfg.get("web") or {}
    dec = int(web.get("coord_decimals", DEFAULT_DECIMALS))
    tol = float(web.get("simplify_m", DEFAULT_SIMPLIFY_M))
    metric, slug = cfg["region"]["metric_crs"], cfg["origin"]["slug"]
    pdir, odir = processed_dir(cfg), outputs_dir(cfg)

    syn = json.loads((odir / f"synthese_{slug}.json").read_text(encoding="utf-8"))
    res = pd.read_csv(odir / f"resultats_{slug}.csv", dtype={"unit_id": str, "parent_id": str})
    carto = pdir / "cartogram.gpkg"
    units_geo = gpd.read_file(carto, layer="units_geo")
    units_time = gpd.read_file(carto, layer="units_time")
    pts_time = gpd.read_file(carto, layer="points_time")
    origins = gpd.read_file(pdir / "origins.geojson")
    origins["id"] = origins["id"].astype(str)

    info = res.drop_duplicates("unit_id").set_index("unit_id")[["name", "level", "population"]]
    units_geo = units_geo.join(info, on="unit_id")
    order = units_geo["unit_id"].tolist()

    results = {}
    for oid, g in res.groupby("origin_id"):
        tp = pts_time[pts_time["origin_id"] == oid].set_index("unit_id")
        g = g.join(tp.geometry.rename("tgeom"), on="unit_id")
        rows = []
        for _, r in g.iterrows():
            d = {c: clean(r[c]) for c in RESULT_COLS if c in g.columns}
            tg = r["tgeom"]
            d["tlon"], d["tlat"] = (round(tg.x, dec), round(tg.y, dec)) if tg is not None and not pd.isna(tg) else (None, None)
            rows.append(d)
        results[oid] = rows

    ut = {}
    for oid, g in units_time.groupby("origin_id"):
        g = g.set_index("unit_id").loc[order].reset_index()        # même ordre que units
        ut[oid] = fc(g, ["unit_id"], dec)

    iso_path = pdir / "isochrones.gpkg"
    iso = simplified(gpd.read_file(iso_path, layer="isochrones"), metric, tol)
    rings = simplified(gpd.read_file(iso_path, layer="rings"), metric, tol)

    itin = {"summary": [], "segments": {"type": "FeatureCollection", "features": []}}
    if (pdir / "itineraries.parquet").exists():
        s = pd.read_parquet(pdir / "itineraries.parquet")
        itin["summary"] = [{k: clean(v) for k, v in r.items()} for r in s.to_dict("records")]
        seg = gpd.read_file(pdir / "itineraries.gpkg")
        seg["departure_time"] = seg["departure_time"].astype(str)
        itin["segments"] = fc(simplified(seg, metric, tol),
                              ["from_id", "to_id", "segment", "mode", "route", "departure_time", "travel_min",
                               "wait_min", "distance"], dec)

    src_ids = [i for v in cfg["inputs"].values() for i in (v if isinstance(v, list) else [v])]
    sources = [{"id": i, "attribution": cfg["sources"][i].get("attribution"), "licence": cfg["sources"][i].get("licence")}
               for i in dict.fromkeys(src_ids) if i in cfg["sources"]]

    pts = cfg["origin"]["points"]
    meta = {
        "slug": slug, "name": cfg["origin"]["name"], "country": cfg["region"]["country"],
        "region": {"id": cfg["region"]["id"], "de": cfg["region"].get("label_de"), "fr": cfg["region"].get("label_fr")},
        "core_city": cfg.get("core_city", {}).get("name"),
        "date": str(cfg["analysis"]["date"]), "window": [cfg["analysis"]["window_start"], cfg["analysis"]["window_end"]],
        "radius_km": cfg["analysis"]["radius_km"], "thresholds": cfg["thresholds"],
        "walk_speed_kmh": cfg["routing"]["walk_speed_kmh"],
        "cartogram_speed_kmh": cfg["visuals"]["cartogram_speed_kmh"],
        "isochrones_min": cfg["visuals"]["isochrones_min"], "rings_km": cfg["visuals"]["distance_rings_km"],
        "origins": [{"id": p["id"], "de": p.get("label_de"), "fr": p.get("label_fr"),
                     "lon": round(origins.loc[origins["id"] == p["id"]].geometry.iloc[0].x, dec),
                     "lat": round(origins.loc[origins["id"] == p["id"]].geometry.iloc[0].y, dec)} for p in pts],
        "summary": {s["origin_id"]: s for s in syn["origins"]},
        "sources": sources,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data = {"meta": meta, "units": fc(units_geo, ["unit_id", "name", "level", "population"], dec),
            "units_time": ut, "results": results,
            "isochrones": fc(iso, ["origin_id", "minutes", "area_km2"], dec),
            "rings": fc(rings, ["origin_id", "km"], dec), "itineraries": itin}

    WEB_DATA.mkdir(parents=True, exist_ok=True)
    out = WEB_DATA / f"{slug}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    LOG.info("Écrit : %s (%.2f Mo) — %d unités, %d origines, %d isochrones, %d segments d'itinéraires", out,
             out.stat().st_size / 1e6, len(units_geo), len(results), len(iso), len(itin["segments"]["features"]))

    idx_path = WEB_DATA / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {"origins": []}
    idx["origins"] = [o for o in idx["origins"] if o["slug"] != slug] + [
        {"slug": slug, "name": meta["name"], "country": meta["country"], "region": meta["region"],
         "file": f"{slug}.json"}]
    idx["origins"].sort(key=lambda o: (o["country"] != "DE", o["slug"]))
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
