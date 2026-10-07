"""
Étape 11 — Comparaison d'une variante (analyse de sensibilité) avec le calcul de référence.

    uv run python pipeline/11_sensitivity.py --config config/sensitivity/kronberg_townhall.yaml

La variante est un fichier de config avec `extends:` et `run_tag:` (dossiers
<slug>__<run_tag>) ; la référence est le fichier parent (même origine, sans run_tag).
Compare, pour chaque point d'origine, les résultats de 07 (resultats_*.csv,
synthese_*.json) : écart des temps TC médians par destination, corrélation de rang,
chiffres de synthèse, recouvrement des tops.

Sortie : outputs_dir(variante)/comparaison_reference.json ; hypothèse S1_<run_tag>
dans le journal de la variante.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from common import cli, get_logger, load_config, log_assumption, outputs_dir

LOG = get_logger("11_sensitivity")

SUMMARY_KEYS = ["t_tc_median_min", "v_eff_popweighted_median_kmh", "spearman_dist_time", "median_transfers",
                "share_2plus_transfers", "ratio_tc_car_median"]
CHANGED_MIN = 5          # seuil d'affichage « temps changé de plus de … min » (contrôle)


def load(cfg: dict):
    slug, odir = cfg["origin"]["slug"], outputs_dir(cfg)
    res = pd.read_csv(odir / f"resultats_{slug}.csv", dtype={"unit_id": str})
    syn = {s["origin_id"]: s for s in json.loads((odir / f"synthese_{slug}.json").read_text(encoding="utf-8"))["origins"]}
    return res, syn


def r(x, n=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)


def main() -> None:
    args = cli("Étape 11 : comparaison variante / référence.").parse_args()
    var = load_config(args.config)
    tag = var.get("run_tag")
    if not tag:
        raise SystemExit("Ce fichier n'est pas une variante (run_tag absent).")
    ref = dict(var)
    ref.pop("run_tag")
    res_v, syn_v = load(var)
    res_r, syn_r = load(ref)

    out = {"variant": tag, "origin": var["origin"]["slug"], "config": var["_config_path"], "by_origin": {}}
    for oid in syn_r:
        a = res_r[res_r["origin_id"] == oid].set_index("unit_id")
        b = res_v[res_v["origin_id"] == oid].set_index("unit_id")
        m = a[["name", "t_tc", "top_nah_fern", "top_fern_nah", "dead_zone", "is_origin_commune"]].join(
            b[["t_tc", "top_nah_fern", "top_fern_nah", "dead_zone"]], rsuffix="_v", how="inner")
        m = m[~m["is_origin_commune"]]
        both = m["t_tc"].notna() & m["t_tc_v"].notna()
        d = (m.loc[both, "t_tc_v"] - m.loc[both, "t_tc"])
        top = {}
        for col in ("top_nah_fern", "top_fern_nah"):
            sa, sb = set(m.index[m[col].notna()]), set(m.index[m[col + "_v"].notna()])
            top[col] = {"common": len(sa & sb), "n": len(sa), "out": sorted(m.loc[list(sa - sb), "name"]),
                        "in": sorted(m.loc[list(sb - sa), "name"])}
        big = d[d.abs() > CHANGED_MIN].sort_values(key=abs, ascending=False)
        out["by_origin"][oid] = {
            "n_compared": int(both.sum()),
            "n_reached_ref": int(m["t_tc"].notna().sum()), "n_reached_variant": int(m["t_tc_v"].notna().sum()),
            "delta_t_tc_min": {"median": r(d.median()), "mean": r(d.mean()), "p90_abs": r(d.abs().quantile(0.9)),
                               "max_abs": r(d.abs().max())},
            f"n_changed_gt_{CHANGED_MIN}min": int(len(big)),
            "largest_changes": [{"name": m.loc[i, "name"], "ref": r(m.loc[i, "t_tc"], 1), "variant": r(m.loc[i, "t_tc_v"], 1)}
                                for i in big.index[:8]],
            "spearman_ref_variant": r(m.loc[both, ["t_tc", "t_tc_v"]].corr(method="spearman").iloc[0, 1], 4),
            "summary": {k: {"ref": r(syn_r[oid].get(k), 3), "variant": r(syn_v[oid].get(k), 3)} for k in SUMMARY_KEYS},
            "dead_zones": {"ref": int(m["dead_zone"].sum()), "variant": int(m["dead_zone_v"].fillna(False).astype(bool).sum())},
            "tops": top,
        }
        LOG.info("[%s] Δ médiane %s min, |Δ| p90 %s, max %s ; %d destinations > %d min ; Spearman %s ; tops communs %s",
                 oid, out["by_origin"][oid]["delta_t_tc_min"]["median"], out["by_origin"][oid]["delta_t_tc_min"]["p90_abs"],
                 out["by_origin"][oid]["delta_t_tc_min"]["max_abs"], len(big), CHANGED_MIN,
                 out["by_origin"][oid]["spearman_ref_variant"],
                 {k: f"{v['common']}/{v['n']}" for k, v in top.items()})
    path = outputs_dir(var) / "comparaison_reference.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Écrit : %s", path)

    g = out["by_origin"].get("gare") or next(iter(out["by_origin"].values()))
    log_assumption(
        var, "11", f"S1_{tag}",
        (f"Sensibilité « {tag} » ({var['_config_path']}) comparée à la référence : écart médian des temps TC "
         f"{g['delta_t_tc_min']['median']} min, |écart| p90 {g['delta_t_tc_min']['p90_abs']} min, corrélation de rang "
         f"{g['spearman_ref_variant']} (point gare) ; détail dans comparaison_reference.json."),
        (f"Sensitivität „{tag}“ ({var['_config_path']}) gegenüber der Referenz: Median der Abweichung der ÖV-Zeiten "
         f"{g['delta_t_tc_min']['median']} Min., |Abweichung| P90 {g['delta_t_tc_min']['p90_abs']} Min., "
         f"Rangkorrelation {g['spearman_ref_variant']} (Punkt Bahnhof); Details in comparaison_reference.json."),
        {k: {kk: vv for kk, vv in v.items() if kk in ("delta_t_tc_min", "spearman_ref_variant", "tops")}
         for k, v in out["by_origin"].items()})


if __name__ == "__main__":
    main()
