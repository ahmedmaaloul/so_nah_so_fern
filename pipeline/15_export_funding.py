"""
Étape 15 : export des montants publics pour les transports en commun (site web).

    uv run python pipeline/15_export_funding.py

Lit config/funding.yaml (montants relevés à la main, avec source), contrôle les champs,
calcule les euros par habitant (si `per_capita`) et par an (si `years`), additionne les
grands projets ferroviaires par région (seule grandeur retenue comme comparable) et écrit
web/data/funding.json. Met à jour web/data/index.json (clé « funding »). Script global,
sans dépendance aux données du pipeline.
"""
from __future__ import annotations

import json

import yaml

from common import CONFIG_DIR, ROOT, get_logger

LOG = get_logger("15_export_funding")
WEB_DATA = ROOT / "web" / "data"
GROUPS = ("invest", "projects", "operating")
REQUIRED = ("id", "group", "amount_meur", "label_de", "label_fr", "note_de", "note_fr", "source")


def check(region: dict) -> None:
    if not region.get("population") or not region.get("population_source"):
        raise SystemExit(f"{region['id']} : population ou source manquante.")
    for it in region["items"]:
        miss = [k for k in REQUIRED if it.get(k) in (None, "")]
        if miss:
            raise SystemExit(f"{region['id']}/{it.get('id')} : champs manquants {miss}.")
        if it["group"] not in GROUPS:
            raise SystemExit(f"{region['id']}/{it['id']} : groupe inconnu {it['group']}.")
        if not it["source"].get("url", "").startswith("https://"):
            raise SystemExit(f"{region['id']}/{it['id']} : URL de source manquante.")


def main() -> None:
    cfg = yaml.safe_load((CONFIG_DIR / "funding.yaml").read_text(encoding="utf-8"))
    regions = []
    for r in cfg["regions"]:
        check(r)
        pop = r["population"]
        items = []
        for it in r["items"]:
            out = {k: v for k, v in it.items()}
            if it.get("years"):
                out["per_year_meur"] = round(it["amount_meur"] / it["years"], 1)
            if it.get("per_capita"):
                out["eur_per_capita"] = round(it["amount_meur"] * 1e6 / pop)
            items.append(out)
        proj = [i for i in items if i["group"] == "projects"]
        total = sum(i["amount_meur"] for i in proj)
        regions.append({
            **{k: r[k] for k in ("id", "population", "population_label_de", "population_label_fr", "population_source")},
            "population_approx": bool(r.get("population_approx")),
            "items": items,
            "projects_total": {"amount_meur": round(total, 1), "eur_per_capita": round(total * 1e6 / pop),
                               "lower_bound": any(i.get("lower_bound") for i in proj),
                               "price_base": sorted({i["price_base"] for i in proj if i.get("price_base")}),
                               "ids": [i["id"] for i in proj]},
        })
        LOG.info("%s : grands projets %.0f M€, %d €/hab.", r["id"], total, regions[-1]["projects_total"]["eur_per_capita"])

    data = {"accessed": cfg["accessed"], "regions": regions}
    WEB_DATA.mkdir(parents=True, exist_ok=True)
    out = WEB_DATA / "funding.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    LOG.info("Écrit : %s", out)

    idx_path = WEB_DATA / "index.json"
    index = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {"origins": []}
    index["funding"] = out.name
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
