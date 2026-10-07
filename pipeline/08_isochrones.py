"""
Étape 08 — Isochrones en transports en commun et cercles de distance.

    uv run python pipeline/08_isochrones.py --config config/garches.yaml

Pour chaque point d'origine (gare, centre) :
  - isochrones (visuals.isochrones_min) : médiane (p50) du temps porte à porte sur la
    fenêtre de départ, mêmes paramètres de routage que 04, vers les centres d'une grille
    de cellules de visuals.isochrone_grid_m m couvrant la zone de découpe ; isochrone =
    union des cellules atteintes (r5py.Isochrones ne renvoie que des contours simplifiés) ;
  - cercles à vol d'oiseau (visuals.distance_rings_km), tracés dans le CRS métrique
    de la région.
Le point d'origine passe par le même contrôle « îlots piétons » que 04.

Indicateurs (qa_isochrones.json) : surface de chaque isochrone, rayon équivalent
(rayon du disque de même surface), part du disque de 5/10/15 km couverte par
chaque isochrone, population des unités du rayon dont le point est dans l'isochrone.

Sorties :
  processed_dir/isochrones.gpkg   couches « isochrones » et « rings » (EPSG:4326)
  outputs_dir/qa_isochrones.json
  outputs_dir/assumptions.jsonl   V1_isochrones
"""
from __future__ import annotations

import importlib
import json
import time
import warnings
from datetime import timedelta

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from common import cli, get_logger, interim_dir, load_config, log_assumption, outputs_dir, processed_dir

t04 = importlib.import_module("04_transit_times")

LOG = get_logger("08_isochrones")


def grid(cfg: dict) -> gpd.GeoDataFrame:
    """Centres des cellules carrées de visuals.isochrone_grid_m m couvrant la zone de découpe (02)."""
    metric, step = cfg["region"]["metric_crs"], float(cfg["visuals"]["isochrone_grid_m"])
    zone = gpd.read_file(interim_dir(cfg) / "clip_area.geojson").to_crs(metric).union_all()
    x0, y0, x1, y1 = zone.bounds
    xs, ys = np.meshgrid(np.arange(x0 + step / 2, x1, step), np.arange(y0 + step / 2, y1, step))
    pts = gpd.GeoDataFrame(geometry=gpd.points_from_xy(xs.ravel(), ys.ravel()), crs=metric)
    pts = pts[pts.within(zone)].reset_index(drop=True)
    pts["id"] = pts.index.astype(str)
    return pts


def isochrones_for(r5py, network, cfg: dict, origin: gpd.GeoDataFrame, cells: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Une ligne par seuil : union des cellules dont le centre est atteint (p50) dans le seuil."""
    v, metric = cfg["visuals"], cfg["region"]["metric_crs"]
    step = float(v["isochrone_grid_m"])
    _, window = t04.departure_and_window(cfg)
    kwargs = t04.r5_common_kwargs(cfg, r5py)
    kwargs["max_time"] = timedelta(minutes=max(int(m) for m in v["isochrones_min"]))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # Centres de cellule NON accrochés : R5 les relie lui-même à la voirie (marche hors voie comptée)
        tt = t04.plain(r5py.TravelTimeMatrix(network, origins=origin[["id", "geometry"]],
                                             destinations=cells[["id", "geometry"]].to_crs("EPSG:4326"),
                                             snap_to_network=False, departure_time_window=window,
                                             percentiles=[50], **kwargs))
    col = "travel_time_p50" if "travel_time_p50" in tt.columns else "travel_time"
    t = cells[["id", "geometry"]].merge(tt[["to_id", col]], left_on="id", right_on="to_id", how="left")[col]
    rows = []
    for m in sorted(int(x) for x in v["isochrones_min"]):
        sel = cells.geometry[(t <= m).to_numpy()]
        boxes = shapely.box(sel.x - step / 2, sel.y - step / 2, sel.x + step / 2, sel.y + step / 2)
        geom = shapely.coverage_union_all(boxes) if len(boxes) else shapely.Polygon()
        rows.append({"minutes": m, "n_cells": int(len(sel)), "geometry": geom})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=metric).to_crs("EPSG:4326")


def main() -> None:
    args = cli("Étape 08 : isochrones TC et cercles de distance.").parse_args()
    cfg = load_config(args.config)
    v, metric = cfg["visuals"], cfg["region"]["metric_crs"]
    t_start = time.time()
    origins, dest = t04.load_od(cfg)
    r5py, jvm = t04.start_r5(cfg)
    network = t04.build_network(r5py, cfg)
    if t04.snap_option(cfg):
        origins, _ = t04.fix_islands(r5py, network, cfg, origins, "origine")

    pop = (gpd.read_parquet(processed_dir(cfg) / "destinations.parquet")
           .query("in_radius")[["unit_id", "population", "is_origin_commune", "geometry"]].to_crs(metric))

    tt04 = pd.read_parquet(processed_dir(cfg) / "tt_transit.parquet")
    tt04 = tt04[tt04["to_id"].isin(pop.loc[~pop["is_origin_commune"], "unit_id"])]
    cells = grid(cfg)
    LOG.info("Grille : %d cellules de %s m sur la zone de découpe", len(cells), v["isochrone_grid_m"])
    isos, rings, qa = [], [], {"origin": cfg["origin"]["slug"], "grid_m": int(v["isochrone_grid_m"]), "n_cells": int(len(cells)),
                               "jvm": jvm, "by_origin": {}}
    for _, o in origins.iterrows():
        oid = o["id"]
        t0 = time.time()
        iso = isochrones_for(r5py, network, cfg, origins[origins["id"] == oid], cells).assign(origin_id=oid)
        LOG.info("Isochrones %s : %s min en %.1f s", oid, iso["minutes"].tolist(), time.time() - t0)
        centre = gpd.GeoSeries([o.geometry], crs="EPSG:4326").to_crs(metric).iloc[0]
        rg = gpd.GeoDataFrame({"origin_id": oid, "km": [float(k) for k in v["distance_rings_km"]]},
                              geometry=[centre.buffer(float(k) * 1000, 256) for k in v["distance_rings_km"]],
                              crs=metric)
        iso_m = iso.to_crs(metric)
        iso["area_km2"] = iso_m.area.div(1e6).round(2).to_numpy()
        rg["area_km2"] = rg.area.div(1e6).round(2)
        rec = {}
        for (_, row), geom in zip(iso.iterrows(), iso_m.geometry):
            inside = pop[pop.within(geom) & ~pop["is_origin_commune"]]
            rec[str(row["minutes"])] = {
                "area_km2": float(row["area_km2"]),
                "equivalent_radius_km": round(float(np.sqrt(row["area_km2"] / np.pi)), 2),
                "share_of_disc": {f"{k:g}km": round(float(geom.intersection(d).area / d.area), 3)
                                  for k, d in zip(rg["km"], rg.geometry)},
                "n_units": int(len(inside)), "population": int(inside["population"].sum()),
                # Contrôle : mêmes unités selon les temps p50 de 04 (points accrochés, temps ≤ seuil)
                "check_04": {"n_units": int(len(ok04 := tt04[(tt04["from_id"] == oid) & (tt04["travel_time_p50"] <= row["minutes"])])),
                             "population": int(pop.set_index("unit_id").loc[ok04["to_id"], "population"].sum())},
            }
        qa["by_origin"][oid] = {"isochrones": rec,
                                "rings_area_km2": {f"{k:g}km": float(a) for k, a in zip(rg["km"], rg["area_km2"])}}
        isos.append(iso)
        rings.append(rg.to_crs("EPSG:4326"))
        LOG.info("  min : (km², rayon équivalent km, population isochrone, population selon 04) %s",
                 {m: (r["area_km2"], r["equivalent_radius_km"], r["population"], r["check_04"]["population"])
                  for m, r in rec.items()})

    out = processed_dir(cfg) / "isochrones.gpkg"
    out.unlink(missing_ok=True)
    pd.concat(isos, ignore_index=True).pipe(gpd.GeoDataFrame, crs="EPSG:4326").to_file(out, layer="isochrones", driver="GPKG")
    pd.concat(rings, ignore_index=True).pipe(gpd.GeoDataFrame, crs="EPSG:4326").to_file(out, layer="rings", driver="GPKG")
    qa["timings_s"] = {"total": round(time.time() - t_start, 1)}
    (outputs_dir(cfg) / "qa_isochrones.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Écrit : %s, qa_isochrones.json", out)

    a = cfg["analysis"]
    log_assumption(
        cfg, "08", "V1_isochrones",
        (f"Isochrones {v['isochrones_min']} min : médiane du temps TC sur la fenêtre "
         f"{a['window_start']}–{a['window_end']} (mêmes paramètres que 04) vers les centres d'une grille de cellules "
         f"de {v['isochrone_grid_m']} m sur la zone de découpe ; isochrone = union des cellules dont le centre est "
         f"atteint. Les centres ne sont pas accrochés de force : R5 les relie à la voirie la plus proche et compte la "
         f"marche hors voirie ; cellules trop loin de toute voie = non atteintes. Cercles {v['distance_rings_km']} km à vol d'oiseau autour du point d'origine. "
         f"Population couverte = unités du rayon dont le point officiel est dans l'isochrone (hors commune d'origine)."),
        (f"Isochronen {v['isochrones_min']} Min.: Median der ÖV-Reisezeit im Fenster "
         f"{a['window_start']}–{a['window_end']} (gleiche Parameter wie in 04) zu den Mittelpunkten eines Rasters mit "
         f"{v['isochrone_grid_m']}-m-Zellen im Zuschnittsgebiet; Isochrone = Vereinigung der Zellen, deren Mittelpunkt "
         f"erreicht wird. Die Mittelpunkte werden nicht eingerastet: R5 verbindet sie mit der nächsten Straße und zählt "
         f"den Fußweg abseits der Straße; Zellen zu weit von jeder Straße = nicht erreicht. "
         f"Kreise {v['distance_rings_km']} km Luftlinie um den Ursprungspunkt. Erreichte Bevölkerung = Einheiten im "
         f"Radius, deren amtlicher Punkt in der Isochrone liegt (ohne Ursprungsgemeinde)."),
        {"isochrones_min": v["isochrones_min"], "grid_m": v["isochrone_grid_m"], "rings_km": v["distance_rings_km"]})
    LOG.info("Terminé en %.0f s.", time.time() - t_start)


if __name__ == "__main__":
    main()
