"""
Étape 12 — Phase 2 : matrice de toutes les unités entre elles (rayon autour de la ville-centre).

    uv run python pipeline/12_all_pairs.py --config config/garches.yaml     # Île-de-France
    uv run python pipeline/12_all_pairs.py --config config/kronberg.yaml    # Rhin-Main

Réutilise le réseau (extraits OSM/GTFS de 02) et les unités (01) de l'origine donnée :
la zone de découpe de 02 couvre déjà les unités in_core_radius (≤ analysis.radius_km de
la ville-centre) plus clip_buffer_km. Origines = destinations = ces unités (points
officiels, contrôle « îlots piétons » de 04). Mêmes paramètres de routage que 04.

Calculs :
  - TC : médiane (et p25/p75) sur la fenêtre, toutes paires (N × N) ;
  - voiture : OSRM (graphe de 06), sans trafic ;
  - distance géodésique entre points.
Indicateurs par unité d'origine (comme 07, mais depuis chaque unité) : temps médian
vers les autres unités, vitesse effective médiane, corrélation de Spearman distance/
temps, nombre de « zones mortes » ; et paires remarquables (proches mais lentes,
lointaines mais rapides) sur l'ensemble de la région.

Sorties (data/processed/phase2/<region>/, outputs/phase2/<region>/) :
  all_pairs.parquet        from_id, to_id, dist_km, t_p25, t_p50, t_p75, car_time
  units_phase2.csv         une ligne par unité d'origine
  pairs_top.csv            paires remarquables
  synthese_phase2.json     chiffres de région (distribution par classe de distance)
  assumptions.jsonl        P1_all_pairs
"""
from __future__ import annotations

import importlib
import json
import time
import warnings

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Geod

from common import ROOT, cli, get_logger, load_config, processed_dir

t04 = importlib.import_module("04_transit_times")
t06 = importlib.import_module("06_car_times")

LOG = get_logger("12_all_pairs")
DIST_BANDS_KM = [0, 5, 10, 15, 20, 30, 60]        # classes d'affichage de la synthèse (contrôle)


def phase2_dirs(cfg: dict):
    rid = cfg["region"]["id"]
    p = ROOT / "data" / "processed" / "phase2" / rid
    o = ROOT / "outputs" / "phase2" / rid
    p.mkdir(parents=True, exist_ok=True)
    o.mkdir(parents=True, exist_ok=True)
    return p, o


def log_p1(odir, text_fr: str, text_de: str, value) -> None:
    path = odir / "assumptions.jsonl"
    from datetime import datetime, timezone
    path.write_text(json.dumps({"code": "P1_all_pairs", "step": "12", "fr": text_fr, "de": text_de, "value": value,
                                "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                               ensure_ascii=False) + "\n", encoding="utf-8")


def car_matrix(cfg: dict, pts: gpd.GeoDataFrame) -> tuple[pd.DataFrame, dict | None]:
    """Voiture sans trafic (car_time) et, si car.peak est défini, en pointe (car_time_peak, C3 de 06)."""
    car = cfg.get("car") or {}
    image = car.get("osrm_image", t06.DEFAULT_IMAGE)
    port = int(car.get("osrm_port", t06.DEFAULT_PORT))
    peak = t06.peak_params(cfg)
    t06.build_graph(cfg, image, rebuild=False)
    name = t06.start_server(cfg, image, port, 2 * len(pts))
    try:
        od, _ = t06.table(port, pts, pts)
        od = od[od["from_id"] != od["to_id"]]
        cols = ["from_id", "to_id", "car_time"]
        od = od.assign(car_time=od["dur_s"] / 60)
        if peak:
            t0 = time.time()
            xy = {i: (g.x, g.y) for i, g in zip(pts["id"], pts.geometry)}
            split = t06.motorway_split_pairs(port, xy, list(zip(od["from_id"], od["to_id"])))
            od = t06.apply_peak(od.merge(split, on=["from_id", "to_id"], how="left"), peak)
            cols += ["car_time_peak", "motorway_share"]
            LOG.info("Pointe : %d itinéraires /route en %.0f s ; part autoroute médiane %.1f %%", len(split),
                     time.time() - t0, 100 * od["motorway_share"].median())
    finally:
        t06.docker("rm", "-f", name, check=False)
    return od[cols], peak


def transit_matrix(cfg: dict, pts: gpd.GeoDataFrame):
    """Matrice TC N × N (médiane et percentiles) ; renvoie (table, infos JVM, durée en s)."""
    r5py, jvm = t04.start_r5(cfg)
    network = t04.build_network(r5py, cfg)
    snap = t04.snap_option(cfg)
    pts_r5 = pts
    if snap:
        pts_r5, isl = t04.fix_islands(r5py, network, cfg, pts, "unité")
        LOG.info("Îlots : %d unité(s) déplacée(s)", sum(r.get("moved", False) for r in isl["isolated"]))
    _, window = t04.departure_and_window(cfg)
    pcts = [int(p) for p in cfg["analysis"]["percentiles"]]
    t0 = time.time()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tt = t04.plain(r5py.TravelTimeMatrix(network, origins=pts_r5[["id", "geometry"]],
                                             destinations=pts_r5[["id", "geometry"]], snap_to_network=snap,
                                             departure_time_window=window, percentiles=pcts,
                                             **t04.r5_common_kwargs(cfg, r5py)))
    t_tt = time.time() - t0
    tt = tt.rename(columns={f"travel_time_p{p}": f"t_p{p}" for p in pcts})
    LOG.info("Matrice TC : %d paires en %.0f s", len(tt), t_tt)

    return tt, jvm, t_tt


def main() -> None:
    parser = cli("Étape 12 : phase 2, toutes les unités entre elles.")
    parser.add_argument("--car-only", action="store_true",
                        help="garde la matrice TC de all_pairs.parquet, refait seulement la voiture et les synthèses")
    args = parser.parse_args()
    cfg = load_config(args.config)
    th = cfg["thresholds"]
    pdir, odir = phase2_dirs(cfg)
    t_start = time.time()

    units = gpd.read_parquet(processed_dir(cfg) / "destinations.parquet")
    units = units[units["in_core_radius"].astype(bool)].rename(columns={"unit_id": "id"})
    units["id"] = units["id"].astype(str)
    pts = units[["id", "name", "population", "geometry"]].to_crs("EPSG:4326").reset_index(drop=True)
    LOG.info("Région %s : %d unités à ≤ %s km de %s", cfg["region"]["id"], len(pts), cfg["analysis"]["radius_km"],
             cfg["core_city"]["name"])

    if args.car_only:
        prev = pd.read_parquet(pdir / "all_pairs.parquet")
        tt = prev[[c for c in prev.columns if c in ("from_id", "to_id") or c.startswith("t_p")]]
        old_syn = json.loads((odir / "synthese_phase2.json").read_text(encoding="utf-8"))
        jvm, t_tt = old_syn.get("jvm"), (old_syn.get("timings_s") or {}).get("transit_matrix")
        LOG.info("--car-only : matrice TC reprise (%d paires)", len(tt))
    else:
        tt, jvm, t_tt = transit_matrix(cfg, pts)

    car, peak = car_matrix(cfg, pts)
    geod = Geod(ellps="WGS84")
    xy = pts.set_index("id").geometry
    df = tt.merge(car, on=["from_id", "to_id"], how="left")
    _, _, d = geod.inv(xy.loc[df["from_id"]].x.to_numpy(), xy.loc[df["from_id"]].y.to_numpy(),
                       xy.loc[df["to_id"]].x.to_numpy(), xy.loc[df["to_id"]].y.to_numpy())
    df["dist_km"] = d / 1000
    df = df[df["from_id"] != df["to_id"]].reset_index(drop=True)
    df.to_parquet(pdir / "all_pairs.parquet", index=False)

    # Indicateurs par unité d'origine
    names = pts.set_index("id")["name"]
    pop = pts.set_index("id")["population"]
    df["v_eff_kmh"] = df["dist_km"] / (df["t_p50"] / 60)
    df["dead_zone"] = (df["dist_km"] < th["dead_zone_max_km"]) & ((df["t_p50"] > th["dead_zone_min_min"]) | df["t_p50"].isna())
    rows = []
    for oid, g in df.groupby("from_id"):
        ok = g["t_p50"].notna()
        rows.append({"unit_id": oid, "name": names[oid], "population": int(pop[oid]),
                     "n_reached": int(ok.sum()), "n_unreached": int((~ok).sum()),
                     "t_p50_median": float(g.loc[ok, "t_p50"].median()),
                     "v_eff_median_kmh": float(g.loc[ok, "v_eff_kmh"].median()),
                     "v_eff_popweighted_kmh": float(np.average(g.loc[ok, "v_eff_kmh"], weights=pop[g.loc[ok, "to_id"]])),
                     "spearman_dist_time": float(g.loc[ok, ["dist_km", "t_p50"]].corr(method="spearman").iloc[0, 1]),
                     "ratio_tc_car_median": float((g.loc[ok, "t_p50"] / g.loc[ok, "car_time"]).median()),
                     "ratio_tc_car_peak_median": (float((g.loc[ok, "t_p50"] / g.loc[ok, "car_time_peak"]).median())
                                                  if "car_time_peak" in g else None),
                     "n_dead_zones": int(g["dead_zone"].sum())})
    per_unit = pd.DataFrame(rows).sort_values("v_eff_popweighted_kmh")
    per_unit.to_csv(odir / "units_phase2.csv", index=False, float_format="%.3f")

    # Paires remarquables (chaque paire non orientée une fois : on garde le sens le plus lent)
    ok = df[df["t_p50"].notna()].copy()
    ok["pair"] = [tuple(sorted(p)) for p in zip(ok["from_id"], ok["to_id"])]
    near = ok[ok["dist_km"] <= th["near_max_km"]].sort_values("v_eff_kmh").drop_duplicates("pair").head(25)
    far = ok[ok["dist_km"] >= th["far_min_km"]].sort_values("v_eff_kmh", ascending=False).drop_duplicates("pair").head(25)
    top = pd.concat([near.assign(kind="nah_fern"), far.assign(kind="fern_nah")])
    top = top.assign(from_name=top["from_id"].map(names), to_name=top["to_id"].map(names))
    top[["kind", "from_id", "from_name", "to_id", "to_name", "dist_km", "t_p50", "car_time"]
        + (["car_time_peak"] if "car_time_peak" in top else []) + ["v_eff_kmh"]] \
        .to_csv(odir / "pairs_top.csv", index=False, float_format="%.2f")

    bands = pd.cut(ok["dist_km"], DIST_BANDS_KM, right=False)
    by_band = ok.groupby(bands, observed=True).agg(n=("t_p50", "size"), t_p50_median=("t_p50", "median"),
                                                   v_eff_median=("v_eff_kmh", "median"),
                                                   ratio_tc_car_median=("car_time", lambda c: float((ok.loc[c.index, "t_p50"] / c).median())),
                                                   **({"ratio_tc_car_peak_median": ("car_time_peak", lambda c: float((ok.loc[c.index, "t_p50"] / c).median()))}
                                                      if "car_time_peak" in ok else {}))
    syn = {
        "region": cfg["region"]["id"], "core_city": cfg["core_city"]["name"], "radius_km": cfg["analysis"]["radius_km"],
        "date": str(cfg["analysis"]["date"]),
        "window": [cfg["analysis"]["window_start"], cfg["analysis"]["window_end"]],
        "n_units": int(len(pts)), "n_pairs": int(len(df)), "n_pairs_unreached": int(df["t_p50"].isna().sum()),
        "t_p50_median": float(ok["t_p50"].median()), "v_eff_median_kmh": float(ok["v_eff_kmh"].median()),
        "spearman_dist_time": float(ok[["dist_km", "t_p50"]].corr(method="spearman").iloc[0, 1]),
        "ratio_tc_car_median": float((ok["t_p50"] / ok["car_time"]).median()),
        "ratio_tc_car_peak_median": (float((ok["t_p50"] / ok["car_time_peak"]).median())
                                     if "car_time_peak" in ok else None),
        "car_peak": {"source": peak[0], "congestion_pct": peak[1]} if peak else None,
        "share_dead_zone_pairs_below_dist": float(df.loc[df["dist_km"] < th["dead_zone_max_km"], "dead_zone"].mean()),
        "by_distance_band": {str(k): {kk: (round(float(vv), 3) if pd.notna(vv) else None) for kk, vv in r.items()}
                             for k, r in by_band.iterrows()},
        "jvm": jvm, "timings_s": {"transit_matrix": round(t_tt, 1), "total": round(time.time() - t_start, 1)},
    }
    (odir / "synthese_phase2.json").write_text(json.dumps(syn, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Synthèse : %s", {k: syn[k] for k in ("n_units", "n_pairs", "n_pairs_unreached", "t_p50_median",
                                                   "v_eff_median_kmh", "spearman_dist_time", "ratio_tc_car_median", "ratio_tc_car_peak_median",
                                                   "share_dead_zone_pairs_below_dist")})
    LOG.info("Par classe de distance : %s", syn["by_distance_band"])

    a = cfg["analysis"]
    log_p1(odir,
           (f"Phase 2 : {len(pts)} unités à ≤ {a['radius_km']} km de {cfg['core_city']['name']} (points officiels, "
            f"contrôle des îlots piétons), toutes paires ; TC = médiane sur {a['window_start']}–{a['window_end']} le "
            f"{a['date']}, mêmes paramètres et même réseau que l'origine {cfg['origin']['slug']} (zone de découpe "
            f"couvrant ces unités + {a['clip_buffer_km']} km) ; voiture = OSRM sans trafic (06) et en pointe du matin "
            f"(facteurs par classe de route de 06, C3) ; distance géodésique."),
           (f"Phase 2: {len(pts)} Einheiten ≤ {a['radius_km']} km von {cfg['core_city']['name']} (amtliche Punkte, "
            f"Prüfung auf Fußweg-Inseln), alle Paare; ÖV = Median {a['window_start']}–{a['window_end']} am "
            f"{a['date']}, gleiche Parameter und gleiches Netz wie Ursprung {cfg['origin']['slug']} (Zuschnitt deckt "
            f"diese Einheiten + {a['clip_buffer_km']} km ab); Pkw = OSRM ohne Verkehr (06) und in der Morgenspitze "
            f"(Faktoren je Straßenklasse aus 06, C3); geodätische Entfernung."),
           {"n_units": int(len(pts)), "network_from": cfg["origin"]["slug"]})
    LOG.info("Terminé en %.0f s.", time.time() - t_start)


if __name__ == "__main__":
    main()
