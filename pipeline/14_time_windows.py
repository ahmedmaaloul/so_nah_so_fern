"""
Étape 14 — Synthèse de la sensibilité à la plage horaire.

    uv run python pipeline/14_time_windows.py

Rassemble, pour chaque origine (config/<slug>.yaml) et chaque variante de
config/sensitivity/ qui ne change QUE la fenêtre de départ (analysis.window_start /
window_end), les chiffres de synthèse de 07 et la comparaison de 11 avec la référence
(fenêtre de config/default.yaml). Script global (pas de --config).

Sorties (outputs/time_windows/) :
  time_windows.csv     une ligne par (origine, point, plage)
  time_windows.json    idem + destinations dont le temps augmente le plus
"""
from __future__ import annotations

import json

import pandas as pd
import yaml

from common import CONFIG_DIR, ROOT, get_logger, load_config, outputs_dir

LOG = get_logger("14_time_windows")
ORIGINS = ["garches", "kronberg", "bad_soden"]
KEYS = ["t_tc_median_min", "n_unreachable", "v_eff_popweighted_median_kmh", "spearman_dist_time",
        "median_transfers", "share_2plus_transfers", "ratio_tc_car_median"]
N_WORST = 5


def window_variants(slug: str) -> list:
    """Fichiers de variante de `slug` dont seul analysis.window_* change."""
    out = []
    for p in sorted((CONFIG_DIR / "sensitivity").glob(f"{slug}_*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        changed = {k for k in raw if k not in ("extends", "run_tag")}
        if changed == {"analysis"} and set(raw["analysis"]) <= {"window_start", "window_end"}:
            out.append(p)
    return out


def main() -> None:
    rows, worst = [], {}
    for slug in ORIGINS:
        ref = load_config(CONFIG_DIR / f"{slug}.yaml")
        runs = [("reference", ref)] + [(load_config(p)["run_tag"], load_config(p)) for p in window_variants(slug)]
        res_ref = pd.read_csv(outputs_dir(ref) / f"resultats_{slug}.csv", dtype={"unit_id": str})
        for tag, cfg in runs:
            odir = outputs_dir(cfg)
            syn_path = odir / f"synthese_{slug}.json"
            if not syn_path.exists():
                LOG.warning("%s %s : synthèse absente, variante ignorée", slug, tag)
                continue
            syn = json.loads(syn_path.read_text(encoding="utf-8"))
            cmp_path = odir / "comparaison_reference.json"
            cmp = json.loads(cmp_path.read_text(encoding="utf-8"))["by_origin"] if cmp_path.exists() else {}
            res = pd.read_csv(odir / f"resultats_{slug}.csv", dtype={"unit_id": str})
            for s in syn["origins"]:
                oid = s["origin_id"]
                c = cmp.get(oid, {})
                rows.append({"origin": slug, "point": oid, "window": "–".join(syn["window"]), "tag": tag,
                             **{k: s.get(k) for k in KEYS}, "dead_zones": s["dead_zones"]["n"],
                             "delta_median_min": (c.get("delta_t_tc_min") or {}).get("median"),
                             "delta_p90_abs_min": (c.get("delta_t_tc_min") or {}).get("p90_abs"),
                             "spearman_vs_reference": c.get("spearman_ref_variant")})
                if tag != "reference":
                    a = res_ref[(res_ref.origin_id == oid) & ~res_ref.is_origin_commune].set_index("unit_id")
                    b = res[(res.origin_id == oid) & ~res.is_origin_commune].set_index("unit_id")
                    d = (b["t_tc"] - a["t_tc"]).dropna().sort_values(ascending=False).head(N_WORST)
                    worst[f"{slug}/{oid}/{tag}"] = [{"name": a.loc[u, "name"], "dist_km": round(float(a.loc[u, "dist_km"]), 1),
                                                     "reference": float(a.loc[u, "t_tc"]), "variant": float(b.loc[u, "t_tc"])}
                                                    for u in d.index]
    df = pd.DataFrame(rows)
    out = ROOT / "outputs" / "time_windows"
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "time_windows.csv", index=False, float_format="%.3f")
    (out / "time_windows.json").write_text(json.dumps({"rows": df.to_dict("records"), "largest_increases": worst},
                                                      ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    show = df[df["point"] == "gare"][["origin", "window", "t_tc_median_min", "n_unreachable",
                                      "v_eff_popweighted_median_kmh", "share_2plus_transfers", "ratio_tc_car_median",
                                      "dead_zones", "delta_median_min", "spearman_vs_reference"]]
    LOG.info("Depuis la gare :\n%s", show.round(3).to_string(index=False))
    LOG.info("Écrit : %s", out)


if __name__ == "__main__":
    main()
