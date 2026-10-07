"""
Étape 09 — Cartogramme temporel : angle conservé, rayon = temps de trajet.

    uv run python pipeline/09_cartogram.py --config config/garches.yaml

Pour chaque point d'origine, chaque sommet des contours des unités (communes,
arrondissements, Stadtteile) est déplacé le long de sa direction depuis l'origine,
à la distance  temps TC (p50) × visuals.cartogram_speed_kmh.  À 15 km/h, une commune
à 30 min est dessinée à 7,5 km : plus près qu'en réalité si elle est bien desservie,
plus loin sinon. La même vitesse est utilisée pour toutes les origines, donc les
cartogrammes de Garches et de Kronberg sont à la même échelle.

Temps d'un sommet : interpolation linéaire (triangulation de Delaunay) des temps p50
de l'étape 04 aux points des unités du rayon, le point d'origine valant 0 min ; hors de
l'enveloppe convexe des points, temps du point le plus proche. Les unités non atteintes
dans routing.max_time_min ne sont pas des points de contrôle (leur contour prend le temps
interpolé des voisines) ; leur point est placé à visuals.cartogram_max_time_min (reached=False). Un premier
essai avec le champ de temps de la grille de 08 donnait des contours très bruités
(le temps porte à porte varie fortement d'une cellule à l'autre).
Les contours sont simplifiés en conservant les frontières communes
(shapely.coverage_simplify, visuals.cartogram_simplify_m) puis densifiés
(visuals.cartogram_segment_m) pour que la déformation reste lisse.

Repliements : quand une unité lointaine est plus rapide à atteindre que sa voisine
proche, les contours se replient (polygones invalides, comptés dans le QA). C'est le
paradoxe étudié lui-même ; l'imposer monotone le long de chaque rayon supprimerait les
replis mais aussi le message. Les points (temps exacts) portent l'information, les
contours servent de fond.

Les géométries « géo » et « temps » ont exactement les mêmes sommets dans le même
ordre : le site web peut animer la transition sommet par sommet.

Sorties :
  processed_dir/cartogram.gpkg   couches units_geo (unit_id), units_time (origin_id,
                                 unit_id), points_time (origin_id, unit_id, t_min,
                                 reached) — EPSG:4326
  outputs_dir/qa_cartogram.json
  outputs_dir/assumptions.jsonl  V2_cartogram
"""
from __future__ import annotations

import importlib
import json
import time

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from scipy import interpolate

from common import cli, get_logger, load_config, log_assumption, outputs_dir, processed_dir

t04 = importlib.import_module("04_transit_times")

LOG = get_logger("09_cartogram")


def control_points(o_xy, d: gpd.GeoDataFrame, t: pd.Series):
    """Points de contrôle (x, y) et temps : unités atteintes + origine à 0 min."""
    ok = t.notna().to_numpy()
    xy = np.column_stack([d.geometry.x, d.geometry.y])[ok]
    tt = t.to_numpy(float)[ok]
    return np.vstack([np.asarray(o_xy)[None, :], xy]), np.concatenate([[0.0], tt])


def warp(geoms: gpd.GeoSeries, o_xy, xy: np.ndarray, tt: np.ndarray, speed_m_per_min: float) -> gpd.GeoSeries:
    """Déplace chaque sommet : même angle depuis l'origine, rayon = temps interpolé × vitesse."""
    lin = interpolate.LinearNDInterpolator(xy, tt)
    near = interpolate.NearestNDInterpolator(xy, tt)
    ox, oy = o_xy

    def f(coords: np.ndarray) -> np.ndarray:
        t = lin(coords)
        t = np.where(np.isnan(t), near(coords), t)
        dx, dy = coords[:, 0] - ox, coords[:, 1] - oy
        r = np.hypot(dx, dy)
        with np.errstate(invalid="ignore", divide="ignore"):
            k = np.where(r > 0, t * speed_m_per_min / r, 0.0)
        return np.column_stack([ox + dx * k, oy + dy * k])

    return gpd.GeoSeries(shapely.transform(geoms.to_numpy(), f), crs=geoms.crs)


def main() -> None:
    args = cli("Étape 09 : cartogramme temporel.").parse_args()
    cfg = load_config(args.config)
    v, metric = cfg["visuals"], cfg["region"]["metric_crs"]
    speed = float(v["cartogram_speed_kmh"]) * 1000 / 60          # m/min
    t_start = time.time()
    pdir = processed_dir(cfg)

    origins, dest = t04.load_od(cfg)
    in_radius = set(dest["id"])
    units = gpd.read_file(pdir / "units.gpkg", layer="units_polygons")
    units = units[units["unit_id"].astype(str).isin(in_radius)].to_crs(metric).reset_index(drop=True)
    n_vert_before = int(shapely.get_num_coordinates(units.geometry.to_numpy()).sum())
    geo = gpd.GeoSeries(shapely.coverage_simplify(units.geometry.to_numpy(), float(v["cartogram_simplify_m"])),
                        crs=metric)
    geo = geo.segmentize(float(v["cartogram_segment_m"]))
    n_vert = int(shapely.get_num_coordinates(geo.to_numpy()).sum())
    LOG.info("%d unités ; sommets %d → %d (simplification %s m, densification %s m)", len(units), n_vert_before,
             n_vert, v["cartogram_simplify_m"], v["cartogram_segment_m"])

    tt04 = pd.read_parquet(pdir / "tt_transit.parquet")
    t_max = float(v["cartogram_max_time_min"])
    dest_m = dest.to_crs(metric)

    time_layers, point_layers = [], []
    qa = {"origin": cfg["origin"]["slug"], "speed_kmh": float(v["cartogram_speed_kmh"]),
          "n_units": int(len(units)), "n_vertices": n_vert, "by_origin": {}}
    for _, o in origins.iterrows():
        oid = o["id"]
        o_m = gpd.GeoSeries([o.geometry], crs="EPSG:4326").to_crs(metric).iloc[0]
        d = dest_m.merge(tt04[tt04["from_id"] == oid][["to_id", "travel_time_p50"]],
                         left_on="id", right_on="to_id", how="left")
        xy, tt = control_points((o_m.x, o_m.y), d, d["travel_time_p50"])
        tg = warp(geo, (o_m.x, o_m.y), xy, tt, speed)

        # Points des destinations : même règle (rayon = temps p50 × vitesse)
        reached = d["travel_time_p50"].notna()
        dx, dy = d.geometry.x - o_m.x, d.geometry.y - o_m.y
        r = np.hypot(dx, dy).replace(0, np.nan)
        k = (d["travel_time_p50"].fillna(t_max) * speed / r).fillna(0)
        pts = gpd.GeoDataFrame({"origin_id": oid, "unit_id": d["id"], "t_min": d["travel_time_p50"],
                                "reached": reached},
                               geometry=gpd.points_from_xy(o_m.x + dx * k, o_m.y + dy * k), crs=metric)
        time_layers.append(gpd.GeoDataFrame({"origin_id": oid, "unit_id": units["unit_id"].astype(str)},
                                            geometry=tg.to_numpy(), crs=metric))
        point_layers.append(pts)
        qa["by_origin"][oid] = {
            "n_control_points": int(len(xy)), "units_unreached": int((~reached).sum()),
            "invalid_warped_polygons": int((~tg.is_valid).sum()),
            "scale_factor_median": round(float(np.nanmedian((d["travel_time_p50"] * speed / r)[reached])), 3),
        }
        LOG.info("Cartogramme %s : %s", oid, qa["by_origin"][oid])

    out = pdir / "cartogram.gpkg"
    out.unlink(missing_ok=True)
    gpd.GeoDataFrame({"unit_id": units["unit_id"].astype(str)}, geometry=geo.to_numpy(), crs=metric) \
        .to_crs("EPSG:4326").to_file(out, layer="units_geo", driver="GPKG")
    pd.concat(time_layers, ignore_index=True).pipe(gpd.GeoDataFrame, crs=metric).to_crs("EPSG:4326") \
        .to_file(out, layer="units_time", driver="GPKG")
    pd.concat(point_layers, ignore_index=True).pipe(gpd.GeoDataFrame, crs=metric).to_crs("EPSG:4326") \
        .to_file(out, layer="points_time", driver="GPKG")
    qa["timings_s"] = {"total": round(time.time() - t_start, 1)}
    (outputs_dir(cfg) / "qa_cartogram.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Écrit : %s, qa_cartogram.json", out)

    log_assumption(
        cfg, "09", "V2_cartogram",
        (f"Cartogramme temporel : chaque sommet des contours garde sa direction depuis le point d'origine et est placé "
         f"à temps TC médian × {v['cartogram_speed_kmh']} km/h (même échelle pour toutes les origines ; valeur dans la "
         f"plage des vitesses effectives médianes mesurées). Temps d'un sommet = interpolation linéaire (Delaunay) des "
         f"temps p50 de l'étape 04 aux points des unités atteintes, origine = 0 min ; le point d'une unité non atteinte "
         f"est placé à {v['cartogram_max_time_min']} min et signalé. Contours simplifiés à {v['cartogram_simplify_m']} m en conservant les "
         f"frontières communes, densifiés à {v['cartogram_segment_m']} m. Image de lecture, pas une mesure : seuls les "
         f"points des communes portent un temps calculé ; les contours se replient là où une unité lointaine est plus "
         f"vite atteinte qu'une proche."),
        (f"Zeitkartogramm: Jeder Stützpunkt der Grenzen behält seine Richtung vom Ursprung und wird im Abstand "
         f"ÖV-Medianzeit × {v['cartogram_speed_kmh']} km/h gezeichnet (gleicher Maßstab für alle Ursprünge; Wert im "
         f"Bereich der gemessenen medianen Effektivgeschwindigkeiten). Zeit eines Stützpunkts = lineare Interpolation "
         f"(Delaunay) der P50-Zeiten aus Schritt 04 an den Punkten der erreichten Einheiten, Ursprung = 0 Min.; der Punkt "
         f"einer nicht erreichbaren Einheit liegt bei {v['cartogram_max_time_min']} Min. und ist markiert. Grenzen auf {v['cartogram_simplify_m']} m vereinfacht "
         f"(gemeinsame Grenzen bleiben gemeinsam), auf {v['cartogram_segment_m']} m verdichtet. Lesebild, keine Messung: "
         f"nur die Gemeindepunkte tragen eine berechnete Zeit; die Grenzen falten sich dort, wo eine ferne Einheit "
         f"schneller erreicht wird als eine nahe."),
        {k: v[k] for k in ("cartogram_speed_kmh", "cartogram_max_time_min", "cartogram_simplify_m",
                           "cartogram_segment_m")})
    LOG.info("Terminé en %.0f s.", time.time() - t_start)


if __name__ == "__main__":
    main()
