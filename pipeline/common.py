"""
Fonctions communes à tous les scripts du pipeline « So nah, so fern ».

Contrat (tous les scripts numérotés le respectent) :
  - ils se lancent depuis la racine du repo :
        uv run python pipeline/NN_xxx.py --config config/garches.yaml
  - ils lisent leurs paramètres UNIQUEMENT via load_config() — aucune valeur
    métier (date, horaire, rayon, seuil, code commune) n'est écrite en dur ;
  - ils écrivent dans les dossiers donnés par interim_dir / processed_dir /
    outputs_dir, jamais ailleurs ;
  - ils journalisent les hypothèses avec log_assumption(), qui alimente
    outputs/<origine>/assumptions.jsonl (repris dans la note méthodologique).
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict, override: dict) -> dict:
    """Fusion récursive : les valeurs de `override` remplacent celles de `base`."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(origin_cfg: str | Path) -> dict:
    """Charge default.yaml, y superpose le fichier d'origine et ajoute les sources.

    Le dict renvoyé contient en plus :
      cfg["sources"]      -> {id: entrée de sources.yaml}
      cfg["_config_path"] -> chemin du fichier d'origine
    """
    origin_cfg = Path(origin_cfg)
    if not origin_cfg.is_absolute():
        origin_cfg = ROOT / origin_cfg
    with open(CONFIG_DIR / "default.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    with open(origin_cfg, encoding="utf-8") as f:
        cfg = _deep_merge(cfg, yaml.safe_load(f))
    cfg["sources"] = load_sources()
    # Chemin relatif au repo si possible (lisible dans les sorties), sinon absolu
    try:
        cfg["_config_path"] = str(origin_cfg.relative_to(ROOT))
    except ValueError:
        cfg["_config_path"] = str(origin_cfg)
    return cfg


def load_sources() -> dict:
    with open(CONFIG_DIR / "sources.yaml", encoding="utf-8") as f:
        entries = yaml.safe_load(f)["sources"]
    return {e["id"]: e for e in entries}


def source_path(cfg: dict, source_id: str) -> Path:
    """Chemin local d'une source déclarée dans config/sources.yaml."""
    return ROOT / cfg["sources"][source_id]["dest"]


# --------------------------------------------------------------------------- #
# Dossiers de travail
# --------------------------------------------------------------------------- #
def interim_dir(cfg: dict) -> Path:
    """Extraits découpés (OSM, GTFS) propres à une origine."""
    return _mkdir(ROOT / "data" / "interim" / cfg["origin"]["slug"])


def processed_dir(cfg: dict) -> Path:
    """Tables intermédiaires calculées (parquet/geoparquet)."""
    return _mkdir(ROOT / "data" / "processed" / cfg["origin"]["slug"])


def outputs_dir(cfg: dict) -> Path:
    """Livrables publiables (CSV, figures, journal des hypothèses)."""
    return _mkdir(ROOT / "outputs" / cfg["origin"]["slug"])


def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- #
# Outils externes
# --------------------------------------------------------------------------- #
def osmium_bin() -> str:
    """osmium-tool : copie locale (.tools/, installée via conda-forge) sinon PATH."""
    local = ROOT / ".tools" / "osmium" / "bin" / "osmium"
    if local.exists():
        return str(local)
    found = shutil.which("osmium")
    if not found:
        raise RuntimeError("osmium-tool introuvable (voir README, section Installation).")
    return found


def configure_jvm(cfg: dict) -> None:
    """À appeler AVANT `import r5py` : mémoire et nombre de cœurs de la JVM.

    - mémoire : r5py lit `--max-memory` dans sys.argv (ConfigArgParse) ; on
      remplace sys.argv pour qu'aucun de nos arguments (--config) ne lui
      parvienne ;
    - cœurs : R5 dimensionne ses pools de threads sur availableProcessors(),
      que l'option JVM -XX:ActiveProcessorCount plafonne (lue au démarrage via
      JAVA_TOOL_OPTIONS, y compris pour la JVM embarquée par jpype). Moins de
      cœurs = calcul plus long mais machine moins chaude.
    """
    import sys
    routing = cfg["routing"]
    sys.argv = [sys.argv[0], "--max-memory", str(routing["jvm_max_memory"])]
    n = routing.get("jvm_active_processors")
    if n:
        opts = os.environ.get("JAVA_TOOL_OPTIONS", "")
        if "ActiveProcessorCount" not in opts:
            os.environ["JAVA_TOOL_OPTIONS"] = f"{opts} -XX:ActiveProcessorCount={int(n)}".strip()


# --------------------------------------------------------------------------- #
# CLI, journalisation, hypothèses
# --------------------------------------------------------------------------- #
def cli(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", required=True,
                        help="fichier d'origine, ex. config/garches.yaml")
    return parser


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(
        level=os.environ.get("LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger(name)


def log_assumption(cfg: dict, step: str, code: str, text_fr: str, text_de: str,
                   value=None) -> None:
    """Consigne une hypothèse ou un écart (ex. date de repli) pour la note méthodo.

    `code` est un identifiant stable (ex. "H3_walk_speed") : une nouvelle
    exécution remplace l'entrée de même code au lieu de la dupliquer.
    """
    path = outputs_dir(cfg) / "assumptions.jsonl"
    entries = []
    if path.exists():
        entries = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    entries = [e for e in entries if e.get("code") != code]
    entries.append({
        "code": code, "step": step, "fr": text_fr, "de": text_de, "value": value,
        "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
                    encoding="utf-8")
