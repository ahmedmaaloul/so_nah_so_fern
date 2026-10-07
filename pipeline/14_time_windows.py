"""
Étape 14 — Synthèse des sensibilités temporelles (plage horaire, jour).

    uv run python pipeline/14_time_windows.py                 # plages horaires (S2)
    uv run python pipeline/14_time_windows.py --kind days     # autres jours (S3)

Rassemble, pour chaque origine (config/<slug>.yaml) et chaque variante de
config/sensitivity/ qui ne change QUE la fenêtre de départ (analysis.window_start /
window_end ; --kind windows) ou QUE la date (analysis.date ; --kind days), les chiffres
de synthèse de 07, le nombre de trajets actifs ce jour-là dans le GTFS découpé (02,
qa_gtfs_service_by_date.csv) et la comparaison de 11 avec la référence. Script global.

Sorties (outputs/time_windows/ ou outputs/days/) :
  <kind>.csv     une ligne par (origine, point, variante)
  <kind>.json    idem + destinations dont le temps augmente le plus
"""
from __future__ import annotations

import argparse
import json

import pandas as pd
import yaml

from common import CONFIG_DIR, ROOT, get_logger, load_config, log_assumption, outputs_dir

LOG = get_logger("14_time_windows")
ORIGINS = ["garches", "kronberg", "bad_soden"]
KEYS = ["t_tc_median_min", "n_unreachable", "v_eff_popweighted_median_kmh", "spearman_dist_time",
        "median_transfers", "share_2plus_transfers", "ratio_tc_car_median"]
N_WORST = 5
KINDS = {
    "windows": {"keys": {"window_start", "window_end"}, "dir": "time_windows", "code": "S2_time_windows",
                "fr": "Sensibilité à la plage horaire (même jour, seule la fenêtre de départ change)",
                "de": "Zeitfenster-Sensitivität (gleicher Tag, nur das Abfahrtsfenster ändert sich)"},
    "days": {"keys": {"date"}, "dir": "days", "code": "S3_days",
             "fr": "Sensibilité au jour (même fenêtre, seule la date change)",
             "de": "Tages-Sensitivität (gleiches Zeitfenster, nur das Datum ändert sich)"},
}


def variants(slug: str, keys: set) -> list:
    """Fichiers de variante de `slug` qui ne changent que analysis.<keys>."""
    out = []
    for p in sorted((CONFIG_DIR / "sensitivity").glob(f"{slug}_*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        changed = {k for k in raw if k not in ("extends", "run_tag")}
        if changed == {"analysis"} and set(raw["analysis"]) <= keys:
            out.append(p)
    return out


def trips_on(ref: dict, date: str):
    """Trajets actifs le jour `date` dans le GTFS découpé de l'origine (contrôle de 02)."""
    path = outputs_dir(ref) / "qa_gtfs_service_by_date.csv"
    if not path.exists():
        return None
    d = pd.read_csv(path, dtype={"date": str}).set_index("date")
    return int(d.loc[date, "trips"]) if date in d.index else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthèse des sensibilités temporelles.")
    parser.add_argument("--kind", choices=sorted(KINDS), default="windows")
    kind = KINDS[parser.parse_args().kind]
    rows, worst = [], {}
    for slug in ORIGINS:
        ref = load_config(CONFIG_DIR / f"{slug}.yaml")
        runs = [("reference", ref)] + [(load_config(p)["run_tag"], load_config(p)) for p in variants(slug, kind["keys"])]
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
                day = str(cfg["analysis"]["date"])
                rows.append({"origin": slug, "point": oid, "date": day,
                             "weekday": pd.Timestamp(day).day_name(), "trips_day": trips_on(ref, day),
                             "window": "–".join(syn["window"]), "tag": tag,
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
    out = ROOT / "outputs" / kind["dir"]
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / f"{kind['dir']}.csv", index=False, float_format="%.3f")
    (out / f"{kind['dir']}.json").write_text(json.dumps({"rows": df.to_dict("records"), "largest_increases": worst},
                                                      ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    show = df[df["point"] == "gare"][["origin", "tag", "date", "weekday", "trips_day", "window", "t_tc_median_min", "n_unreachable",
                                      "v_eff_popweighted_median_kmh", "share_2plus_transfers", "ratio_tc_car_median",
                                      "dead_zones", "delta_median_min", "spearman_vs_reference"]]
    LOG.info("Depuis la gare :\n%s", show.round(3).to_string(index=False))
    LOG.info("Écrit : %s", out)

    # Hypothèse S2 / S3 dans le journal de chaque origine (point gare)
    for slug in ORIGINS:
        v = df[(df["origin"] == slug) & (df["point"] == "gare")]
        if len(v) < 2:
            continue
        lab = (lambda r: r.window) if kind["dir"] == "time_windows" else (lambda r: f"{r.date} ({r.trips_day} trajets/Fahrten)")
        fr = "; ".join(f"{lab(r)} médiane {r.t_tc_median_min:g} min, {r.n_unreachable} non atteintes" for r in v.itertuples())
        de = "; ".join(f"{lab(r)} Median {r.t_tc_median_min:g} Min., {r.n_unreachable} nicht erreicht" for r in v.itertuples())
        log_assumption(
            load_config(CONFIG_DIR / f"{slug}.yaml"), "14", kind["code"],
            f"{kind['fr']} : depuis la gare, {fr}. "
            f"Médianes calculées sur les destinations atteintes seulement. Détail : outputs/{kind['dir']}/.",
            f"{kind['de']}: ab Bahnhof {de}. "
            f"Mediane nur über erreichte Ziele. Details: outputs/{kind['dir']}/.",
            v.drop(columns=["origin"]).to_dict("records"))


if __name__ == "__main__":
    main()
