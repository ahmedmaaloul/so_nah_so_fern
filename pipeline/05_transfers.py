"""
Étape 05 — Correspondances : itinéraires détaillés et nombre de changements.

Pour des départs espacés de analysis.itinerary_step_min minutes dans la fenêtre
[window_start, window_end[, retrouve avec r5py (DetailedItineraries) l'itinéraire le
plus rapide de chaque origine vers chaque destination, en compte les
correspondances et résume, par OD, ce que l'on rencontre « typiquement ».

    uv run python pipeline/05_transfers.py --config config/garches.yaml
        [--benchmark]   un seul départ (le représentatif) puis extrapolation, et arrêt
        [--fresh]       ignore les résultats déjà calculés par départ

Aucune valeur d'analyse n'est écrite en dur (date, fenêtre, pas, vitesse de marche,
temps max., snapping, mémoire JVM : config). Deux paramètres de CONTRÔLE ont une
valeur de repli ici, surchargeable dans la config :
  routing.itinerary_window_min   (repli : 5)  fenêtre de chaque requête DetailedItineraries
                                 (1 minute fait déborder le tas de la JVM, voir run_departure)
  routing.itinerary_direct_walk  (repli : true) inclure la marche directe origine→destination
                                 parmi les options (comme le fait R5 pour la matrice de 04)

Définitions (T1_transfers_definition) :
  n_transit_legs = segments dont le mode n'est pas WALK ; transfers = max(n_transit_legs − 1, 0) ;
  walk_only      = aucun segment en transport en commun ;
  chain / mode_chain = lignes (route_short_name du GTFS) / modes des segments TC, joints par « › ».

Entrées : celles de 04 (origines, destinations, réseau R5) et tt_transit.parquet (cohérence).
Sorties :
  processed_dir/transfers.parquet        agrégat par OD (voir aggregate())
  processed_dir/itineraries_rep.gpkg     segments de l'option retenue au départ représentatif
  interim_dir/transfers_by_departure/    résultats par départ (reprise après interruption)
  outputs_dir/qa_transfers.json          contrôles qualité
  outputs_dir/assumptions.jsonl          T1_transfers_definition
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import shutil
import sys
import time
import warnings
import zipfile
from collections import Counter
from datetime import datetime, timedelta

import geopandas as gpd
import numpy as np
import pandas as pd

from common import (cli, get_logger, interim_dir, load_config, log_assumption,
                    outputs_dir, processed_dir)

# Fonctions communes de l'étape 04 (démarrage de r5py, entrées, réseau, paramètres)
t04 = importlib.import_module("04_transit_times")

LOG = get_logger("05_transfers")

# --------------------------------------------------------------------------- #
# Valeurs de repli des paramètres de CONTRÔLE (pas des paramètres d'analyse)
# --------------------------------------------------------------------------- #
DEFAULT_ITINERARY_WINDOW_MIN = 5          # routing.itinerary_window_min
DEFAULT_ITINERARY_DIRECT_WALK = True      # routing.itinerary_direct_walk
BENCHMARK_MAX_TOTAL_MIN = 90              # seuil du benchmark (consigne de lancement), pas un paramètre d'analyse
CHAIN_SEP = " › "


# --------------------------------------------------------------------------- #
# Paramètres
# --------------------------------------------------------------------------- #
def itinerary_window(cfg: dict) -> timedelta:
    return timedelta(minutes=int(cfg["routing"].get("itinerary_window_min", DEFAULT_ITINERARY_WINDOW_MIN)))


def direct_walk(cfg: dict) -> bool:
    return bool(cfg["routing"].get("itinerary_direct_walk", DEFAULT_ITINERARY_DIRECT_WALK))


def departures(cfg: dict) -> list[datetime]:
    """Un départ tous les itinerary_step_min minutes, de window_start à window_end exclu."""
    start, window = t04.departure_and_window(cfg)
    step = timedelta(minutes=int(cfg["analysis"]["itinerary_step_min"]))
    out, t = [], start
    while t < start + window:
        out.append(t)
        t += step
    return out


def representative_departure(cfg: dict, deps: list[datetime]) -> datetime:
    """Départ le plus proche du milieu de la fenêtre (à égalité : le plus tôt)."""
    start, window = t04.departure_and_window(cfg)
    mid = start + window / 2
    return min(deps, key=lambda d: (abs(d - mid), d))


def route_names(cfg: dict) -> dict[str, str]:
    """route_id -> nom affiché de la ligne (route_short_name, sinon route_long_name)."""
    _, gtfs = t04.stage_inputs(cfg)
    names: dict[str, str] = {}
    for zpath in gtfs:
        with zipfile.ZipFile(zpath) as z:
            routes = pd.read_csv(z.open("routes.txt"), dtype=str).fillna("")
        for rid, short, long_ in zip(routes["route_id"], routes["route_short_name"], routes["route_long_name"]):
            names[rid] = short.strip() or long_.strip() or rid
    return names


# --------------------------------------------------------------------------- #
# Un départ : DetailedItineraries -> option la plus rapide par OD
# --------------------------------------------------------------------------- #
def run_departure(r5py, network, cfg: dict, origins, dest, dt: datetime, names: dict) -> tuple[pd.DataFrame, pd.DataFrame, gpd.GeoDataFrame]:
    """Renvoie (options, meilleures options, segments de TOUTES les options).

    options : une ligne par (from_id, to_id, option) avec durée totale, nombre de segments TC, chaînes.
    """
    kwargs = t04.r5_common_kwargs(cfg, r5py)
    kwargs["departure"] = dt
    modes = list(kwargs["transport_modes"])
    if direct_walk(cfg) and r5py.TransportMode.WALK not in modes:
        modes.append(r5py.TransportMode.WALK)       # marche directe parmi les options
    kwargs["transport_modes"] = modes
    # r5py lance autant de threads Python que la moitié des cœurs de la machine ; chacun
    # appelle R5 : on s'aligne sur le plafond de cœurs de la JVM (machine moins chaude)
    r5py.DetailedItineraries.NUM_THREADS = max(1, int(cfg["routing"].get("jvm_active_processors") or 1))
    with warnings.catch_warnings():
        # r5py avertit en dessous de 5 min de fenêtre (utile si on la règle plus bas)
        warnings.filterwarnings("ignore", message="The provided departure time window is below 5 minutes")
        raw = r5py.DetailedItineraries(
            network, origins=origins, destinations=dest, snap_to_network=t04.snap_option(cfg),
            force_all_to_all=True, departure_time_window=itinerary_window(cfg), **kwargs)
    return summarize_options(raw, dt, names)


def summarize_options(raw: gpd.GeoDataFrame, dt: datetime, names: dict):
    seg = pd.DataFrame(raw.drop(columns="geometry"))
    if seg.empty:
        return seg, seg, raw
    seg["mode"] = seg["transport_mode"].map(lambda m: m.name)
    seg["is_transit"] = seg["transport_mode"].map(lambda m: bool(m.is_transit_mode))
    seg["travel_min"] = pd.to_timedelta(seg["travel_time"]).dt.total_seconds() / 60.0
    seg["wait_min"] = pd.to_timedelta(seg["wait_time"]).dt.total_seconds() / 60.0
    seg["departure_time"] = pd.to_datetime(seg["departure_time"])
    seg["end_time"] = seg["departure_time"] + pd.to_timedelta(seg["travel_time"])
    seg["route"] = [names.get(r, r) if (t and r) else "" for r, t in zip(seg["route_id"], seg["is_transit"])]
    seg["tw_min"] = seg["travel_min"] + seg["wait_min"]
    seg = seg.sort_values(["from_id", "to_id", "option", "segment"], kind="stable")
    keys = ["from_id", "to_id", "option"]
    g = seg.groupby(keys, sort=False)
    opt = g.agg(sum_tw_min=("tw_min", "sum"), n_segments=("segment", "size"),
                n_transit_legs=("is_transit", "sum"), last_end=("end_time", "last")).reset_index()
    tr = seg[seg["is_transit"]]
    chains = tr.groupby(keys, sort=False).agg(chain=("route", CHAIN_SEP.join), mode_chain=("mode", CHAIN_SEP.join)).reset_index()
    opt = opt.merge(chains, on=keys, how="left")
    opt[["chain", "mode_chain"]] = opt[["chain", "mode_chain"]].fillna("")
    opt["n_transit_legs"] = opt["n_transit_legs"].astype(int)
    # Instant de départ de l'option = embarquement − attente au 1er arrêt − marche d'accès.
    # (On ne se fie pas à departure_time du segment d'accès : r5py réutilise et écrase le même
    # objet « marche d'accès » pour plusieurs options.)
    first_tr = tr.groupby(keys, sort=False).first().reset_index()[keys + ["departure_time", "wait_min"]]
    access = seg[(seg["segment"] == 0) & ~seg["is_transit"]][keys + ["travel_min"]].rename(columns={"travel_min": "access_min"})
    first_tr = first_tr.rename(columns={"wait_min": "first_wait_min"})
    opt = opt.merge(first_tr, on=keys, how="left").merge(access, on=keys, how="left")
    opt["instant"] = opt["departure_time"] - pd.to_timedelta(opt["first_wait_min"] + opt["access_min"].fillna(0), unit="min")
    opt["offset_min"] = (opt["instant"] - dt).dt.total_seconds() / 60.0           # NaN pour la marche directe
    opt["clock_min"] = (opt["last_end"] - dt).dt.total_seconds() / 60.0           # arrivée − dt (si horodaté)
    opt = opt.drop(columns=["departure_time", "last_end", "instant"])
    return opt, seg, raw


def choose_total(opt: pd.DataFrame) -> pd.DataFrame:
    """Durée d'une option depuis l'instant dt et sélection de la plus rapide par OD.

    total_min = décalage de départ + somme (travel_time + wait_time) des segments : c'est
    « l'heure d'arrivée − dt » d'une personne prête à dt qui attend jusqu'au départ de
    cette option (décalage > 0 si l'option part après dt dans la fenêtre de la requête).
    La marche directe n'a pas de décalage. À durée égale : moins de segments TC, puis
    plus petit numéro d'option.
    """
    opt = opt.copy()
    opt["total_min"] = opt["sum_tw_min"] + opt["offset_min"].fillna(0).clip(lower=0)
    opt["transfers"] = (opt["n_transit_legs"] - 1).clip(lower=0)
    best = (opt.sort_values(["from_id", "to_id", "total_min", "n_transit_legs", "option"], kind="stable")
               .drop_duplicates(["from_id", "to_id"], keep="first"))
    return opt, best


def representative_segments(seg: pd.DataFrame, raw: gpd.GeoDataFrame, best: pd.DataFrame) -> gpd.GeoDataFrame:
    """Tous les segments de l'option retenue de chaque OD (pour l'affichage sur la carte).

    Colonnes : from_id, to_id, segment, transport_mode, route, route_id, departure_time,
    travel_time et wait_time (minutes), distance (mètres), geometry (EPSG:4326).
    """
    idx = seg.set_index(["from_id", "to_id", "option"]).index
    chosen = seg[idx.isin(best.set_index(["from_id", "to_id", "option"]).index)]
    rs = gpd.GeoDataFrame(chosen.assign(geometry=raw.geometry.loc[chosen.index].values),
                          geometry="geometry", crs=raw.crs or "EPSG:4326")
    rs = rs.rename(columns={"transport_mode": "transport_mode_enum", "mode": "transport_mode"})
    rs["travel_time"] = rs["travel_min"].round(2)
    rs["wait_time"] = rs["wait_min"].round(2)
    rs["distance"] = rs["distance"].astype(float).round(1)
    cols = ["from_id", "to_id", "segment", "transport_mode", "route", "route_id", "departure_time",
            "travel_time", "wait_time", "distance", "geometry"]
    return rs[cols].sort_values(["from_id", "to_id", "segment"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Agrégation par OD
# --------------------------------------------------------------------------- #
def most_frequent(values: pd.Series) -> str:
    """Valeur la plus fréquente ; à égalité, la première dans l'ordre alphabétique."""
    c = Counter(values)
    top = max(c.values())
    return sorted(v for v, n in c.items() if n == top)[0]


def aggregate(per_dep: pd.DataFrame, origins: gpd.GeoDataFrame, dest: gpd.GeoDataFrame) -> pd.DataFrame:
    """Une ligne par OD (toutes les OD, même sans itinéraire : n_departures_found = 0)."""
    rows = []
    for (f, t), g in per_dep.groupby(["from_id", "to_id"], sort=False):
        typical = most_frequent(g["chain"])
        same = g[g["chain"] == typical]
        rows.append({
            "from_id": f, "to_id": t, "n_departures_found": int(len(g)),
            "median_total_min": float(g["total_min"].median()),
            "median_transfers": float(g["transfers"].median()),
            "mean_transfers": float(g["transfers"].mean()),
            "share_2plus_transfers": float((g["transfers"] >= 2).mean()),
            "share_walk_only": float(g["walk_only"].mean()),
            "typical_chain": typical, "typical_mode_chain": most_frequent(same["mode_chain"]),
        })
    agg = pd.DataFrame(rows)
    grid = pd.MultiIndex.from_product([origins["id"], dest["id"]], names=["from_id", "to_id"]).to_frame(index=False)
    agg = grid.merge(agg, on=["from_id", "to_id"], how="left")
    agg["n_departures_found"] = agg["n_departures_found"].fillna(0).astype(int)
    return agg


# --------------------------------------------------------------------------- #
# Reprise : un fichier par départ
# --------------------------------------------------------------------------- #
def signature(cfg: dict, names_n: int) -> str:
    """Empreinte des paramètres et des fichiers d'entrée : change -> résultats par départ invalidés."""
    osm, gtfs = t04.stage_inputs(cfg)
    files = [(p.name, p.resolve().stat().st_size, int(p.resolve().stat().st_mtime)) for p in [osm] + gtfs]
    payload = {"a": {k: str(v) for k, v in cfg["analysis"].items()}, "r": {k: str(v) for k, v in cfg["routing"].items()},
               "iw": itinerary_window(cfg).total_seconds(), "dw": direct_walk(cfg), "files": files, "rule": TOTAL_RULE}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


TOTAL_RULE = "offset+sum_tw"   # durée d'une option, voir choose_total() ; fait partie de la signature des résultats


def ckpt_dir(cfg: dict, sig: str, fresh: bool):
    d = interim_dir(cfg) / "transfers_by_departure"
    marker = d / "signature.txt"
    if d.exists() and (fresh or not marker.exists() or marker.read_text().strip() != sig):
        LOG.info("Résultats par départ invalidés (%s)", "--fresh" if fresh else "paramètres ou entrées modifiés")
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    marker.write_text(sig + "\n")
    return d


# --------------------------------------------------------------------------- #
# Vérifications
# --------------------------------------------------------------------------- #
REF_WINDOW = timedelta(minutes=1)     # référence « un seul départ » : TravelTimeMatrix à l'instant dt


def q(s: pd.Series, qs=(0, .05, .25, .5, .75, .95, 1)) -> dict:
    s = s.dropna()
    return {f"q{int(x * 100):02d}": round(float(s.quantile(x)), 2) for x in qs} | {"n": int(len(s))} if len(s) else {"n": 0}


def verify_wait_included(r5py, network, cfg, origins, dest, dt, opt, best) -> dict:
    """Vérifie que la durée d'une option inclut l'attente initiale et colle au temps de R5.

    (1) horloge : durée retenue (décalage + Σ travel_time + wait_time) comparée à
        « heure d'arrivée du dernier segment − dt » ;
    (2) attente initiale : attente au 1er arrêt (wait_time du 1er segment TC) des options retenues ;
    (3) référence R5 : TravelTimeMatrix à l'instant dt seul (fenêtre d'1 minute), qui
        mesure lui aussi « porte à porte avec attente initiale ».
    """
    out: dict = {"departure": dt.strftime("%H:%M")}
    tr = opt[opt["n_transit_legs"] > 0]
    clock_gap = tr["clock_min"] - tr["total_min"]
    out["options_with_transit"] = int(len(tr))
    out["clock_minus_total_min"] = q(clock_gap) | {"by_n_transit_legs": {
        int(k): round(float(v.mean()), 2) for k, v in clock_gap.groupby(tr["n_transit_legs"])}}
    bt = best[best["n_transit_legs"] > 0]
    out["best_first_wait_min"] = q(bt["first_wait_min"])
    out["best_access_min"] = q(bt["access_min"])
    out["best_offset_min"] = q(best["offset_min"])
    out["n_options_per_od"] = q(opt.groupby(["from_id", "to_id"]).size())
    kwargs = t04.r5_common_kwargs(cfg, r5py)
    kwargs["departure"] = dt
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The provided departure time window is below 5 minutes")
        m = r5py.TravelTimeMatrix(network, origins=origins, destinations=dest, snap_to_network=t04.snap_option(cfg),
                                  departure_time_window=REF_WINDOW, percentiles=[50], **kwargs)
    m = t04.plain(m).rename(columns={"travel_time": "ref_min"})[["from_id", "to_id", "ref_min"]]
    j = best.merge(m, on=["from_id", "to_id"], how="outer")
    both = j.dropna(subset=["total_min", "ref_min"])
    diff = both["total_min"] - both["ref_min"]
    out["vs_r5_single_departure"] = {
        "n_od_both": int(len(both)), "n_only_itinerary": int((j["total_min"].notna() & j["ref_min"].isna()).sum()),
        "n_only_r5_matrix": int((j["total_min"].isna() & j["ref_min"].notna()).sum()),
        "diff_total_minus_ref": q(diff),
        "share_abs_le_1min": round(float((diff.abs() <= 1).mean()), 4) if len(diff) else None,
        "share_abs_le_3min": round(float((diff.abs() <= 3).mean()), 4) if len(diff) else None,
        "mean_diff_by_transfers": {int(k): round(float(v.mean()), 2) for k, v in
                                   diff.groupby(both["transfers"].clip(upper=3))} if len(diff) else None,
    }
    return out


def distribution(s: pd.Series) -> dict:
    s = s.dropna()
    if s.empty:
        return {"n": 0}
    return {"n": int(len(s)), "min": round(float(s.min()), 1), "p05": round(float(s.quantile(.05)), 1),
            "p25": round(float(s.quantile(.25)), 1), "median": round(float(s.median()), 1),
            "mean": round(float(s.mean()), 1), "p75": round(float(s.quantile(.75)), 1),
            "p95": round(float(s.quantile(.95)), 1), "max": round(float(s.max()), 1)}


# --------------------------------------------------------------------------- #
# Programme principal
# --------------------------------------------------------------------------- #
def main() -> None:
    parser: argparse.ArgumentParser = cli("Étape 05 : correspondances (r5py DetailedItineraries).")
    parser.add_argument("--benchmark", action="store_true",
                        help="calcule seulement le départ représentatif, extrapole la durée et s'arrête")
    parser.add_argument("--fresh", action="store_true", help="ignore les résultats par départ déjà calculés")
    args = parser.parse_args()
    cfg = load_config(args.config)
    t_start = time.time()
    a = cfg["analysis"]
    origins, dest = t04.load_od(cfg)
    deps = departures(cfg)
    rep = representative_departure(cfg, deps)
    LOG.info("Origine %s — %d origines × %d destinations = %d OD ; %d départs (%s … %s, pas %d min), "
             "départ représentatif %s, fenêtre de chaque requête %d min, marche directe : %s",
             cfg["origin"]["slug"], len(origins), len(dest), len(origins) * len(dest), len(deps),
             deps[0].strftime("%H:%M"), deps[-1].strftime("%H:%M"), int(a["itinerary_step_min"]),
             rep.strftime("%H:%M"), int(itinerary_window(cfg).total_seconds() // 60), direct_walk(cfg))

    r5py, jvm = t04.start_r5(cfg)
    network = t04.build_network(r5py, cfg)
    names = route_names(cfg)
    sig = signature(cfg, len(names))
    cdir = ckpt_dir(cfg, sig, args.fresh)
    todo = [rep] if args.benchmark else deps

    timings: dict[str, float] = {}
    verification = None
    for dt in sorted(todo, key=lambda d: (d != rep, d)):         # le représentatif d'abord
        f_dep = cdir / f"dep_{dt:%H%M}.parquet"
        f_rep = cdir / "rep_segments.gpkg"
        if f_dep.exists() and (dt != rep or f_rep.exists()):
            LOG.info("Départ %s : déjà calculé (reprise)", dt.strftime("%H:%M"))
            timings[dt.strftime("%H:%M")] = float("nan")
            continue
        t0 = time.time()
        opt, seg, raw = run_departure(r5py, network, cfg, origins, dest, dt, names)
        opt, best = choose_total(opt)
        el = time.time() - t0
        timings[dt.strftime("%H:%M")] = round(el, 1)
        n_od = best[["from_id", "to_id"]].drop_duplicates().shape[0]
        LOG.info("Départ %s : %d OD avec itinéraire sur %d, %d options, %d segments — %.1f s",
                 dt.strftime("%H:%M"), n_od, len(origins) * len(dest), len(opt), len(seg), el)
        res = best[["from_id", "to_id", "option", "total_min", "sum_tw_min", "offset_min", "first_wait_min",
                    "clock_min", "n_transit_legs", "transfers", "chain", "mode_chain"]].copy()
        res["walk_only"] = res["n_transit_legs"] == 0
        res.insert(2, "departure", dt.strftime("%H:%M"))
        res = res.merge(opt.groupby(["from_id", "to_id"]).size().rename("n_options").reset_index(),
                        on=["from_id", "to_id"], how="left")
        if dt == rep:
            verification = verify_wait_included(r5py, network, cfg, origins, dest, dt, opt, best)
            LOG.info("Vérification attente initiale (%s) : %s", dt.strftime("%H:%M"), json.dumps(verification))
            opt.to_parquet(cdir / f"options_{dt:%H%M}.parquet", index=False)     # toutes les options, pour analyse a posteriori
            rs = representative_segments(seg, raw, best)
            rs.to_file(f_rep, driver="GPKG", layer="itineraries")
            (cdir / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
        res.to_parquet(f_dep, index=False)

    if args.benchmark:
        t = [v for v in timings.values() if v == v]
        if t:
            tot = t[0] * len(deps) / 60
            LOG.info("BENCHMARK : %.1f s pour 1 départ (%d OD) -> extrapolation %d départs : %.1f min (seuil %d min)",
                     t[0], len(origins) * len(dest), len(deps), tot, BENCHMARK_MAX_TOTAL_MIN)
        else:
            LOG.info("BENCHMARK : départ déjà calculé, voir transfers_by_departure/ (pas de chronométrage)")
        return

    # ---- Agrégation de tous les départs
    per_dep = pd.concat([pd.read_parquet(cdir / f"dep_{d:%H%M}.parquet") for d in deps], ignore_index=True)
    agg = aggregate(per_dep, origins, dest)
    pdir = processed_dir(cfg)
    agg.to_parquet(pdir / "transfers.parquet", index=False)
    LOG.info("Écrit : %s (%d OD, %d sans itinéraire)", pdir / "transfers.parquet", len(agg), int((agg["n_departures_found"] == 0).sum()))

    rs = gpd.read_file(cdir / "rep_segments.gpkg", layer="itineraries")
    out_gpkg = pdir / "itineraries_rep.gpkg"
    if out_gpkg.exists():
        out_gpkg.unlink()
    rs.to_file(out_gpkg, driver="GPKG", layer="itineraries")
    LOG.info("Écrit : %s (%d segments, %d OD, départ %s)", out_gpkg, len(rs),
             rs[["from_id", "to_id"]].drop_duplicates().shape[0], rep.strftime("%H:%M"))

    # ---- Contrôles
    qa = qa_transfers(cfg, agg, per_dep, dest, rep, deps)
    verification = verification or json.loads((cdir / "verification.json").read_text(encoding="utf-8"))
    qa["verification_initial_wait"] = verification
    qa["timings_s"] = {"per_departure": timings, "total_run": round(time.time() - t_start, 1)}
    qa["jvm"] = jvm
    qpath = outputs_dir(cfg) / "qa_transfers.json"
    qpath.write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Écrit : %s", qpath)
    log_t1(cfg, rep, deps)
    LOG.info("Terminé en %.0f s.", time.time() - t_start)


def qa_transfers(cfg, agg, per_dep, dest, rep, deps) -> dict:
    tt = pd.read_parquet(processed_dir(cfg) / "tt_transit.parquet")
    j = agg.merge(tt, on=["from_id", "to_id"], how="left")
    j["diff_vs_p50"] = j["median_total_min"] - j["travel_time_p50"]
    names = dest.set_index("id")["name"]
    j["name"] = j["to_id"].map(names)
    qa: dict = {"n_od": int(len(agg)), "n_departures": len(deps), "representative_departure": rep.strftime("%H:%M"),
                "n_od_without_itinerary": int((agg["n_departures_found"] == 0).sum()),
                "n_departures_found": {str(k): int(v) for k, v in agg["n_departures_found"].value_counts().sort_index().items()}}
    # OD avec temps en 04 mais sans itinéraire ici, et inversement
    qa["n_od_tt_ok_but_no_itinerary"] = int((j["travel_time_p50"].notna() & (j["n_departures_found"] == 0)).sum())
    qa["n_od_itinerary_but_tt_nan"] = int((j["travel_time_p50"].isna() & (j["n_departures_found"] > 0)).sum())
    ok = agg[agg["n_departures_found"] > 0]
    qa["median_transfers_distribution"] = {
        o: {"0": int((g["median_transfers"] < 0.5).sum()), "1": int(((g["median_transfers"] >= 0.5) & (g["median_transfers"] < 1.5)).sum()),
            "2": int(((g["median_transfers"] >= 1.5) & (g["median_transfers"] < 2.5)).sum()), "3+": int((g["median_transfers"] >= 2.5).sum()),
            "n": int(len(g))} for o, g in ok.groupby("from_id")}
    qa["median_transfers_value_counts"] = {str(k): int(v) for k, v in ok["median_transfers"].value_counts().sort_index().items()}
    qa["share_walk_only_gt0"] = int((ok["share_walk_only"] > 0).sum())
    qa["n_od_walk_only_always"] = int((ok["share_walk_only"] >= 1).sum())
    d = j["diff_vs_p50"].dropna()
    qa["median_total_minus_p50"] = distribution(d)
    qa["median_total_minus_p50_by_origin"] = {o: distribution(g["diff_vs_p50"]) for o, g in j.groupby("from_id")}
    qa["abs_diff_gt_5min"] = int((d.abs() > 5).sum())
    qa["abs_diff_gt_10min"] = int((d.abs() > 10).sum())
    ex = j.dropna(subset=["diff_vs_p50"])
    cols = ["from_id", "to_id", "name", "median_total_min", "travel_time_p50", "diff_vs_p50", "n_departures_found", "typical_chain"]
    qa["largest_positive_diffs"] = ex.sort_values("diff_vs_p50", ascending=False).head(10)[cols].round(1).to_dict("records")
    qa["largest_negative_diffs"] = ex.sort_values("diff_vs_p50").head(10)[cols].round(1).to_dict("records")
    return qa


def log_t1(cfg, rep, deps) -> None:
    a = cfg["analysis"]
    w = int(itinerary_window(cfg).total_seconds() // 60)
    dw = direct_walk(cfg)
    log_assumption(
        cfg, "05", "T1_transfers_definition",
        (f"Correspondances : {len(deps)} départs espacés de {a['itinerary_step_min']} min entre {a['window_start']} et "
         f"{a['window_end']} (exclu) ; pour chacun, r5py (DetailedItineraries, fenêtre de {w} min = un seul départ) liste les "
         f"itinéraires possibles et on retient le plus rapide (durée totale = marche d'accès + attente + temps en véhicule + "
         f"marche de correspondance et de sortie). Un segment est « transport en commun » si son mode n'est pas la marche ; "
         f"nombre de correspondances = max(segments TC − 1, 0) ; « marche seule » = aucun segment TC"
         f"{' (la marche directe est comptée parmi les options)' if dw else ''}. Par OD, on retient la médiane et la moyenne du "
         f"nombre de correspondances, la part des départs avec ≥ 2 correspondances, la part « marche seule » et la chaîne de "
         f"lignes la plus fréquente (noms GTFS route_short_name). Itinéraire affiché sur la carte : départ {rep:%H:%M}."),
        (f"Umstiege: {len(deps)} Abfahrten im Abstand von {a['itinerary_step_min']} Min. zwischen {a['window_start']} und "
         f"{a['window_end']} (exklusive); für jede liefert r5py (DetailedItineraries, Fenster {w} Min. = genau eine Abfahrt) die "
         f"möglichen Verbindungen, gewählt wird die schnellste (Gesamtdauer = Zugangsweg + Wartezeit + Fahrzeit + Fußweg beim "
         f"Umsteigen und zum Ziel). Ein Abschnitt zählt als ÖV, wenn sein Modus nicht Gehen ist; Anzahl Umstiege = "
         f"max(ÖV-Abschnitte − 1, 0); „nur zu Fuß“ = kein ÖV-Abschnitt"
         f"{' (der direkte Fußweg zählt als Option)' if dw else ''}. Je Verbindung werden Median und Mittel der Umstiege, der "
         f"Anteil der Abfahrten mit ≥ 2 Umstiegen, der Anteil „nur zu Fuß“ und die häufigste Linienkette (GTFS route_short_name) "
         f"festgehalten. Auf der Karte angezeigte Verbindung: Abfahrt {rep:%H:%M}."),
        {"n_departures": len(deps), "step_min": int(a["itinerary_step_min"]), "request_window_min": w,
         "direct_walk_option": dw, "representative_departure": rep.strftime("%H:%M")})


if __name__ == "__main__":
    main()
