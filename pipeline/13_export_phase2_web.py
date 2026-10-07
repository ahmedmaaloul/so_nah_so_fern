"""
Étape 13 — Export de la phase 2 (toutes les unités entre elles) pour le site web.

    uv run python pipeline/13_export_phase2_web.py --config config/garches.yaml    # Île-de-France
    uv run python pipeline/13_export_phase2_web.py --config config/kronberg.yaml   # Rhin-Main

Lit les sorties de 12 (data/processed/phase2/<région>/all_pairs.parquet,
outputs/phase2/<région>/*) et les contours de 01 ; écrit web/data/phase2_<région>.json :

  meta        région, ville-centre, rayon, date, fenêtre, seuils, synthèse de 12, sources
  units       FeatureCollection des contours simplifiés (coverage_simplify, frontières
              communes conservées) ; propriétés : i (indice dans la matrice), unit_id,
              name, level, population, lon, lat (point officiel) et les indicateurs
              par unité de 12 (t_p50_median, v_eff_popweighted_kmh, …)
  matrix      ids (ordre des lignes/colonnes) et trois tableaux N×N aplatis, ligne =
              origine, colonne = destination : t_p50 (min, entier), car (min, 1 déc.),
              dist (km, 2 déc.), car_peak (voiture en pointe, min, si 12 l'a calculée) ;
              -1 = pas de valeur (non atteint, diagonale)
  pairs_top   paires remarquables de 12
  by_band     synthèse par classe de distance (12)

Met à jour web/data/index.json (clé « phase2 »).
"""
from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from common import ROOT, cli, get_logger, load_config, processed_dir

t10 = __import__("importlib").import_module("10_export_web")

LOG = get_logger("13_export_phase2_web")
WEB_DATA = ROOT / "web" / "data"
DEFAULT_SIMPLIFY_M = 100        # web.phase2_simplify_m


def main() -> None:
    args = cli("Étape 13 : export web de la phase 2.").parse_args()
    cfg = load_config(args.config)
    web = cfg.get("web") or {}
    dec = int(web.get("coord_decimals", t10.DEFAULT_DECIMALS))
    tol = float(web.get("phase2_simplify_m", DEFAULT_SIMPLIFY_M))
    rid, metric = cfg["region"]["id"], cfg["region"]["metric_crs"]
    pdir2 = ROOT / "data" / "processed" / "phase2" / rid
    odir2 = ROOT / "outputs" / "phase2" / rid

    pairs = pd.read_parquet(pdir2 / "all_pairs.parquet")
    per_unit = pd.read_csv(odir2 / "units_phase2.csv", dtype={"unit_id": str})
    syn = json.loads((odir2 / "synthese_phase2.json").read_text(encoding="utf-8"))
    top = pd.read_csv(odir2 / "pairs_top.csv", dtype={"from_id": str, "to_id": str})

    ids = sorted(set(pairs["from_id"]) | set(pairs["to_id"]))
    idx = {u: i for i, u in enumerate(ids)}
    n = len(ids)

    units = gpd.read_file(processed_dir(cfg) / "units.gpkg", layer="units_polygons")
    units["unit_id"] = units["unit_id"].astype(str)
    units = units[units["unit_id"].isin(idx)].copy()
    pts = gpd.read_parquet(processed_dir(cfg) / "destinations.parquet")[["unit_id", "geometry"]]
    pts["unit_id"] = pts["unit_id"].astype(str)
    pts = pts.set_index("unit_id").geometry
    if len(units) != n:
        raise SystemExit(f"{n} unités dans la matrice mais {len(units)} contours trouvés.")
    units["i"] = units["unit_id"].map(idx)
    units = units.sort_values("i").reset_index(drop=True)
    g = units.to_crs(metric)
    g["geometry"] = shapely.coverage_simplify(g.geometry.to_numpy(), tol)
    units = g.to_crs("EPSG:4326")
    units["lon"] = [round(pts[u].x, dec) for u in units["unit_id"]]
    units["lat"] = [round(pts[u].y, dec) for u in units["unit_id"]]
    pu_cols = [c for c in per_unit.columns if c not in ("name", "population")]
    units = units.merge(per_unit[pu_cols], on="unit_id", how="left")

    def flat(col: str, nd: int) -> list:
        m = np.full((n, n), -1.0)
        v = pairs[col].to_numpy(float)
        ok = ~np.isnan(v)
        m[pairs["from_id"].map(idx).to_numpy()[ok], pairs["to_id"].map(idx).to_numpy()[ok]] = np.round(v[ok], nd)
        out = m.ravel().tolist()
        return [int(x) for x in out] if nd == 0 else out

    props = ["i", "unit_id", "name", "level", "population", "lon", "lat"] + [c for c in pu_cols if c != "unit_id"]
    a = cfg["analysis"]
    src_ids = [i for v in cfg["inputs"].values() for i in (v if isinstance(v, list) else [v])]
    data = {
        "meta": {"region": {"id": rid, "de": cfg["region"].get("label_de"), "fr": cfg["region"].get("label_fr")},
                 "country": cfg["region"]["country"], "core_city": cfg["core_city"]["name"],
                 "radius_km": a["radius_km"], "date": str(a["date"]), "window": [a["window_start"], a["window_end"]],
                 "thresholds": cfg["thresholds"], "n_units": n,
                 "summary": {k: v for k, v in syn.items() if k not in ("by_distance_band", "jvm", "timings_s")},
                 "sources": [{"id": i, "attribution": cfg["sources"][i].get("attribution"),
                              "licence": cfg["sources"][i].get("licence")} for i in dict.fromkeys(src_ids)
                             if i in cfg["sources"]]},
        "units": t10.fc(units, props, dec),
        "matrix": {"ids": ids, "t_p50": flat("t_p50", 0), "car": flat("car_time", 1), "dist": flat("dist_km", 2),
                   **({"car_peak": flat("car_time_peak", 1)} if "car_time_peak" in pairs else {})},
        "pairs_top": [{k: t10.clean(v) for k, v in r.items()} for r in top.to_dict("records")],
        "by_band": syn["by_distance_band"],
    }
    WEB_DATA.mkdir(parents=True, exist_ok=True)
    out = WEB_DATA / f"phase2_{rid}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    LOG.info("Écrit : %s (%.2f Mo) — %d unités, %d paires", out, out.stat().st_size / 1e6, n, len(pairs))

    idx_path = WEB_DATA / "index.json"
    index = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {"origins": []}
    index["phase2"] = [r for r in index.get("phase2", []) if r["region"]["id"] != rid] + [
        {"region": data["meta"]["region"], "country": data["meta"]["country"], "core_city": data["meta"]["core_city"],
         "file": out.name}]
    index["phase2"].sort(key=lambda r: (r["country"] != "DE", r["region"]["id"]))
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
