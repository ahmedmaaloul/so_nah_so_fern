"""
Étape 04 — Temps porte à porte en transports en commun (r5py / R5).

Calcule, depuis chaque origine (gare, centre) vers chaque destination du rayon,
le temps de trajet en transports en commun sur la fenêtre de départ de l'analyse,
plus un temps « marche seule » qui sert à repérer les liaisons où marcher est
la meilleure option.

    uv run python pipeline/04_transit_times.py --config config/garches.yaml

Aucune valeur d'analyse n'est écrite en dur : date, fenêtre, percentiles, vitesse
de marche, temps maximaux, mémoire JVM, snapping, modes viennent de la
configuration (config/default.yaml + fichier d'origine). Deux paramètres de
CONTRÔLE seulement ont une valeur de repli ici, surchargeable dans la config :
  routing.walk_only_max_time_min  (repli : 120)  durée max du calcul « marche seule »
  qa.max_plausible_speed_kmh      (repli : 60)   seuil de vitesse effective suspecte

Entrées (étapes 01 et 02), dans processed_dir(cfg) et interim_dir(cfg) :
  origins.geojson, destinations.parquet (in_radius == True),
  osm_clip.osm.pbf, gtfs/*.zip

Sorties :
  processed_dir/tt_transit.parquet   from_id, to_id, travel_time_p25/p50/p75 (minutes)
  processed_dir/tt_walk.parquet      from_id, to_id, walk_time (minutes)
  outputs_dir/qa_tt_transit.json     contrôles qualité
  outputs_dir/assumptions.jsonl      R1_r5_parameters, R2_window_semantics, R3_snapping

Ce module est aussi importé par 05_transfers.py (démarrage de r5py, chargement des
origines/destinations, construction du réseau, paramètres communs) : tout ce qui
touche à r5py est donc dans des fonctions, jamais à l'import.
"""
from __future__ import annotations

import json
import re
import time
import warnings
from collections import Counter
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from common import (cli, configure_jvm, get_logger, interim_dir, load_config,
                    log_assumption, outputs_dir, processed_dir)

LOG = get_logger("04_transit_times")

# --------------------------------------------------------------------------- #
# Valeurs de repli des paramètres de CONTRÔLE (pas des paramètres d'analyse)
# --------------------------------------------------------------------------- #
DEFAULT_WALK_ONLY_MAX_TIME_MIN = 120      # routing.walk_only_max_time_min
DEFAULT_MAX_PLAUSIBLE_SPEED_KMH = 60      # qa.max_plausible_speed_kmh


# --------------------------------------------------------------------------- #
# Paramètres lus dans la configuration
# --------------------------------------------------------------------------- #
def parse_hhmm(text: str) -> dtime:
    """« 07:30 » -> datetime.time (les heures de la config sont des chaînes)."""
    h, m = str(text).split(":")[:2]
    return dtime(int(h), int(m))


def departure_and_window(cfg: dict) -> tuple[datetime, timedelta]:
    """Premier départ (date + window_start) et durée de la fenêtre (end − start)."""
    a = cfg["analysis"]
    start = datetime.combine(a["date"], parse_hhmm(a["window_start"]))
    end = datetime.combine(a["date"], parse_hhmm(a["window_end"]))
    return start, end - start


def walk_only_max_time_min(cfg: dict) -> int:
    return int(cfg["routing"].get("walk_only_max_time_min", DEFAULT_WALK_ONLY_MAX_TIME_MIN))


def max_speed_kmh(cfg: dict) -> float:
    return float(cfg.get("qa", {}).get("max_plausible_speed_kmh", DEFAULT_MAX_PLAUSIBLE_SPEED_KMH))


# --------------------------------------------------------------------------- #
# Entrées : origines, destinations, fichiers du réseau
# --------------------------------------------------------------------------- #
def load_od(cfg: dict) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Origines (id, geometry) et destinations du rayon (id = unit_id, name, dist_km_*)."""
    pdir = processed_dir(cfg)
    origins = gpd.read_file(pdir / "origins.geojson")[["id", "geometry"]].copy()
    origins["id"] = origins["id"].astype(str)
    dest = gpd.read_parquet(pdir / "destinations.parquet")
    dest = dest[dest["in_radius"].astype(bool)].copy()
    dest = dest.rename(columns={"unit_id": "id"})
    dest["id"] = dest["id"].astype(str)
    keep = ["id", "name", "level"] + [c for c in dest.columns if c.startswith("dist_km_")] + ["geometry"]
    dest = dest[[c for c in keep if c in dest.columns]].reset_index(drop=True)
    if dest["id"].duplicated().any() or origins["id"].duplicated().any():
        raise SystemExit("Identifiants dupliqués dans les origines ou les destinations.")
    return origins.to_crs("EPSG:4326"), dest.to_crs("EPSG:4326")


def stage_inputs(cfg: dict) -> tuple[Path, list[Path]]:
    """Fichiers d'entrée de R5, sous des noms propres à l'origine.

    r5py ne travaille pas sur les fichiers eux-mêmes mais sur un lien créé dans
    ~/.cache/r5py/<nom du fichier>, et le réutilise tel quel s'il existe déjà.
    Deux origines dont le pbf s'appelle toutes deux « osm_clip.osm.pbf » se
    retrouveraient donc sur LE MÊME lien, c.-à-d. sur le réseau de la première.
    On passe à r5py des liens préfixés par le slug de l'origine
    (data/interim/<slug>/r5_inputs/<slug>__osm_clip.osm.pbf) pour l'éviter. Le cache
    du réseau, lui, est indexé par le contenu des fichiers : rien n'est recalculé.
    """
    idir = interim_dir(cfg)
    slug = cfg["origin"]["slug"]
    osm = idir / "osm_clip.osm.pbf"
    gtfs = sorted((idir / "gtfs").glob("*.zip"))
    tmp = list((idir / "gtfs").glob("*.tmp")) + list(idir.glob("*.pbf.tmp"))
    if tmp:
        raise SystemExit(f"Découpe (étape 02) en cours d'écriture : {[p.name for p in tmp]}")
    if not osm.exists() or not gtfs:
        raise SystemExit(f"Entrées manquantes dans {idir} (osm_clip.osm.pbf, gtfs/*.zip) : lancer l'étape 02.")
    stage = idir / "r5_inputs"
    stage.mkdir(exist_ok=True)
    out = []
    for src in [osm] + gtfs:
        link = stage / f"{slug}__{src.name}"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(src.resolve())
        out.append(link)
    return out[0], out[1:]


# --------------------------------------------------------------------------- #
# Démarrage de r5py
# --------------------------------------------------------------------------- #
def start_r5(cfg: dict):
    """Importe r5py avec le heap et le nombre de cœurs de la config ; renvoie (module r5py, infos JVM).

    common.configure_jvm() doit passer AVANT l'import : il règle --max-memory (lu par
    r5py dans sys.argv, qu'il remplace pour que nos options ne lui parviennent pas) et
    plafonne le nombre de cœurs de la JVM (routing.jvm_active_processors).
    """
    configure_jvm(cfg)
    import jpype
    import r5py
    from r5py.util.classpath import R5_CLASSPATH
    runtime = jpype.JClass("java.lang.Runtime").getRuntime()
    m = re.search(r"r5-v([0-9][0-9A-Za-z.\-]*?)(?:-r5py)?-all", str(R5_CLASSPATH))
    info = {
        "r5py": r5py.__version__,
        "r5": m.group(1) if m else Path(str(R5_CLASSPATH)).name,
        "java": str(jpype.JClass("java.lang.System").getProperty("java.version")),
        "jvm_max_memory_requested": str(cfg["routing"]["jvm_max_memory"]),
        "jvm_max_heap_bytes": int(runtime.maxMemory()),
        "jvm_available_processors": int(runtime.availableProcessors()),
    }
    LOG.info("JVM : heap max %.2f Go (demandé %s), %d cœurs — Java %s, R5 %s, r5py %s",
             info["jvm_max_heap_bytes"] / 2**30, info["jvm_max_memory_requested"],
             info["jvm_available_processors"], info["java"], info["r5"], info["r5py"])
    return r5py, info


def build_network(r5py, cfg: dict):
    """TransportNetwork (cache r5py : lent la 1re fois, quasi instantané ensuite)."""
    osm, gtfs = stage_inputs(cfg)
    LOG.info("Réseau R5 : %s + %s", osm.name, [g.name for g in gtfs])
    t0 = time.time()
    network = r5py.TransportNetwork(osm, gtfs)
    LOG.info("Réseau R5 prêt en %.0f s", time.time() - t0)
    return network


def snap_option(cfg: dict):
    """routing.snap_to_network : booléen, ou rayon de recherche en mètres."""
    v = cfg["routing"]["snap_to_network"]
    return v if isinstance(v, bool) else (int(v) if v else False)


def r5_common_kwargs(cfg: dict, r5py) -> dict:
    """Paramètres de routage communs à 04 et 05 (hors fenêtre, percentiles, snapping)."""
    r = cfg["routing"]
    modes = lambda names: [r5py.TransportMode[n] for n in names]  # noqa: E731
    departure, _ = departure_and_window(cfg)
    return dict(
        departure=departure,
        transport_modes=modes(r["transit_modes"]),
        access_modes=modes(r["access_modes"]),
        speed_walking=float(r["walk_speed_kmh"]),
        max_time=timedelta(minutes=int(r["max_time_min"])),
        max_time_walking=timedelta(minutes=int(r["max_walk_time_min"])),
    )


def snapping_stats(network, points: gpd.GeoDataFrame, metric_crs: str) -> dict:
    """Écart (m) entre chaque point et sa position après accrochage au réseau piéton."""
    snapped = network.snap_to_network(points.geometry)
    not_snapped = snapped.is_empty.to_numpy()
    d = points.geometry.to_crs(metric_crs).distance(snapped.to_crs(metric_crs), align=False).to_numpy()
    d = d[~not_snapped]
    return {
        "n": int(len(points)), "n_not_snapped": int(not_snapped.sum()),
        "median_m": round(float(np.median(d)), 1) if len(d) else None,
        "p90_m": round(float(np.percentile(d, 90)), 1) if len(d) else None,
        "max_m": round(float(d.max()), 1) if len(d) else None,
        "n_gt_100m": int((d > 100).sum()), "n_gt_500m": int((d > 500).sum()),
    }


# --------------------------------------------------------------------------- #
# Calculs
# --------------------------------------------------------------------------- #
def plain(df) -> pd.DataFrame:
    """Les matrices r5py sont des GeoDataFrame ; on garde un DataFrame simple."""
    return pd.DataFrame({c: df[c].to_numpy() for c in df.columns})


def summarize_warnings(caught: list) -> list[dict]:
    """Avertissements Python émis par r5py : message unique + nombre d'occurrences."""
    counts = Counter((w.category.__name__, str(w.message)) for w in caught)
    return [{"category": c, "message": m[:300], "count": n} for (c, m), n in counts.most_common()]


def qa_checks(cfg: dict, tt: pd.DataFrame, walk: pd.DataFrame, dest: gpd.GeoDataFrame,
              pcts: list[int]) -> dict:
    """Contrôles loggés et écrits dans qa_tt_transit.json."""
    p50 = f"travel_time_p{50}"
    names = dest.set_index("id")
    dcols = [c for c in dest.columns if c.startswith("dist_km_")]
    j = tt.merge(walk, on=["from_id", "to_id"], how="left")
    for oid in j["from_id"].unique():
        c = f"dist_km_{oid}"
        if c in names.columns:
            j.loc[j["from_id"] == oid, "dist_km"] = j.loc[j["from_id"] == oid, "to_id"].map(names[c])
    j["name"] = j["to_id"].map(names["name"])

    def row(r, extra=()):
        d = {"from_id": r["from_id"], "to_id": r["to_id"], "name": r["name"],
             "dist_km": None if pd.isna(r.get("dist_km")) else round(float(r["dist_km"]), 2)}
        for k in extra:
            d[k] = None if pd.isna(r[k]) else round(float(r[k]), 1)
        return d

    qa: dict = {"n_origins": int(tt["from_id"].nunique()), "n_destinations": int(tt["to_id"].nunique()),
                "n_od": int(len(tt))}
    # 1. OD sans temps
    nan = j[j[p50].isna()]
    qa["n_nan_by_percentile"] = {f"p{p}": int(tt[f"travel_time_p{p}"].isna().sum()) for p in pcts}
    qa["nan_od"] = [row(r, ["walk_time"]) for _, r in nan.sort_values(["from_id", "dist_km"]).iterrows()]
    # 2. min / médiane / max du p50 par origine
    qa["p50_by_origin"] = {}
    for oid, g in tt.groupby("from_id"):
        v = g[p50].dropna()
        qa["p50_by_origin"][oid] = {"n_valid": int(len(v)), "min": float(v.min()), "median": float(v.median()),
                                    "max": float(v.max())}
    # 3. cohérence des percentiles (p25 <= p50 <= p75)
    ordered = tt[[f"travel_time_p{p}" for p in sorted(pcts)]].dropna()
    qa["percentiles_monotone"] = bool((ordered.diff(axis=1).iloc[:, 1:] >= 0).all().all())
    # 4. p50 > temps de marche seule
    both = j.dropna(subset=[p50, "walk_time"])
    worse = both[both[p50] > both["walk_time"]]
    qa["p50_gt_walk"] = {
        "n_od_with_both": int(len(both)),
        "n_strict": int(len(worse)),
        "n_gt_1min": int((both[p50] > both["walk_time"] + 1).sum()),
        "max_excess_min": None if worse.empty else float((worse[p50] - worse["walk_time"]).max()),
        "examples": [row(r, [p50, "walk_time"]) for _, r in
                     worse.assign(ex=worse[p50] - worse["walk_time"]).sort_values("ex", ascending=False).head(15).iterrows()],
    }
    qa["walk_better_or_equal"] = int((both["walk_time"] <= both[p50]).sum())
    qa["walk_only_nan_where_transit_ok"] = int((j["walk_time"].isna() & j[p50].notna()).sum())
    # 5. vitesse effective > seuil
    vmax = max_speed_kmh(cfg)
    ok = j.dropna(subset=[p50, "dist_km"])
    ok = ok[ok[p50] > 0]
    ok = ok.assign(speed_kmh=ok["dist_km"] / (ok[p50] / 60.0))
    fast = ok[ok["speed_kmh"] > vmax]
    qa["speed_gt_threshold"] = {
        "threshold_kmh": vmax, "n": int(len(fast)),
        "max_kmh": None if ok.empty else round(float(ok["speed_kmh"].max()), 1),
        "examples": [row(r, [p50, "speed_kmh"]) for _, r in fast.sort_values("speed_kmh", ascending=False).head(15).iterrows()],
    }
    return qa


def main() -> None:
    args = cli("Étape 04 : temps porte à porte en transports en commun (r5py).").parse_args()
    cfg = load_config(args.config)
    t_start = time.time()
    a, r = cfg["analysis"], cfg["routing"]
    LOG.info("Origine %s — date %s, fenêtre %s–%s", cfg["origin"]["slug"], a["date"],
             a["window_start"], a["window_end"])

    origins, dest = load_od(cfg)
    LOG.info("%d origines %s, %d destinations (in_radius)", len(origins), origins["id"].tolist(), len(dest))

    r5py, jvm = start_r5(cfg)
    network = build_network(r5py, cfg)
    t_net = time.time() - t_start

    kwargs = r5_common_kwargs(cfg, r5py)
    snap = snap_option(cfg)
    departure, window = departure_and_window(cfg)
    pcts = [int(p) for p in a["percentiles"]]
    walk_max = walk_only_max_time_min(cfg)

    # Accrochage au réseau : écart par point (pour R3_snapping)
    metric = cfg["region"]["metric_crs"]
    snap_o = snapping_stats(network, origins, metric) if snap else None
    snap_d = snapping_stats(network, dest, metric) if snap else None
    LOG.info("Accrochage origines : %s", snap_o)
    LOG.info("Accrochage destinations : %s", snap_d)

    # 1. Matrice transports en commun, fenêtre complète
    t0 = time.time()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ttm = r5py.TravelTimeMatrix(network, origins=origins, destinations=dest, snap_to_network=snap,
                                    departure_time_window=window, percentiles=pcts, **kwargs)
    tt = plain(ttm)
    t_tt = time.time() - t0
    LOG.info("Matrice transit : %d OD en %.1f s — colonnes %s", len(tt), t_tt, list(tt.columns))
    warns_tt = summarize_warnings(caught)

    # 2. Matrice marche seule (durée max propre à ce calcul)
    t0 = time.time()
    walk_kwargs = dict(kwargs)
    walk_kwargs.update(transport_modes=[r5py.TransportMode.WALK],
                       max_time=timedelta(minutes=walk_max), max_time_walking=timedelta(minutes=walk_max))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        wm = r5py.TravelTimeMatrix(network, origins=origins, destinations=dest, snap_to_network=snap,
                                   **walk_kwargs)
    walk = plain(wm).rename(columns={"travel_time": "walk_time"})[["from_id", "to_id", "walk_time"]]
    t_walk = time.time() - t0
    LOG.info("Matrice marche seule : %d OD en %.1f s, %d OD atteintes à pied (≤ %d min)", len(walk), t_walk,
             int(walk["walk_time"].notna().sum()), walk_max)
    warns_walk = summarize_warnings(caught)

    # Écriture des tables
    pdir = processed_dir(cfg)
    tt = tt[["from_id", "to_id"] + [f"travel_time_p{p}" for p in pcts]]
    tt.to_parquet(pdir / "tt_transit.parquet", index=False)
    walk.to_parquet(pdir / "tt_walk.parquet", index=False)
    LOG.info("Écrit : %s, %s", pdir / "tt_transit.parquet", pdir / "tt_walk.parquet")

    # 3. Contrôles
    qa = qa_checks(cfg, tt, walk, dest, pcts)
    qa.update({
        "origin": cfg["origin"]["slug"], "date": str(a["date"]),
        "window": f"{a['window_start']}-{a['window_end']}", "percentiles": pcts,
        "jvm": jvm, "snapping": {"origins": snap_o, "destinations": snap_d},
        "r5_warnings": {"transit": warns_tt, "walk": warns_walk},
        "timings_s": {"network": round(t_net, 1), "transit_matrix": round(t_tt, 1),
                      "walk_matrix": round(t_walk, 1), "total": round(time.time() - t_start, 1)},
    })
    qpath = outputs_dir(cfg) / "qa_tt_transit.json"
    qpath.write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Contrôles : %d OD, %d sans temps (p50), p50 par origine %s", qa["n_od"],
             qa["n_nan_by_percentile"]["p50"], qa["p50_by_origin"])
    LOG.info("p50 > marche seule : %s", {k: v for k, v in qa["p50_gt_walk"].items() if k != "examples"})
    LOG.info("Vitesse effective > %s km/h : %d OD (max %s km/h)", qa["speed_gt_threshold"]["threshold_kmh"],
             qa["speed_gt_threshold"]["n"], qa["speed_gt_threshold"]["max_kmh"])
    for w in warns_tt + warns_walk:
        LOG.warning("R5/r5py : %s × %s", w["count"], w["message"])
    LOG.info("Écrit : %s", qpath)

    # 4. Hypothèses
    params = {
        "date": str(a["date"]), "departure": departure.isoformat(timespec="minutes"),
        "departure_time_window_min": int(window.total_seconds() // 60), "percentiles": pcts,
        "transport_modes": r["transit_modes"], "access_modes": r["access_modes"],
        "egress_modes": r["access_modes"], "walk_speed_kmh": r["walk_speed_kmh"],
        "max_time_min": r["max_time_min"], "max_walk_time_min": r["max_walk_time_min"],
        "max_public_transport_rides": 8, "snap_to_network": snap, "walk_only_max_time_min": walk_max,
        "jvm_max_memory": r["jvm_max_memory"], **{k: v for k, v in jvm.items() if k not in ("jvm_max_memory_requested",)},
    }
    log_assumption(
        cfg, "04", "R1_r5_parameters",
        (f"Routage r5py {jvm['r5py']} / R5 {jvm['r5']} (Java {jvm['java']}, heap {jvm['jvm_max_heap_bytes'] / 2**30:.1f} Go) : "
         f"départ {departure:%d/%m/%Y %H:%M}, fenêtre de {params['departure_time_window_min']} min, percentiles {pcts}, "
         f"modes {r['transit_modes']} avec accès/sortie à pied ({r['access_modes']}), marche à {r['walk_speed_kmh']} km/h, "
         f"marche max. {r['max_walk_time_min']} min en accès ou sortie, durée max. {r['max_time_min']} min, "
         f"au plus 8 correspondances (valeur par défaut de r5py), accrochage au réseau piéton : {snap}. "
         f"Matrice « marche seule » : durée max. {walk_max} min."),
        (f"Routing r5py {jvm['r5py']} / R5 {jvm['r5']} (Java {jvm['java']}, Heap {jvm['jvm_max_heap_bytes'] / 2**30:.1f} GB): "
         f"Abfahrt {departure:%d.%m.%Y %H:%M}, Zeitfenster {params['departure_time_window_min']} Min., Perzentile {pcts}, "
         f"Modi {r['transit_modes']} mit Zu-/Abgang zu Fuß ({r['access_modes']}), Gehgeschwindigkeit {r['walk_speed_kmh']} km/h, "
         f"max. Fußweg {r['max_walk_time_min']} Min. bei Zu-/Abgang, max. Reisezeit {r['max_time_min']} Min., "
         f"höchstens 8 Fahrten (r5py-Standardwert), Einrasten ins Fußwegenetz: {snap}. "
         f"Matrix „nur zu Fuß“: max. Dauer {walk_max} Min."),
        params)
    log_assumption(
        cfg, "04", "R2_window_semantics",
        (f"Pour chaque origine, R5 simule un départ à chaque minute de la fenêtre [{a['window_start']}, {a['window_end']}[ "
         f"({params['departure_time_window_min']} départs) et calcule le temps de chaque trajet jusqu'à chaque destination. "
         f"Les percentiles {pcts} sont pris sur ces départs (p50 = médiane, indicateur principal). Le temps est mesuré "
         f"« porte à porte » à partir de l'instant de départ : marche d'accès, ATTENTE du premier véhicule à l'arrêt "
         f"(incluse, comme celle des correspondances), temps en véhicule et marche de sortie. R5 inclut aussi la marche "
         f"directe de l'origine à la destination quand elle est plus rapide."),
        (f"Für jede Herkunft simuliert R5 in jeder Minute des Zeitfensters [{a['window_start']}, {a['window_end']}[ eine Abfahrt "
         f"({params['departure_time_window_min']} Abfahrten) und berechnet die Reisezeit zu jedem Ziel. Die Perzentile {pcts} "
         f"werden über diese Abfahrten gebildet (P50 = Median, Hauptindikator). Gemessen wird „von Haustür zu Haustür“ ab dem "
         f"Abfahrtszeitpunkt: Zugangsweg zu Fuß, WARTEZEIT auf das erste Fahrzeug an der Haltestelle (eingeschlossen, ebenso "
         f"wie beim Umsteigen), Fahrzeit und Fußweg zum Ziel. R5 berücksichtigt auch den direkten Fußweg, wenn er schneller ist."),
        {"window_min": params["departure_time_window_min"], "n_departures": params["departure_time_window_min"],
         "percentiles": pcts})
    log_assumption(
        cfg, "04", "R3_snapping",
        (f"Origines et destinations sont accrochées au réseau piéton OSM le plus proche (rayon de recherche par défaut de R5) "
         f"avant le calcul. Écart d'accrochage, origines : {snap_o}. Destinations (points officiels des communes, qui ne "
         f"tombent pas forcément sur une voie) : {snap_d}. Les destinations non accrochables sont exclues par r5py."
         if snap else "Aucun accrochage au réseau : les points sont utilisés tels quels."),
        (f"Herkunfts- und Zielpunkte werden vor der Berechnung auf das nächstgelegene OSM-Fußwegenetz eingerastet "
         f"(R5-Standardsuchradius). Verschiebung, Herkunft: {snap_o}. Ziele (amtliche Gemeindepunkte, die nicht "
         f"zwingend auf einer Straße liegen): {snap_d}. Nicht einrastbare Ziele schließt r5py aus."
         if snap else "Kein Einrasten ins Netz: Die Punkte werden unverändert verwendet."),
        {"snap_to_network": snap, "origins": snap_o, "destinations": snap_d})
    LOG.info("Terminé en %.0f s.", time.time() - t_start)


if __name__ == "__main__":
    main()
