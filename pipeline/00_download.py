"""
Téléchargement des données brutes du pipeline « So nah, so fern ».

Télécharge chaque source déclarée dans config/sources.yaml vers son `dest`, la
vérifie (zip / GTFS / parquet / osm.pbf) et consigne taille, SHA-256 et en-têtes
HTTP dans data/raw/MANIFEST.json (une entrée par `id`, mise à jour source par
source : les autres entrées sont conservées).

  - écriture dans <dest>.part puis renommage atomique ; reprise (requête Range)
    d'un .part laissé par une exécution interrompue si le serveur l'accepte
    (réponse 206), sinon on repart de zéro ;
  - idempotent : une source dont le fichier correspond au manifeste (URL,
    taille, SHA-256) n'est pas retéléchargée, sauf avec --force ;
  - toutes les sources sélectionnées sont tentées ; code de sortie 1 si l'une
    d'elles a échoué (téléchargement ou vérification).

Script global (pas de --config), à lancer depuis la racine du repo :
    uv run python pipeline/00_download.py                      # tout (~1,5 Go)
    uv run python pipeline/00_download.py --only fr            # une région
    uv run python pipeline/00_download.py --only fr_osm de_gtfs --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import requests
from tqdm import tqdm

from common import ROOT, get_logger, load_sources, osmium_bin

MANIFEST = ROOT / "data" / "raw" / "MANIFEST.json"

TIMEOUT = (30, 120)          # (connexion, lecture) en secondes
TRIES = 3                    # nombre de tentatives par fichier
BACKOFF_S = 5                # pause avant la 2e tentative, doublée ensuite
CHUNK = 1 << 20              # 1 Mio par bloc (écriture et hachage)
MO = 1_000_000
TESTZIP_MAX = 20 * MO        # testzip() relit toute l'archive : trop long au-delà

GTFS_REQUIRED = {"stops.txt", "stop_times.txt", "trips.txt", "routes.txt"}
GTFS_CALENDAR = {"calendar.txt", "calendar_dates.txt"}   # au moins un des deux
# Champs du manifeste conservés tels quels quand le téléchargement est ignoré
META_KEYS = ("final_url", "http_last_modified", "http_etag", "downloaded_at_utc")


# --------------------------------------------------------------------------- #
# Manifeste
# --------------------------------------------------------------------------- #
def read_manifest() -> dict:
    """Relit le manifeste ({} s'il n'existe pas ; un fichier illisible est mis de côté)."""
    if not MANIFEST.exists():
        return {}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = MANIFEST.with_name(MANIFEST.name + ".bak")
        os.replace(MANIFEST, backup)
        get_logger("00_download").warning("MANIFEST.json illisible, copié vers %s", backup.name)
        return {}


def update_manifest(source_id: str, record: dict) -> None:
    """Relit, remplace l'entrée `source_id`, réécrit de façon atomique."""
    manifest = read_manifest()
    manifest[source_id] = record
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_name(MANIFEST.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, MANIFEST)


# --------------------------------------------------------------------------- #
# Téléchargement
# --------------------------------------------------------------------------- #
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def is_up_to_date(dest: Path, entry: dict | None, src: dict) -> bool:
    """Vrai si `dest` correspond au manifeste : même URL, même taille, même SHA-256."""
    if not entry or entry.get("url") != src["url"] or not dest.is_file():
        return False
    if dest.stat().st_size != entry.get("bytes"):    # test bon marché d'abord
        return False
    return sha256_file(dest) == entry.get("sha256")


def fetch(session: requests.Session, src: dict, url: str, part: Path, log) -> dict:
    """Une tentative : télécharge (ou reprend) `url` vers `part` et renvoie les métadonnées HTTP."""
    offset = part.stat().st_size if part.exists() else 0
    for _ in range(2):          # 2e passage seulement si le serveur refuse la reprise (416)
        # identity : pas de compression, pour que les octets reçus = les octets du fichier
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        with session.get(url, headers=headers, stream=True, timeout=TIMEOUT) as r:
            if offset and r.status_code == 416:
                log.warning("%s : reprise refusée (416), on repart de zéro", src["id"])
                part.unlink()
                offset = 0
                continue
            r.raise_for_status()

            # Reprise valable seulement si 206 ET Content-Range commence bien à `offset`
            m = re.match(r"bytes (\d+)-\d+/(\d+|\*)", r.headers.get("Content-Range", ""))
            if offset and r.status_code == 206 and m and int(m.group(1)) == offset:
                mode = "ab"
                total = int(m.group(2)) if m.group(2).isdigit() else None
                log.info("%s : reprise à %.1f Mo", src["id"], offset / MO)
            else:
                if offset:
                    log.info("%s : reprise impossible (HTTP %d), on repart de zéro",
                             src["id"], r.status_code)
                offset, mode = 0, "wb"
                total = int(r.headers["Content-Length"]) if "Content-Length" in r.headers else None

            with open(part, mode) as f, tqdm(
                total=total, initial=offset, desc=src["id"], unit="B", unit_scale=True,
                unit_divisor=1024, mininterval=1.0,
            ) as bar:
                for chunk in r.iter_content(CHUNK):
                    f.write(chunk)
                    bar.update(len(chunk))

            # Connexion coupée proprement mais trop tôt : on garde .part pour la reprise
            size = part.stat().st_size
            if total is not None and size != total:
                raise OSError(f"fichier incomplet : {size} octets reçus sur {total}")
            return {
                "final_url": r.url,
                "http_last_modified": r.headers.get("Last-Modified"),
                "http_etag": r.headers.get("ETag"),
            }
    raise OSError("téléchargement impossible")      # inatteignable en pratique


def download(session: requests.Session, src: dict, dest: Path, force: bool, log) -> dict:
    """Télécharge `src` vers `dest` et renvoie les métadonnées.

    3 tentatives avec backoff par URL : d'abord `url`, puis chaque entrée de
    `mirrors` (copie du même fichier sur un autre serveur) si la précédente échoue.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if force:
        part.unlink(missing_ok=True)        # --force : pas de reprise d'un .part ancien
    urls = [src["url"], *src.get("mirrors", [])]
    for i, url in enumerate(urls):
        if i:
            # Un .part d'un autre serveur ne doit pas être complété par celui ci
            part.unlink(missing_ok=True)
            log.warning("%s : essai du miroir %d/%d %s", src["id"], i, len(urls) - 1, url)
        try:
            meta = fetch_with_retries(session, src, url, part, log)
            break
        except OSError:
            if i == len(urls) - 1:
                raise
    os.replace(part, dest)
    meta["downloaded_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return meta


def fetch_with_retries(session: requests.Session, src: dict, url: str, part: Path, log) -> dict:
    """Jusqu'à TRIES tentatives sur `url` ; lève la dernière erreur."""
    for attempt in range(1, TRIES + 1):
        try:
            return fetch(session, src, url, part, log)
        except OSError as e:        # requests.RequestException hérite d'OSError
            # Une erreur 4xx (hors 408/429) ne passera pas en réessayant
            status = e.response.status_code if isinstance(e, requests.HTTPError) \
                and e.response is not None else None
            definitive = status is not None and 400 <= status < 500 and status not in (408, 429)
            if definitive or attempt == TRIES:
                log.error("%s : %s en échec (%s)", src["id"], url, e)
                raise
            wait = BACKOFF_S * 2 ** (attempt - 1)
            log.warning("%s : tentative %d/%d échouée (%s), nouvel essai dans %d s",
                        src["id"], attempt, TRIES, e, wait)
            time.sleep(wait)
    raise OSError("téléchargement impossible")      # inatteignable


# --------------------------------------------------------------------------- #
# Vérification des fichiers
# --------------------------------------------------------------------------- #
def verify(path: Path, source_id: str) -> tuple[str, dict]:
    """Contrôle d'intégrité selon le type de fichier.

    Renvoie (check, extras) : check vaut "ok" ou un message d'erreur ; extras
    (num_rows, gtfs_files, osm_info) est ajouté à l'entrée du manifeste.
    """
    name = path.name.lower()
    try:
        if name.endswith(".zip"):
            return verify_zip(path, source_id)
        if name.endswith(".parquet"):
            return "ok", {"num_rows": pq.read_metadata(path).num_rows}
        if name.endswith(".osm.pbf"):
            return verify_osm(path)
        if name.endswith(".geojson"):
            return verify_geojson(path)
        if name.endswith(".csv"):
            return verify_csv(path)
        return "ok", {}         # type non reconnu : pas de contrôle spécifique
    except Exception as e:      # fichier illisible = échec de la vérification, pas du script
        return f"{type(e).__name__}: {e}", {}


def verify_zip(path: Path, source_id: str) -> tuple[str, dict]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if not names:
            return "archive zip vide", {}
        if path.stat().st_size < TESTZIP_MAX:
            bad = z.testzip()
            if bad:
                return f"entrée zip corrompue : {bad}", {}
    if "gtfs" not in source_id:
        return "ok", {}

    # GTFS : les fichiers indispensables doivent être présents
    txt = [n for n in names if n.lower().endswith(".txt")]
    present = {Path(n).name.lower() for n in txt}
    missing = sorted(GTFS_REQUIRED - present)
    if not present & GTFS_CALENDAR:
        missing.append("calendar.txt ou calendar_dates.txt")
    if missing:
        return "GTFS incomplet, manquant : " + ", ".join(missing), {"gtfs_files": txt}
    return "ok", {"gtfs_files": txt}


def verify_osm(path: Path) -> tuple[str, dict]:
    # fileinfo sans -e ne lit que l'en-tête : instantané même sur un gros .pbf
    res = subprocess.run([osmium_bin(), "fileinfo", "-j", str(path)],
                         capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        return f"osmium fileinfo (code {res.returncode}) : {res.stderr.strip()[:200]}", {}
    header = json.loads(res.stdout).get("header")
    if header is None:
        return "osmium fileinfo : bloc header absent", {}
    return "ok", {"osm_info": header}      # contient le timestamp de réplication s'il existe


def verify_geojson(path: Path) -> tuple[str, dict]:
    # Un WFS peut renvoyer une erreur XML avec un statut 200 : on exige un vrai GeoJSON
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") != "FeatureCollection" or not data.get("features"):
        return "GeoJSON invalide ou sans entités", {}
    return "ok", {"num_rows": len(data["features"])}


def verify_csv(path: Path) -> tuple[str, dict]:
    # Contrôle minimal : en-tête + au moins une ligne de données, pas de HTML d'erreur
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if len(lines) < 2 or lines[0].lstrip().startswith("<"):
        return "CSV vide ou réponse HTML", {}
    return "ok", {"num_rows": len(lines) - 1, "header": lines[0]}


# --------------------------------------------------------------------------- #
# Traitement d'une source
# --------------------------------------------------------------------------- #
def process(session: requests.Session, src: dict, force: bool, log) -> dict:
    """Télécharge (ou saute), vérifie, met à jour le manifeste ; renvoie la ligne de résumé."""
    sid = src["id"]
    dest = ROOT / src["dest"]
    entry = read_manifest().get(sid)

    if not force and is_up_to_date(dest, entry, src):
        log.info("%s : déjà présent et conforme au manifeste, téléchargement ignoré", sid)
        meta = {k: entry.get(k) for k in META_KEYS}
        digest = entry["sha256"]
    else:
        meta = download(session, src, dest, force, log)
        log.info("%s : calcul du SHA-256", sid)
        digest = sha256_file(dest)

    # La vérification est refaite même si le téléchargement est ignoré (peu coûteuse)
    check, extras = verify(dest, sid)
    size = dest.stat().st_size
    update_manifest(sid, {
        "url": src["url"],
        "final_url": meta["final_url"],
        "dest": src["dest"],
        "bytes": size,
        "sha256": digest,
        "http_last_modified": meta["http_last_modified"],
        "http_etag": meta["http_etag"],
        "downloaded_at_utc": meta["downloaded_at_utc"],
        "licence": src.get("licence"),
        "attribution": src.get("attribution"),
        "check": check,
        **extras,
    })
    if check != "ok":
        log.error("%s : vérification échouée — %s", sid, check)
    return {"id": sid, "bytes": size, "sha256": digest, "check": check}


def select_sources(sources: dict, only: list[str] | None, parser) -> list[dict]:
    """Applique --only (régions ou ids) ; l'ordre est celui de sources.yaml."""
    if not only:
        return list(sources.values())
    regions = {s["region"] for s in sources.values()}
    unknown = [t for t in only if t not in regions and t not in sources]
    if unknown:
        parser.error(f"--only : valeur inconnue {unknown} "
                     f"(régions : {sorted(regions)} ; ids : {sorted(sources)})")
    return [s for s in sources.values() if s["id"] in only or s["region"] in only]


def log_summary(log, rows: list[dict]) -> None:
    """Tableau final : id, taille en Mo, début du SHA-256, résultat du contrôle."""
    w = max(len(r["id"]) for r in rows)
    log.info("Résumé")
    log.info("%-*s  %10s  %-12s  %s", w, "id", "Mo", "sha256", "check")
    for r in rows:
        mo = f"{r['bytes'] / MO:.3f}" if r["bytes"] is not None else "-"
        sha = r["sha256"][:12] if r["sha256"] else "-"
        log.info("%-*s  %10s  %-12s  %s", w, r["id"], mo, sha, r["check"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Télécharge les sources de config/sources.yaml.")
    parser.add_argument("--only", nargs="+", metavar="fr|de|ID",
                        help="région(s) ou id(s) de source à traiter (défaut : toutes)")
    parser.add_argument("--force", action="store_true",
                        help="retélécharge même si le fichier correspond au manifeste")
    args = parser.parse_args()

    log = get_logger("00_download")
    selected = select_sources(load_sources(), args.only, parser)
    log.info("%d source(s) à traiter", len(selected))

    session = requests.Session()
    session.headers["User-Agent"] = "so_nah_so_fern-pipeline/1.0 (python-requests)"
    rows = []
    for src in selected:        # une source en échec n'arrête pas les suivantes
        try:
            rows.append(process(session, src, args.force, log))
        except Exception as e:
            log.error("%s : ÉCHEC — %s: %s", src["id"], type(e).__name__, e)
            rows.append({"id": src["id"], "bytes": None, "sha256": None,
                         "check": f"ÉCHEC : {type(e).__name__}: {e}"})

    log_summary(log, rows)
    failed = [r["id"] for r in rows if r["check"] != "ok"]
    if failed:
        log.error("%d source(s) en échec : %s", len(failed), ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
