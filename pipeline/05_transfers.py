"""
Étape 05 — Correspondances : nombre de changements et itinéraires détaillés.

Deux modes :

    uv run python pipeline/05_transfers.py --config config/garches.yaml
        Nombre de correspondances de chaque OD, par matrices successives.
    uv run python pipeline/05_transfers.py --config config/garches.yaml --itineraries
        Itinéraires détaillés des destinations mises en avant (tops, zones mortes),
        lues dans resultats_<slug>.csv : lancer APRÈS 07_indicators.py.

Méthode (T1_transfers_definition)
---------------------------------
R5 (TravelTimeMatrix) calcule la médiane des temps sur la fenêtre de départ en
limitant le nombre de véhicules empruntés à k = 1 … analysis.max_rides_tested.
La médiane « sans limite » est celle de 04 (tt_transit.parquet, limite r5py par
défaut de 8 véhicules). Pour chaque OD :

    k*           = plus petit k dont la médiane ≤ médiane sans limite + transfer_tolerance_min
    transfers    = k* − 1
    walk_only    = la marche seule (tt_walk.parquet) est au moins aussi rapide que la
                   médiane sans limite  → transfers = 0
    censored     = aucun k ≤ max_rides_tested ne suffit → transfers = max_rides_tested
                   (à lire « au moins max_rides_tested »)

Cette définition donne le nombre de correspondances qu'il FAUT accepter pour
voyager (presque) aussi vite que possible, en médiane sur la fenêtre ; elle ne
compte pas les correspondances d'un itinéraire particulier. Une matrice coûte
quelques secondes, contre plus de 17 min par départ pour DetailedItineraries
sur toutes les OD (ancienne méthode, abandonnée).

Itinéraires détaillés : DetailedItineraries pour un seul départ
(analysis.itinerary_departure), fenêtre de routing.itinerary_window_min minutes
(repli : 10), seulement vers les destinations mises en avant. On garde l'option
la plus rapide (durée = décalage de départ + somme marche/attente/véhicule).

Entrées : celles de 04 (origines, destinations, réseau R5), tt_transit.parquet, tt_walk.parquet.
Sorties :
  processed_dir/transfers.parquet          from_id, to_id, transfers, transfers_censored,
                                           walk_only, p50_rides_1 … p50_rides_K, p50_unlimited
  processed_dir/itineraries.gpkg           (--itineraries) segments de l'option retenue
  processed_dir/itineraries.parquet        (--itineraries) une ligne par OD : chaîne de lignes, durée
  outputs_dir/qa_transfers.json            contrôles qualité
  outputs_dir/assumptions.jsonl            T1_transfers_definition, T2_itineraries
"""
from __future__ import annotations

import importlib
import json
import time
import warnings
import zipfile
from datetime import datetime, timedelta

import geopandas as gpd
import numpy as np
import pandas as pd

from common import cli, get_logger, load_config, log_assumption, outputs_dir, processed_dir

# Fonctions communes de l'étape 04 (démarrage de r5py, entrées, réseau, paramètres)
t04 = importlib.import_module("04_transit_times")

LOG = get_logger("05_transfers")

DEFAULT_ITINERARY_WINDOW_MIN = 10         # routing.itinerary_window_min (paramètre de CONTRÔLE)
CHAIN_SEP = " › "


# --------------------------------------------------------------------------- #
# Nombre de correspondances par matrices successives
# --------------------------------------------------------------------------- #
def rides_matrices(r5py, network, cfg: dict, origins, dest) -> tuple[pd.DataFrame, dict]:
    """Médiane p50 pour k = 1 … max_rides_tested véhicules ; renvoie (table large, durées)."""
    a = cfg["analysis"]
    _, window = t04.departure_and_window(cfg)
    kmax = int(a["max_rides_tested"])
    out, timings = None, {}
    for k in range(1, kmax + 1):
        t0 = time.time()
        kwargs = t04.r5_common_kwargs(cfg, r5py)
        m = r5py.TravelTimeMatrix(network, origins=origins, destinations=dest, snap_to_network=t04.snap_option(cfg),
                                  departure_time_window=window, percentiles=[50],
                                  max_public_transport_rides=k, **kwargs)
        m = t04.plain(m)
        col = "travel_time_p50" if "travel_time_p50" in m.columns else "travel_time"
        m = m[["from_id", "to_id", col]].rename(columns={col: f"p50_rides_{k}"})
        out = m if out is None else out.merge(m, on=["from_id", "to_id"], how="outer")
        timings[f"rides_{k}"] = round(time.time() - t0, 1)
        LOG.info("Matrice ≤ %d véhicule(s) : %d OD atteintes en %.1f s", k, int(m[f"p50_rides_{k}"].notna().sum()),
                 timings[f"rides_{k}"])
    return out, timings


def classify(cfg: dict, wide: pd.DataFrame) -> pd.DataFrame:
    """Applique la règle k* (voir docstring du module) à chaque OD."""
    a = cfg["analysis"]
    kmax, tol = int(a["max_rides_tested"]), float(a["transfer_tolerance_min"])
    pdir = processed_dir(cfg)
    tt = pd.read_parquet(pdir / "tt_transit.parquet")[["from_id", "to_id", "travel_time_p50"]]
    walk = pd.read_parquet(pdir / "tt_walk.parquet")[["from_id", "to_id", "walk_time"]]
    df = (tt.rename(columns={"travel_time_p50": "p50_unlimited"})
            .merge(walk, on=["from_id", "to_id"], how="left")
            .merge(wide, on=["from_id", "to_id"], how="left"))
    ref = df["p50_unlimited"]
    kstar = pd.Series(np.nan, index=df.index)
    for k in range(kmax, 0, -1):                 # du plus grand au plus petit : le dernier vrai gagne
        ok = df[f"p50_rides_{k}"].notna() & (df[f"p50_rides_{k}"] <= ref + tol)
        kstar[ok] = k
    df["walk_only"] = ref.notna() & df["walk_time"].notna() & (df["walk_time"] <= ref)
    df["transfers_censored"] = ref.notna() & kstar.isna() & ~df["walk_only"]
    df["transfers"] = kstar - 1
    df.loc[df["walk_only"], "transfers"] = 0
    df.loc[df["transfers_censored"], "transfers"] = kmax
    df.loc[ref.isna(), "transfers"] = np.nan
    cols = (["from_id", "to_id", "transfers", "transfers_censored", "walk_only", "p50_unlimited"]
            + [f"p50_rides_{k}" for k in range(1, kmax + 1)])
    return df[cols]


def qa_transfers(cfg: dict, df: pd.DataFrame, dest: gpd.GeoDataFrame) -> dict:
    kmax = int(cfg["analysis"]["max_rides_tested"])
    names = dest.set_index("id")["name"]
    qa: dict = {"n_od": int(len(df)), "n_od_with_time": int(df["p50_unlimited"].notna().sum()),
                "max_rides_tested": kmax, "tolerance_min": float(cfg["analysis"]["transfer_tolerance_min"])}
    qa["transfers_by_origin"] = {
        o: {str(int(k)) + ("+" if int(k) == kmax else ""): int(v)
            for k, v in g["transfers"].dropna().value_counts().sort_index().items()}
        for o, g in df.groupby("from_id")}
    qa["n_walk_only"] = {o: int(g["walk_only"].sum()) for o, g in df.groupby("from_id")}
    qa["n_censored"] = int(df["transfers_censored"].sum())
    # Monotonie : la médiane ne peut pas augmenter quand on autorise plus de véhicules
    viol = 0
    for k in range(1, kmax):
        a_, b_ = df[f"p50_rides_{k}"], df[f"p50_rides_{k + 1}"]
        viol += int((a_.notna() & b_.notna() & (b_ > a_ + 1e-9)).sum())
    qa["n_non_monotone_steps"] = viol
    # p50 à kmax plus rapide que la référence « sans limite » (incohérence de matrice)
    last = df[f"p50_rides_{kmax}"]
    qa["n_kmax_faster_than_unlimited"] = int((last.notna() & (last < df["p50_unlimited"] - 1e-9)).sum())
    ex = df[df["transfers_censored"]].assign(name=lambda d: d["to_id"].map(names))
    qa["censored_examples"] = ex.head(15)[["from_id", "to_id", "name", "p50_unlimited", f"p50_rides_{kmax}"]].round(1).to_dict("records")
    return qa


def log_t1(cfg: dict) -> None:
    a = cfg["analysis"]
    kmax, tol = int(a["max_rides_tested"]), a["transfer_tolerance_min"]
    log_assumption(
        cfg, "05", "T1_transfers_definition",
        (f"Correspondances : R5 recalcule la médiane du temps sur la fenêtre {a['window_start']}–{a['window_end']} en "
         f"limitant le nombre de véhicules empruntés à k = 1 … {kmax}. Nombre de correspondances = (plus petit k dont la "
         f"médiane dépasse d'au plus {tol} min la médiane sans limite) − 1 ; 0 si la marche seule est au moins aussi "
         f"rapide ; « {kmax} ou plus » si aucun k ≤ {kmax} ne suffit. C'est le nombre de changements qu'il faut accepter "
         f"pour voyager (presque) au plus vite, pas celui d'un itinéraire particulier."),
        (f"Umstiege: R5 berechnet den Median der Reisezeit im Fenster {a['window_start']}–{a['window_end']} erneut, "
         f"wobei höchstens k = 1 … {kmax} Fahrzeuge genutzt werden dürfen. Anzahl Umstiege = (kleinstes k, dessen Median "
         f"höchstens {tol} Min. über dem Median ohne Begrenzung liegt) − 1; 0, wenn der Fußweg allein mindestens so schnell "
         f"ist; „{kmax} oder mehr“, wenn kein k ≤ {kmax} genügt. Gemeint ist die Zahl der Umstiege, die man für eine "
         f"(nahezu) schnellste Reise in Kauf nehmen muss, nicht die einer bestimmten Verbindung."),
        {"max_rides_tested": kmax, "tolerance_min": tol, "reference": "04 tt_transit p50 (max_public_transport_rides=8)"})


# --------------------------------------------------------------------------- #
# Itinéraires détaillés (destinations mises en avant)
# --------------------------------------------------------------------------- #
def highlighted(cfg: dict) -> pd.DataFrame:
    """(origin_id, unit_id, raison) des tops et zones mortes, depuis resultats_<slug>.csv (étape 07)."""
    path = outputs_dir(cfg) / f"resultats_{cfg['origin']['slug']}.csv"
    if not path.exists():
        raise SystemExit(f"{path} absent : lancer 07_indicators.py avant --itineraries.")
    res = pd.read_csv(path, dtype={"unit_id": str, "origin_id": str})
    rows = []
    for col in ("top_nah_fern", "top_fern_nah", "dead_zone"):
        if col not in res.columns:
            continue
        sel = res[res[col].fillna(False).astype(bool)] if col == "dead_zone" else res[res[col].notna()]
        rows.append(sel[["origin_id", "unit_id"]].assign(reason=col))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["origin_id", "unit_id", "reason"])


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


def run_itineraries(r5py, network, cfg: dict, origins, dest, hl: pd.DataFrame) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    a = cfg["analysis"]
    dt = datetime.combine(a["date"], t04.parse_hhmm(a["itinerary_departure"]))
    window = timedelta(minutes=int(cfg["routing"].get("itinerary_window_min", DEFAULT_ITINERARY_WINDOW_MIN)))
    names = route_names(cfg)
    kwargs = t04.r5_common_kwargs(cfg, r5py)
    kwargs["departure"] = dt
    kwargs["transport_modes"] = list(kwargs["transport_modes"]) + [r5py.TransportMode.WALK]   # marche directe parmi les options
    r5py.DetailedItineraries.NUM_THREADS = max(1, int(cfg["routing"].get("jvm_active_processors") or 1))
    summaries, segments = [], []
    for oid, g in hl.groupby("origin_id"):
        o = origins[origins["id"] == oid]
        d = dest[dest["id"].isin(g["unit_id"].unique())]
        t0 = time.time()
        raw = r5py.DetailedItineraries(network, origins=o, destinations=d, snap_to_network=t04.snap_option(cfg),
                                       force_all_to_all=True, departure_time_window=window, **kwargs)
        LOG.info("Itinéraires %s → %d destinations : %d segments en %.1f s", oid, len(d), len(raw), time.time() - t0)
        if raw.empty:
            continue
        s, seg = summarize(raw, dt, names)
        summaries.append(s)
        segments.append(seg)
    if not summaries:
        return pd.DataFrame(), gpd.GeoDataFrame()
    best = pd.concat(summaries, ignore_index=True)
    seg = pd.concat(segments)
    keep = seg.set_index(["from_id", "to_id", "option"]).index.isin(best.set_index(["from_id", "to_id", "option"]).index)
    seg = seg[keep]
    gseg = gpd.GeoDataFrame(seg[["from_id", "to_id", "segment", "mode", "route", "route_id", "departure_time",
                                 "travel_min", "wait_min", "distance", "geometry"]], geometry="geometry", crs="EPSG:4326")
    reasons = hl.groupby(["origin_id", "unit_id"])["reason"].agg(",".join).reset_index()
    best = best.merge(reasons, left_on=["from_id", "to_id"], right_on=["origin_id", "unit_id"], how="left").drop(
        columns=["origin_id", "unit_id"])
    return best, gseg


def summarize(raw: gpd.GeoDataFrame, dt: datetime, names: dict) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    """Option la plus rapide par OD, depuis l'instant dt (décalage de départ compris)."""
    seg = raw.copy()
    seg["mode"] = seg["transport_mode"].map(lambda m: m.name)
    seg["is_transit"] = seg["transport_mode"].map(lambda m: bool(m.is_transit_mode))
    seg["travel_min"] = pd.to_timedelta(seg["travel_time"]).dt.total_seconds() / 60.0
    seg["wait_min"] = pd.to_timedelta(seg["wait_time"]).dt.total_seconds() / 60.0
    seg["departure_time"] = pd.to_datetime(seg["departure_time"])
    seg["route"] = [names.get(r, r) if (t and r) else "" for r, t in zip(seg["route_id"], seg["is_transit"])]
    seg["distance"] = seg["distance"].astype(float)
    seg = seg.sort_values(["from_id", "to_id", "option", "segment"], kind="stable")
    keys = ["from_id", "to_id", "option"]
    seg["tw"] = seg["travel_min"] + seg["wait_min"]
    opt = seg.groupby(keys, sort=False).agg(sum_tw=("tw", "sum"), n_transit=("is_transit", "sum")).reset_index()
    tr = seg[seg["is_transit"]]
    chains = tr.groupby(keys, sort=False).agg(chain=("route", CHAIN_SEP.join), mode_chain=("mode", CHAIN_SEP.join)).reset_index()
    # Instant de départ = embarquement − attente au 1er arrêt − marche d'accès
    first = tr.groupby(keys, sort=False).first().reset_index()[keys + ["departure_time", "wait_min"]]
    access = seg[(seg["segment"] == 0) & ~seg["is_transit"]][keys + ["travel_min"]].rename(columns={"travel_min": "access"})
    opt = opt.merge(chains, on=keys, how="left").merge(first, on=keys, how="left").merge(access, on=keys, how="left")
    start = opt["departure_time"] - pd.to_timedelta(opt["wait_min"] + opt["access"].fillna(0), unit="min")
    opt["offset_min"] = ((start - dt).dt.total_seconds() / 60.0).fillna(0).clip(lower=0)
    opt["total_min"] = opt["sum_tw"] + opt["offset_min"]
    opt["n_transit"] = opt["n_transit"].astype(int)
    opt["transfers"] = (opt["n_transit"] - 1).clip(lower=0)
    opt[["chain", "mode_chain"]] = opt[["chain", "mode_chain"]].fillna("")
    best = (opt.sort_values(["from_id", "to_id", "total_min", "n_transit", "option"], kind="stable")
               .drop_duplicates(["from_id", "to_id"], keep="first"))
    best = best[["from_id", "to_id", "option", "total_min", "offset_min", "transfers", "chain", "mode_chain"]]
    return best, seg


def log_t2(cfg: dict, n: int) -> None:
    a = cfg["analysis"]
    w = int(cfg["routing"].get("itinerary_window_min", DEFAULT_ITINERARY_WINDOW_MIN))
    log_assumption(
        cfg, "05", "T2_itineraries",
        (f"Itinéraires affichés sur la carte ({n} OD : tops et zones mortes) : départ {a['itinerary_departure']}, "
         f"r5py DetailedItineraries sur {w} min, option la plus rapide pour une personne prête à "
         f"{a['itinerary_departure']} (attente du départ comprise). Exemple illustratif d'un seul départ ; les temps "
         f"et correspondances des indicateurs restent ceux des médianes sur la fenêtre."),
        (f"Auf der Karte gezeigte Verbindungen ({n} Relationen: Tops und tote Zonen): Abfahrt {a['itinerary_departure']}, "
         f"r5py DetailedItineraries über {w} Min., schnellste Option für eine Person, die um {a['itinerary_departure']} "
         f"startbereit ist (Wartezeit bis zur Abfahrt inklusive). Beispiel einer einzelnen Abfahrt; Reisezeiten und "
         f"Umstiege der Indikatoren bleiben die Mediane über das Zeitfenster."),
        {"departure": a["itinerary_departure"], "window_min": w, "n_od": n})


# --------------------------------------------------------------------------- #
def main() -> None:
    parser = cli("Étape 05 : correspondances (matrices r5py à nombre de véhicules limité).")
    parser.add_argument("--itineraries", action="store_true",
                        help="itinéraires détaillés des destinations mises en avant (après 07)")
    args = parser.parse_args()
    cfg = load_config(args.config)
    t_start = time.time()
    origins, dest = t04.load_od(cfg)
    r5py, jvm = t04.start_r5(cfg)
    network = t04.build_network(r5py, cfg)
    if t04.snap_option(cfg):              # mêmes points déplacés qu'en 04 (îlots piétons)
        origins, _ = t04.fix_islands(r5py, network, cfg, origins, "origine")
        dest, _ = t04.fix_islands(r5py, network, cfg, dest, "destination")
    pdir, odir = processed_dir(cfg), outputs_dir(cfg)

    if args.itineraries:
        hl = highlighted(cfg)
        LOG.info("%d OD mises en avant : %s", len(hl), hl["reason"].value_counts().to_dict())
        best, gseg = run_itineraries(r5py, network, cfg, origins, dest, hl)
        best.to_parquet(pdir / "itineraries.parquet", index=False)
        out = pdir / "itineraries.gpkg"
        if out.exists():
            out.unlink()
        if len(gseg):
            gseg.to_file(out, driver="GPKG", layer="itineraries")
        LOG.info("Écrit : %s (%d OD), %s (%d segments)", pdir / "itineraries.parquet", len(best), out, len(gseg))
        log_t2(cfg, len(best))
        LOG.info("Terminé en %.0f s.", time.time() - t_start)
        return

    wide, timings = rides_matrices(r5py, network, cfg, origins, dest)
    df = classify(cfg, wide)
    df.to_parquet(pdir / "transfers.parquet", index=False)
    qa = qa_transfers(cfg, df, dest)
    qa["timings_s"] = timings | {"total": round(time.time() - t_start, 1)}
    qa["jvm"] = jvm
    (odir / "qa_transfers.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Correspondances par origine : %s ; marche seule : %s ; censurées : %d ; non monotones : %d",
             qa["transfers_by_origin"], qa["n_walk_only"], qa["n_censored"], qa["n_non_monotone_steps"])
    log_t1(cfg)
    LOG.info("Terminé en %.0f s.", time.time() - t_start)


if __name__ == "__main__":
    main()
