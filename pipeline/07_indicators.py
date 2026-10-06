"""
07 — Indicateurs « So nah, so fern » et tableau de résultats.

Rôle : à partir des temps calculés par r5py (04, 05) et, s'ils existent, des
temps voiture (06), calcule pour chaque origine (gare, centre) et chaque
destination du rayon :

  v_eff_kmh        vitesse effective = distance à vol d'oiseau / temps TC
  rank_dist        rang en distance (1 = la plus proche)
  rank_time        rang en temps TC médian (1 = la plus rapide)
  paradox_index    rank_time − rank_dist  (> 0 : « plus loin qu'il n'y paraît »)
  paradox_norm     paradox_index / N (comparable entre régions de tailles ≠)
  time_excess_pct  écart au temps « attendu » pour cette distance, d'après une
                   régression log(temps) ~ log(distance) propre à l'origine
                   (+50 % = moitié plus long qu'une destination typique
                   à la même distance) — indépendant du nombre de communes
  ratio_tc_car     temps TC / temps voiture (si 06 a tourné)
  dead_zone        distance < seuil ET temps TC > seuil (config `thresholds`)
  top_nah_fern     top N « so nah, so fern » : paradox_index le plus élevé
                   parmi les destinations à ≤ near_max_km
  top_fern_nah     top N « so fern, so nah » : paradox_index le plus bas
                   parmi les destinations à ≥ far_min_km

La commune d'origine elle-même est conservée dans le tableau mais exclue des
rangs, de la régression et des tops (distance quasi nulle).

Usage :
    uv run python pipeline/07_indicators.py --config config/garches.yaml

Sorties (outputs_dir) :
    resultats_<slug>.csv     une ligne par (origine, destination)
    synthese_<slug>.json     chiffres de synthèse par origine (comparaison)
    nuage_<slug>.png         nuage distance/temps statique (contrôle visuel)
"""
from __future__ import annotations

import json

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from common import cli, get_logger, load_config, log_assumption, outputs_dir, processed_dir

log = get_logger("07_indicators")


# --------------------------------------------------------------------------- #
# Chargement
# --------------------------------------------------------------------------- #
def load_inputs(cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Renvoie (destinations du rayon, temps OD fusionnés)."""
    pdir = processed_dir(cfg)
    dest = gpd.read_parquet(pdir / "destinations.parquet")
    dest = dest[dest["in_radius"]].copy()
    dest["lon"], dest["lat"] = dest.geometry.x, dest.geometry.y
    dest = pd.DataFrame(dest.drop(columns="geometry"))

    tt = pd.read_parquet(pdir / "tt_transit.parquet")
    walk = pd.read_parquet(pdir / "tt_walk.parquet")
    od = tt.merge(walk, on=["from_id", "to_id"], how="left")

    # Correspondances (05) : facultatif à ce stade, colonnes NaN sinon
    tr_path = pdir / "transfers.parquet"
    if tr_path.exists():
        tr = pd.read_parquet(tr_path)[["from_id", "to_id", "median_transfers",
                                       "share_walk_only", "share_2plus_transfers",
                                       "typical_chain", "typical_mode_chain"]]
        od = od.merge(tr, on=["from_id", "to_id"], how="left")
    else:
        log.warning("transfers.parquet absent : colonnes de correspondances vides")

    # Voiture (06) : facultatif
    car_path = pdir / "tt_car.parquet"
    if car_path.exists():
        car = pd.read_parquet(car_path)[["from_id", "to_id", "car_time"]]
        od = od.merge(car, on=["from_id", "to_id"], how="left")
    else:
        od["car_time"] = np.nan
        log.warning("tt_car.parquet absent : ratio TC/voiture non calculé")
    return dest, od


# --------------------------------------------------------------------------- #
# Indicateurs pour une origine
# --------------------------------------------------------------------------- #
def indicators_for_origin(cfg: dict, origin_id: str, dest: pd.DataFrame,
                          od: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    th = cfg["thresholds"]
    df = dest.merge(od[od["from_id"] == origin_id], left_on="unit_id",
                    right_on="to_id", how="left")
    df["origin_id"] = origin_id
    df["dist_km"] = df[f"dist_km_{origin_id}"]
    df["t_tc"] = df["travel_time_p50"]
    df["t_tc_spread"] = df["travel_time_p75"] - df["travel_time_p25"]
    df["v_eff_kmh"] = df["dist_km"] / (df["t_tc"] / 60)
    df["ratio_tc_car"] = df["t_tc"] / df["car_time"]

    # Ensemble de référence pour les rangs : destinations atteintes, hors
    # commune d'origine
    ref = df["t_tc"].notna() & ~df["is_origin_commune"]
    n = int(ref.sum())
    df["rank_dist"] = df.loc[ref, "dist_km"].rank(method="average")
    df["rank_time"] = df.loc[ref, "t_tc"].rank(method="average")
    df["paradox_index"] = df["rank_time"] - df["rank_dist"]
    df["paradox_norm"] = df["paradox_index"] / n

    # Temps « attendu » : log(t) = a + b·log(d), MCO sur l'ensemble de référence
    x, y = np.log(df.loc[ref, "dist_km"]), np.log(df.loc[ref, "t_tc"])
    b, a = np.polyfit(x, y, 1)
    resid = np.log(df["t_tc"]) - (a + b * np.log(df["dist_km"]))
    df["time_excess_pct"] = np.where(ref, (np.exp(resid) - 1) * 100, np.nan)
    r2 = 1 - ((y - (a + b * x)) ** 2).sum() / ((y - y.mean()) ** 2).sum()

    # Zones mortes : proches mais lentes (ou inatteignables dans max_time)
    near = (df["dist_km"] < th["dead_zone_max_km"]) & ~df["is_origin_commune"]
    df["dead_zone"] = near & ((df["t_tc"] > th["dead_zone_min_min"]) | df["t_tc"].isna())

    # Tops : le rang 1 est le cas le plus paradoxal
    near_set = ref & (df["dist_km"] <= th["near_max_km"])
    far_set = ref & (df["dist_km"] >= th["far_min_km"])
    df["top_nah_fern"] = (df.loc[near_set, "paradox_index"]
                          .rank(ascending=False, method="first"))
    df["top_fern_nah"] = (df.loc[far_set, "paradox_index"]
                          .rank(ascending=True, method="first"))
    for col in ("top_nah_fern", "top_fern_nah"):
        df.loc[df[col] > th["top_n"], col] = np.nan

    # Synthèse : pondérée par la population quand c'est pertinent, car les
    # communes françaises sont bien plus petites que les Gemeinden allemandes
    w = df.loc[ref, "population"]
    summary = {
        "origin_id": origin_id,
        "n_destinations": int(len(df)),
        "n_reference": n,
        "n_unreachable": int(df["t_tc"].isna().sum()),
        "population_reference": int(w.sum()),
        "t_tc_median_min": float(df.loc[ref, "t_tc"].median()),
        "v_eff_median_kmh": float(df.loc[ref, "v_eff_kmh"].median()),
        "v_eff_popweighted_median_kmh": weighted_median(df.loc[ref, "v_eff_kmh"], w),
        "loglog_fit": {"a": float(a), "b": float(b), "r2": float(r2)},
        "spearman_dist_time": float(df.loc[ref, ["dist_km", "t_tc"]]
                                    .corr(method="spearman").iloc[0, 1]),
        "dead_zones": {
            "n": int(df["dead_zone"].sum()),
            "n_candidates_below_dist": int(near.sum()),
            "population": int(df.loc[df["dead_zone"], "population"].sum()),
        },
        "median_transfers": (float(df.loc[ref, "median_transfers"].median())
                             if "median_transfers" in df else None),
        "ratio_tc_car_median": (float(df.loc[ref, "ratio_tc_car"].median())
                                if df["ratio_tc_car"].notna().any() else None),
    }
    return df, summary


def weighted_median(values: pd.Series, weights: pd.Series) -> float:
    order = np.argsort(values.to_numpy())
    v, w = values.to_numpy()[order], weights.to_numpy()[order]
    cum = np.cumsum(w)
    return float(v[np.searchsorted(cum, cum[-1] / 2)])


# --------------------------------------------------------------------------- #
# Figure de contrôle
# --------------------------------------------------------------------------- #
def scatter(cfg: dict, res: pd.DataFrame, path) -> None:
    origins = res["origin_id"].unique()
    fig, axes = plt.subplots(1, len(origins), figsize=(7 * len(origins), 6), squeeze=False)
    for ax, oid in zip(axes[0], origins):
        d = res[(res["origin_id"] == oid) & ~res["is_origin_commune"]]
        ax.scatter(d["dist_km"], d["t_tc"], s=np.sqrt(d["population"]) / 4,
                   c=d["paradox_index"], cmap="RdBu_r", alpha=0.8, edgecolor="none")
        for col, color in (("top_nah_fern", "firebrick"), ("top_fern_nah", "navy")):
            for _, r in d[d[col].notna()].iterrows():
                ax.annotate(r["name"], (r["dist_km"], r["t_tc"]), fontsize=7, color=color)
        for kmh in (10, 20, 30):            # repères de vitesse effective
            xs = np.array([0, d["dist_km"].max()])
            ax.plot(xs, xs / kmh * 60, lw=0.6, ls="--", color="grey")
            ax.text(xs[1], xs[1] / kmh * 60, f"{kmh} km/h", fontsize=7, color="grey")
        th = cfg["thresholds"]
        ax.axvspan(0, th["dead_zone_max_km"], ymin=0, ymax=1, alpha=0.04, color="red")
        ax.axhline(th["dead_zone_min_min"], lw=0.6, color="red")
        ax.set_xlabel("Luftlinie (km)")
        ax.set_ylabel("ÖV-Reisezeit, Median 07:30–09:00 (min)")
        ax.set_title(f"{cfg['origin']['name']} — {oid}")
        ax.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------- #
def main() -> None:
    args = cli(__doc__.splitlines()[1]).parse_args()
    cfg = load_config(args.config)
    slug = cfg["origin"]["slug"]
    dest, od = load_inputs(cfg)

    frames, summaries = [], []
    for p in cfg["origin"]["points"]:
        df, s = indicators_for_origin(cfg, p["id"], dest, od)
        frames.append(df)
        summaries.append(s)
        log.info("[%s] %d destinations, %d sans temps, v_eff médiane %.1f km/h, "
                 "zones mortes %d, log-log b=%.2f R²=%.2f", p["id"], s["n_destinations"],
                 s["n_unreachable"], s["v_eff_median_kmh"], s["dead_zones"]["n"],
                 s["loglog_fit"]["b"], s["loglog_fit"]["r2"])
    res = pd.concat(frames, ignore_index=True)

    cols = ["origin_id", "unit_id", "name", "level", "parent_id", "population",
            "point_source", "lon", "lat", "dist_km", "dist_km_core",
            "travel_time_p25", "t_tc", "travel_time_p75", "t_tc_spread", "walk_time",
            "car_time", "v_eff_kmh", "rank_dist", "rank_time", "paradox_index",
            "paradox_norm", "time_excess_pct", "ratio_tc_car", "median_transfers",
            "share_walk_only", "share_2plus_transfers", "typical_chain",
            "typical_mode_chain", "dead_zone", "top_nah_fern", "top_fern_nah",
            "is_origin_commune"]
    res = res[[c for c in cols if c in res.columns]].sort_values(["origin_id", "dist_km"])
    out = outputs_dir(cfg)
    res.to_csv(out / f"resultats_{slug}.csv", index=False, float_format="%.3f")

    meta = {
        "origin": cfg["origin"]["name"], "date": str(cfg["analysis"]["date"]),
        "window": [cfg["analysis"]["window_start"], cfg["analysis"]["window_end"]],
        "radius_km": cfg["analysis"]["radius_km"], "thresholds": cfg["thresholds"],
        "walk_speed_kmh": cfg["routing"]["walk_speed_kmh"], "origins": summaries,
    }
    (out / f"synthese_{slug}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                              encoding="utf-8")
    scatter(cfg, res, out / f"nuage_{slug}.png")

    log_assumption(
        cfg, "07", "I1_indicators",
        "Indice de paradoxe = rang en temps − rang en distance, calculé hors commune "
        "d'origine et hors destinations sans temps ; complété par l'écart en % au temps "
        "attendu selon une régression log(temps) ~ log(distance) propre à chaque origine.",
        "Paradoxie-Index = Rang nach Zeit − Rang nach Entfernung, ohne Ursprungsgemeinde "
        "und ohne nicht erreichbare Ziele; ergänzt durch die prozentuale Abweichung von "
        "der erwarteten Zeit laut Regression log(Zeit) ~ log(Entfernung) je Ursprung.",
    )
    log_assumption(
        cfg, "07", "I2_dead_zone",
        f"Zone morte = destination à moins de {cfg['thresholds']['dead_zone_max_km']} km "
        f"et à plus de {cfg['thresholds']['dead_zone_min_min']} min (ou inatteignable).",
        f"Tote Zone = Ziel näher als {cfg['thresholds']['dead_zone_max_km']} km und "
        f"länger als {cfg['thresholds']['dead_zone_min_min']} min (oder unerreichbar).",
        value={k: cfg["thresholds"][k] for k in ("dead_zone_max_km", "dead_zone_min_min")},
    )
    log.info("Écrit : resultats_%s.csv, synthese_%s.json, nuage_%s.png", slug, slug, slug)


if __name__ == "__main__":
    main()
