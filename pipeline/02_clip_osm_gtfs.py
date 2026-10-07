"""
Étape 02 — Découpe OSM et GTFS autour de la zone d'analyse et contrôle du jour.

Réduit les gros fichiers sources (pbf OSM régional, GTFS régional / national) à
la seule zone utile au routage r5py, puis vérifie que le jour d'analyse est un
jour de service « normal » du GTFS découpé.

    uv run python pipeline/02_clip_osm_gtfs.py --config config/garches.yaml
        [--clip-area chemin.geojson] [--allow-low-service] [--skip-osm]

Le script ne contient aucune valeur d'analyse : date, fenêtre horaire, tampon,
seuil de service, vacances et fériés viennent de la configuration. Seules figurent
en tête de fichier des constantes de FORMAT (noms de fichiers GTFS standards) et
la convention « grandes lignes » (types de routes et préfixes de noms), qui peut
être surchargée par routing.long_distance_route_types /
routing.long_distance_name_prefixes dans la configuration.

Entrées (contrat de l'étape 01), dans processed_dir(cfg) :
  destinations.parquet   GeoParquet EPSG:4326, colonnes in_radius, in_core_radius
  origins.geojson        un point par origine
  (inutiles si --clip-area fournit la zone)
et les sources cfg["inputs"]["osm"] (.osm.pbf) / cfg["inputs"]["gtfs"] (.zip).

Sorties, dans interim_dir(cfg) :
  clip_area.geojson        zone de découpe (EPSG:4326)
  osm_clip.osm.pbf         extrait OSM (fusion des extraits si plusieurs pbf)
  gtfs/<id_source>.zip     GTFS découpé, fichiers standards uniquement
et dans outputs_dir(cfg) :
  qa_gtfs_service_by_date.csv         trajets actifs par date du feed
  qa_gtfs_routes_missing.csv          lignes habituelles sans trajet le jour d'analyse (D2)
  qa_gtfs_long_distance_excluded.csv  routes grandes lignes écartées
  assumptions.jsonl                   D1_analysis_date, G1_long_distance_excluded,
                                      G2_gtfs_clip, G3_osm_clip
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from urllib.parse import urlparse

from common import (ROOT, cli, get_logger, interim_dir, load_config, log_assumption,
                    osmium_bin, outputs_dir, processed_dir, source_path)

LOG = get_logger("02_clip_osm_gtfs")

# --------------------------------------------------------------------------- #
# Constantes de format et conventions (PAS des paramètres d'analyse)
# --------------------------------------------------------------------------- #
WGS84 = "EPSG:4326"

DEFAULT_MAX_MISSING_ROUTE_SHARE = 0.01   # analysis.max_missing_route_share (contrôle D2)

# Fichiers GTFS écrits dans l'extrait, dans cet ordre. Volontairement PAS de
# pathways, transfers, booking_rules ni extensions (object_codes, ticketing…) :
# r5py n'en a pas besoin et certaines extensions peuvent le gêner.
GTFS_OUT_FILES = ["agency", "stops", "routes", "trips", "stop_times",
                  "calendar", "calendar_dates", "shapes", "feed_info"]

CHUNK_ROWS = 1_000_000        # lignes lues par bloc dans les gros fichiers (stop_times, shapes)
WRITE_ROWS = 500_000          # lignes écrites par bloc dans stop_times.txt

# Convention « grandes lignes » (trains non utilisables avec un abonnement local).
# Surchargeable via cfg["routing"]["long_distance_route_types"] / ["long_distance_name_prefixes"].
LONG_DISTANCE_ROUTE_TYPES = ["101", "102"]     # types GTFS étendus : grande vitesse / longue distance
LONG_DISTANCE_PREFIXES = ["ICE", "IC", "EC", "ECE", "EN", "NJ", "RJ", "RJX", "FLX", "TGV"]


# --------------------------------------------------------------------------- #
# Petits outils
# --------------------------------------------------------------------------- #
def human(n_bytes: float) -> str:
    """Taille lisible (Mo / Go)."""
    if n_bytes >= 1e9:
        return f"{n_bytes / 1e9:.2f} Go"
    return f"{n_bytes / 1e6:.1f} Mo"


def to_day(value) -> pd.Timestamp:
    """Date YAML (datetime.date ou texte) -> Timestamp à minuit."""
    return pd.Timestamp(value).normalize()


def hms_to_sec(text: str) -> float:
    """« HH:MM » ou « HH:MM:SS » -> secondes depuis minuit (heures > 24 permises)."""
    parts = [int(p) for p in str(text).strip().split(":")]
    parts += [0] * (3 - len(parts))
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def hms_series_to_sec(series: pd.Series) -> np.ndarray:
    """Colonne GTFS d'heures (texte, heures > 24 possibles) -> secondes ; NaN si vide."""
    ex = series.str.extract(r"^\s*(\d+):(\d+):(\d+)\s*$")
    sec = (pd.to_numeric(ex[0], errors="coerce") * 3600
           + pd.to_numeric(ex[1], errors="coerce") * 60
           + pd.to_numeric(ex[2], errors="coerce"))
    return sec.to_numpy(dtype="float64")


def find_member(zf: zipfile.ZipFile, name: str) -> str | None:
    """Nom du membre `<name>.txt` dans le zip (à la racine ou dans un sous-dossier)."""
    target = f"{name}.txt"
    for member in zf.namelist():
        if member == target or member.endswith("/" + target):
            return member
    return None


def iter_chunks(zf: zipfile.ZipFile, name: str, usecols=None, chunksize=CHUNK_ROWS):
    """Lit un fichier GTFS par blocs, tout en texte (dtype=str) : rien n'est altéré
    (zéros initiaux, heures > 24:00) et les champs vides restent des chaînes vides."""
    member = find_member(zf, name)
    if member is None:
        return
    with zf.open(member) as fh:
        for chunk in pd.read_csv(fh, dtype=str, keep_default_na=False, na_filter=False,
                                 usecols=usecols, chunksize=chunksize, encoding="utf-8-sig"):
            chunk.columns = chunk.columns.str.strip()
            yield chunk


def read_table(zf: zipfile.ZipFile, name: str) -> pd.DataFrame | None:
    """Lit un petit fichier GTFS en entier (None s'il est absent)."""
    member = find_member(zf, name)
    if member is None:
        return None
    with zf.open(member) as fh:
        df = pd.read_csv(fh, dtype=str, keep_default_na=False, na_filter=False,
                         encoding="utf-8-sig")
    df.columns = df.columns.str.strip()
    return df


def write_table(zout: zipfile.ZipFile, name: str, df: pd.DataFrame, chunk_rows=WRITE_ROWS,
                order=None) -> None:
    """Écrit `<name>.txt` dans le zip (CSV UTF-8, en-tête, LF), par blocs.
    `order` : permutation optionnelle des lignes (tri sans copie complète)."""
    n = len(df)
    with zout.open(f"{name}.txt", "w", force_zip64=True) as out:
        if n == 0:
            out.write((",".join(df.columns) + "\n").encode("utf-8"))
            return
        for i in range(0, n, chunk_rows):
            block = df.iloc[order[i:i + chunk_rows]] if order is not None else df.iloc[i:i + chunk_rows]
            out.write(block.to_csv(index=False, header=(i == 0), lineterminator="\n").encode("utf-8"))


# --------------------------------------------------------------------------- #
# 1. Zone de découpe
# --------------------------------------------------------------------------- #
def build_clip_area(cfg: dict, clip_area_arg: str | None, out_path: Path):
    """Zone de découpe en EPSG:4326 : enveloppe convexe de l'union des tampons
    (analysis.clip_buffer_km) autour des destinations in_radius | in_core_radius
    et des origines — ou zone imposée par --clip-area (tests)."""
    if clip_area_arg:
        gdf = gpd.read_file(clip_area_arg)
        if gdf.crs is None:
            gdf = gdf.set_crs(WGS84)
        zone = gdf.to_crs(WGS84).geometry.union_all()
        metric_area = gpd.GeoSeries([zone], crs=WGS84).to_crs(cfg["region"]["metric_crs"]).iloc[0]
        info = {"mode": "clip_area_fournie", "source": str(clip_area_arg),
                "area_km2": metric_area.area / 1e6}
        LOG.info("Zone de découpe fournie (--clip-area %s) : %.0f km²", clip_area_arg, info["area_km2"])
    else:
        metric_crs = cfg["region"]["metric_crs"]
        buffer_km = float(cfg["analysis"]["clip_buffer_km"])
        dest = gpd.read_parquet(processed_dir(cfg) / "destinations.parquet")
        sel = dest["in_radius"].fillna(False).astype(bool) | dest["in_core_radius"].fillna(False).astype(bool)
        if not sel.any():
            raise SystemExit("ERREUR : aucune destination in_radius / in_core_radius dans "
                             "destinations.parquet — lancer l'étape 01 d'abord.")
        origins = gpd.read_file(processed_dir(cfg) / "origins.geojson")
        pts = pd.concat([dest.loc[sel, "geometry"].to_crs(metric_crs),
                         origins.geometry.to_crs(metric_crs)], ignore_index=True)
        pts = gpd.GeoSeries(pts, crs=metric_crs)
        hull = pts.buffer(buffer_km * 1000).union_all().convex_hull
        zone = gpd.GeoSeries([hull], crs=metric_crs).to_crs(WGS84).iloc[0]
        info = {"mode": "tampon_enveloppe_convexe", "buffer_km": buffer_km,
                "n_destinations": int(sel.sum()), "n_origins": int(len(origins)),
                "area_km2": hull.area / 1e6}
        LOG.info("Zone de découpe : enveloppe convexe des tampons de %.0f km autour de %d destinations "
                 "et %d origines — %.0f km²", buffer_km, info["n_destinations"], info["n_origins"],
                 info["area_km2"])
    gpd.GeoDataFrame({"name": ["clip_area"]}, geometry=[zone], crs=WGS84).to_file(
        out_path, driver="GeoJSON")
    info["bounds_wgs84"] = [round(b, 4) for b in zone.bounds]
    LOG.info("clip_area.geojson écrit (emprise lon/lat %s)", info["bounds_wgs84"])
    return zone, info


# --------------------------------------------------------------------------- #
# 2. OSM
# --------------------------------------------------------------------------- #
def run_cmd(cmd: list[str]) -> None:
    """Lance une commande externe ; en cas d'échec, remonte sa sortie d'erreur."""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"ERREUR : commande en échec ({' '.join(cmd)})\n{res.stderr or res.stdout}")


def osm_fileinfo(path: Path) -> dict:
    """Taille et nombre d'objets d'un pbf (osmium fileinfo -e -j)."""
    res = subprocess.run([osmium_bin(), "fileinfo", "-e", "-j", str(path)],
                         capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)["data"]
    return {"size_bytes": path.stat().st_size,
            "nodes": data["count"]["nodes"], "ways": data["count"]["ways"],
            "relations": data["count"]["relations"],
            "objects_ordered": data.get("objects_ordered"),
            "multiple_versions": data.get("multiple_versions")}


def log_osm_info(label: str, info: dict) -> None:
    LOG.info("%s : %s — %s nœuds, %s ways, %s relations (trié : %s, versions multiples : %s)",
             label, human(info["size_bytes"]), f"{info['nodes']:,}", f"{info['ways']:,}",
             f"{info['relations']:,}", info["objects_ordered"], info["multiple_versions"])


def log_mirrors(cfg: dict) -> None:
    """Consigne les sources OSM/GTFS téléchargées depuis un miroir (MANIFEST de 00)."""
    path = ROOT / "data" / "raw" / "MANIFEST.json"
    if not path.exists():
        return
    manifest = json.loads(path.read_text(encoding="utf-8"))
    used = {}
    for sid in [*cfg["inputs"]["osm"], *cfg["inputs"]["gtfs"]]:
        e = manifest.get(sid) or {}
        url, final = e.get("url"), e.get("final_url")
        if url and final and urlparse(url).hostname != urlparse(final).hostname:
            used[sid] = {"url": url, "final_url": final}
    if not used:
        return
    for sid, v in used.items():
        LOG.warning("%s téléchargé depuis un miroir : %s", sid, v["final_url"])
    log_assumption(
        cfg, "02", "G0_mirror",
        "Source(s) téléchargée(s) depuis un miroir, l'URL principale étant injoignable : "
        + " ; ".join(f"{k} ← {urlparse(v['final_url']).hostname}" for k, v in used.items())
        + ". Mêmes données OSM ; le polygone d'extrait peut différer, sans effet après découpe "
        "tant que la zone de découpe reste dans l'extrait.",
        "Quelle(n) von einem Spiegel geladen, da die Haupt-URL nicht erreichbar war: "
        + "; ".join(f"{k} ← {urlparse(v['final_url']).hostname}" for k, v in used.items())
        + ". Gleiche OSM-Daten; das Auszugspolygon kann abweichen, ohne Folgen nach dem "
        "Zuschnitt, solange das Zuschnittsgebiet im Auszug liegt.",
        value=used)


def clip_osm(cfg: dict, clip_path: Path, interim: Path) -> dict:
    """Extrait chaque pbf OSM selon la zone (stratégie complete_ways), fusionne si besoin."""
    final = interim / "osm_clip.osm.pbf"
    parts, parts_info = [], {}
    for sid in cfg["inputs"]["osm"]:
        src = source_path(cfg, sid)
        if not src.exists():
            raise SystemExit(f"ERREUR : source OSM absente : {src} (lancer l'étape 00).")
        out = interim / f"osm_part_{sid}.osm.pbf"
        t0 = time.perf_counter()
        LOG.info("OSM %s : extraction (%s, %s)…", sid, src.name, human(src.stat().st_size))
        run_cmd([osmium_bin(), "extract", "-p", str(clip_path), "-s", "complete_ways",
                 "--set-bounds", "--overwrite", "-o", str(out), str(src)])
        info = osm_fileinfo(out)
        log_osm_info(f"  extrait {sid} ({time.perf_counter() - t0:.0f} s)", info)
        if info["ways"] == 0:
            LOG.warning("  l'extrait %s ne contient aucun way : la zone ne touche pas cette source.", sid)
        parts.append(out)
        parts_info[sid] = info

    if len(parts) == 1:
        parts[0].replace(final)                       # un seul pbf : on renomme
    else:
        LOG.info("OSM : fusion de %d extraits…", len(parts))
        run_cmd([osmium_bin(), "merge", *map(str, parts), "--overwrite", "-o", str(final)])
        for p in parts:                               # extraits intermédiaires devenus inutiles
            p.unlink()
    info = osm_fileinfo(final)
    log_osm_info("osm_clip.osm.pbf", info)
    if info["multiple_versions"]:
        LOG.warning("osm_clip.osm.pbf contient plusieurs versions d'un même objet (extraits de "
                    "dates différentes ?) — à examiner avant le routage.")
    log_assumption(
        cfg, "02", "G3_osm_clip",
        f"OSM découpé avec osmium extract (polygone = zone de découpe, stratégie complete_ways) ; "
        f"extraits fusionnés si plusieurs sources. Résultat : {info['nodes']:,} nœuds, "
        f"{info['ways']:,} ways, {info['relations']:,} relations, {human(info['size_bytes'])}.",
        f"OSM mit osmium extract zugeschnitten (Polygon = Zuschnittsgebiet, Strategie complete_ways); "
        f"bei mehreren Quellen werden die Auszüge zusammengeführt. Ergebnis: {info['nodes']:,} Knoten, "
        f"{info['ways']:,} Ways, {info['relations']:,} Relationen, {human(info['size_bytes'])}.",
        value={"sources": list(cfg["inputs"]["osm"]), "parts": parts_info, "final": info})
    return {"parts": parts_info, "final": info}


# --------------------------------------------------------------------------- #
# 3. GTFS
# --------------------------------------------------------------------------- #
def long_distance_matcher(prefixes: list[str]):
    """Regex « mot entier » : le nom commence par un préfixe, qui n'est pas suivi d'une
    lettre (ICE 1, IC2, FLX10, RJX 60 correspondent ; ICE ne correspond pas à « IC »
    mais à « ICE » ; « Bus ICEBERG » non), insensible à la casse."""
    alt = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    return re.compile(rf"^\s*(?:{alt})(?![^\W\d_])", re.IGNORECASE)


def clip_gtfs(cfg: dict, source_id: str, zone, out_dir: Path) -> dict:
    """Découpe un GTFS selon la zone ; renvoie bilan + tables utiles au contrôle du jour."""
    t_start = time.perf_counter()
    src = source_path(cfg, source_id)
    if not src.exists():
        raise SystemExit(f"ERREUR : source GTFS absente : {src} (lancer l'étape 00).")
    out_path = out_dir / f"{source_id}.zip"
    LOG.info("GTFS %s : %s (%s)", source_id, src.name, human(src.stat().st_size))
    zf = zipfile.ZipFile(src)
    dropped = [m for m in zf.namelist()
               if Path(m).stem not in GTFS_OUT_FILES and m.endswith(".txt")]
    if dropped:
        LOG.info("  fichiers non recopiés (hors fichiers standards retenus) : %s", ", ".join(dropped))

    # ---- arrêts dans la zone (+ parents + enfants des parents) ----------------
    stops = read_table(zf, "stops")
    n_stops_before = len(stops)
    lon = pd.to_numeric(stops["stop_lon"], errors="coerce").to_numpy(dtype="float64")
    lat = pd.to_numeric(stops["stop_lat"], errors="coerce").to_numpy(dtype="float64")
    shapely.prepare(zone)
    inside = shapely.contains_xy(zone, lon, lat)          # coordonnées absentes -> False
    ids = stops["stop_id"].to_numpy()
    parent_of = dict(zip(ids, stops["parent_station"].to_numpy())) if "parent_station" in stops else {}
    keep = set(ids[inside])
    n_inside = len(keep)
    # parents (et parents de parents : quai -> arrêt -> zone d'arrêt)
    frontier = set(keep)
    while frontier:
        new = {parent_of.get(s, "") for s in frontier} - {""} - keep
        keep |= new
        frontier = new
    parents = {parent_of[s] for s in keep if parent_of.get(s, "")}
    children = {s for s, p in parent_of.items() if p in parents}
    keep |= children
    known = set(ids)
    n_dangling = len(keep - known)
    keep &= known
    LOG.info("  arrêts : %s au total, %s dans la zone, %s retenus avec parents/enfants%s",
             f"{n_stops_before:,}", f"{n_inside:,}", f"{len(keep):,}",
             f" ({n_dangling} parents absents de stops.txt)" if n_dangling else "")

    # ---- routes, trajets, grandes lignes --------------------------------------
    routes = read_table(zf, "routes")
    trips = read_table(zf, "trips")
    n_routes_before, n_trips_before = len(routes), len(trips)
    routing = cfg.get("routing", {})
    exclude_ld = bool(routing.get("exclude_long_distance_rail", False))
    ld_types = [str(t) for t in routing.get("long_distance_route_types", LONG_DISTANCE_ROUTE_TYPES)]
    ld_prefixes = list(routing.get("long_distance_name_prefixes", LONG_DISTANCE_PREFIXES))
    rx = long_distance_matcher(ld_prefixes)
    for col in ("route_short_name", "route_long_name"):      # l'un des deux peut manquer
        if col not in routes:
            routes[col] = ""
    short, long_ = routes["route_short_name"], routes["route_long_name"]
    label = short.where(short.str.strip() != "", long_)      # nom court, à défaut nom long
    is_type = routes["route_type"].isin(ld_types)
    is_name = label.map(lambda s: bool(rx.search(s)))
    routes["_ld"] = (is_type | is_name) if exclude_ld else False
    routes["_label"] = label
    ld_route_ids = set(routes.loc[routes["_ld"], "route_id"])

    # ---- passe 1 : nb d'arrêts-dans-la-zone par trajet (stop_times, 2 colonnes) ------
    t0 = time.perf_counter()
    counts: dict[str, int] = {}
    n_rows = 0
    for i, chunk in enumerate(iter_chunks(zf, "stop_times", usecols=["trip_id", "stop_id"])):
        n_rows += len(chunk)
        vc = chunk.loc[chunk["stop_id"].isin(keep), "trip_id"].value_counts()
        for trip, n in zip(vc.index, vc.to_numpy()):
            counts[trip] = counts.get(trip, 0) + int(n)
        if (i + 1) % 10 == 0:
            LOG.info("  passe 1 : %s lignes lues…", f"{n_rows:,}")
    n_stop_times_before = n_rows
    in_zone_trips = {t for t, n in counts.items() if n >= 2}
    LOG.info("  passe 1 : %s lignes stop_times, %s trajets avec ≥ 2 arrêts dans la zone (%.0f s)",
             f"{n_rows:,}", f"{len(in_zone_trips):,}", time.perf_counter() - t0)

    zone_trips = trips[trips["trip_id"].isin(in_zone_trips)]
    unknown_route = ~zone_trips["route_id"].isin(set(routes["route_id"]))
    if unknown_route.any():
        LOG.warning("  %d trajets de la zone référencent une route absente de routes.txt : écartés",
                    int(unknown_route.sum()))
        zone_trips = zone_trips[~unknown_route]
    n_zone_trips = len(zone_trips)
    excluded = zone_trips["route_id"].isin(ld_route_ids)
    kept_trips_df = zone_trips[~excluded]
    kept_trip_ids = set(kept_trips_df["trip_id"])

    # bilan des routes grandes lignes réellement présentes dans la zone
    ld_stats = (zone_trips[excluded].groupby("route_id").size().rename("n_trips_in_zone").reset_index()
                .merge(routes[[c for c in ("route_id", "agency_id", "route_short_name", "route_long_name",
                                           "route_type", "_label") if c in routes]],
                       on="route_id", how="left")
                .sort_values(["_label", "route_id"]))
    ld_stats.insert(0, "feed", source_id)
    if exclude_ld:
        LOG.info("  grandes lignes exclues : %d routes, %s trajets (de la zone) — règle : route_type ∈ %s "
                 "ou nom commençant par %s", len(ld_stats), f"{int(ld_stats['n_trips_in_zone'].sum()):,}",
                 ld_types, ld_prefixes)
        # récapitulatif par famille (ICE, IC, EC…, ou « type 101 ») : la liste complète va dans le CSV
        by_family: dict[str, list[int]] = {}
        for _, r in ld_stats.iterrows():
            m = re.match(r"\s*([^\W\d_]+)", r["_label"])
            key = m.group(1).upper() if m and rx.search(r["_label"]) else f"route_type {r['route_type']}"
            by_family.setdefault(key, [0, 0])
            by_family[key][0] += 1
            by_family[key][1] += int(r["n_trips_in_zone"])
        for key, (nr, nt) in sorted(by_family.items(), key=lambda kv: -kv[1][1]):
            LOG.info("    %-16s %3d routes %6d trajets", key, nr, nt)
        LOG.info("    liste complète : qa_gtfs_long_distance_excluded.csv")
    else:
        LOG.info("  routing.exclude_long_distance_rail = false : aucune route exclue")

    # ---- passe 2 : stop_times restreints (toutes colonnes) ----------------------
    t0 = time.perf_counter()
    ws, we = (hms_to_sec(cfg["analysis"]["window_start"]), hms_to_sec(cfg["analysis"]["window_end"]))
    parts, deps = [], []
    n_rows, n_kept_rows, n_missing_time = 0, 0, 0
    for i, chunk in enumerate(iter_chunks(zf, "stop_times")):
        n_rows += len(chunk)
        sub = chunk[chunk["stop_id"].isin(keep)]
        sub = sub[sub["trip_id"].isin(kept_trip_ids)]
        if len(sub):
            dep = hms_series_to_sec(sub["departure_time"]) if "departure_time" in sub else np.full(len(sub), np.nan)
            arr = hms_series_to_sec(sub["arrival_time"]) if "arrival_time" in sub else np.full(len(sub), np.nan)
            n_missing_time += int((np.isnan(dep) | np.isnan(arr)).sum())
            parts.append(sub)
            deps.append(np.where(np.isnan(dep), arr, dep))   # départ, à défaut arrivée
            n_kept_rows += len(sub)
        if (i + 1) % 10 == 0:
            LOG.info("  passe 2 : %s lignes lues, %s retenues…", f"{n_rows:,}", f"{n_kept_rows:,}")
    if not parts:
        raise SystemExit(f"ERREUR : aucun trajet du GTFS {source_id} n'a ≥ 2 arrêts dans la zone de découpe "
                         f"(zone mal placée ou GTFS d'une autre région ?).")
    st = pd.concat(parts, ignore_index=True)
    dep = np.concatenate(deps)
    del parts, deps
    # tri par trip_id puis stop_sequence (numérique), sans rien renuméroter
    codes, uniques = pd.factorize(st["trip_id"], sort=True)
    seq = pd.to_numeric(st["stop_sequence"], errors="coerce").to_numpy(dtype="float64")
    if np.isnan(seq).any():
        LOG.warning("  %d lignes stop_times sans stop_sequence numérique", int(np.isnan(seq).sum()))
    order = np.lexsort((seq, codes))
    codes_s, dep_s = codes[order], dep[order]
    LOG.info("  passe 2 : %s lignes stop_times retenues sur %s (%.0f s)",
             f"{len(st):,}", f"{n_rows:,}", time.perf_counter() - t0)
    if n_missing_time:
        LOG.warning("  %s lignes retenues ont une heure d'arrivée ou de départ vide "
                    "(R5 les interpole entre arrêts voisins, parfois supprimés par la découpe)",
                    f"{n_missing_time:,}")

    # statistiques par trajet pour le contrôle du jour : 1er départ dans la zone, départs en fenêtre
    starts = np.flatnonzero(np.r_[True, codes_s[1:] != codes_s[:-1]])
    first_dep = dep_s[starts]                                  # aligné sur `uniques` (codes triés)
    in_win = (dep_s >= ws) & (dep_s <= we)
    n_win = np.bincount(codes_s[in_win], minlength=len(uniques))
    ends = np.r_[starts[1:] - 1, len(codes_s) - 1]             # dernière ligne de chaque trajet
    edge_no_time = np.isnan(first_dep) | np.isnan(dep_s[ends])  # 1er ou dernier arrêt sans aucune heure
    trip_stats = pd.DataFrame({"trip_id": np.asarray(uniques),
                               "first_in_win": ((first_dep >= ws) & (first_dep <= we)).astype(int),
                               "n_win": n_win, "edge_no_time": edge_no_time})

    # ---- tables dépendantes : calendar, calendar_dates, routes, agency, shapes ------
    kept_trips_df = kept_trips_df[kept_trips_df["trip_id"].isin(set(uniques))]
    # trajets sans heure au 1er ou dernier arrêt retenu (ex. transport à la demande à fenêtres
    # horaires) : R5 ne peut pas les interpoler et les ignore avec un avertissement
    bad_ids = set(trip_stats.loc[trip_stats["edge_no_time"], "trip_id"])
    if bad_ids:
        bad = kept_trips_df[kept_trips_df["trip_id"].isin(bad_ids)].groupby("route_id").size()
        names = routes.set_index("route_id")["_label"]
        LOG.warning("  %d trajets dont le 1er ou le dernier arrêt retenu n'a aucune heure (R5 les ignorera) : %s",
                    len(bad_ids), "; ".join(f"{names.get(r, '?')} [{r}] : {n}" for r, n in bad.items()))
    service_ids = set(kept_trips_df["service_id"])
    route_ids = set(kept_trips_df["route_id"])
    routes_out = routes[routes["route_id"].isin(route_ids)].drop(columns=["_ld", "_label"])
    calendar = read_table(zf, "calendar")
    calendar_dates = read_table(zf, "calendar_dates")
    cal_out = calendar[calendar["service_id"].isin(service_ids)] if calendar is not None else None
    cd_out = calendar_dates[calendar_dates["service_id"].isin(service_ids)] if calendar_dates is not None else None
    agency = read_table(zf, "agency")
    if (agency is not None and "agency_id" in agency and "agency_id" in routes_out
            and (routes_out["agency_id"] != "").all()):
        agency_out = agency[agency["agency_id"].isin(set(routes_out["agency_id"]))]
    else:
        agency_out = agency                                    # agence unique implicite : tout garder
    stops_out = stops[stops["stop_id"].isin(keep)]
    shape_ids = set(kept_trips_df["shape_id"]) - {""} if "shape_id" in kept_trips_df else set()

    # ---- écriture du zip --------------------------------------------------------------
    tmp_path = out_path.with_suffix(".zip.tmp")
    n_shapes_before = n_shapes_after = None
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
        if agency_out is not None:
            write_table(zout, "agency", agency_out)
        write_table(zout, "stops", stops_out)
        write_table(zout, "routes", routes_out)
        write_table(zout, "trips", kept_trips_df)
        write_table(zout, "stop_times", st, order=order)
        if cal_out is not None:
            write_table(zout, "calendar", cal_out)
        if cd_out is not None:
            write_table(zout, "calendar_dates", cd_out)
        if find_member(zf, "shapes"):
            n_shapes_before = n_shapes_after = 0
            seen_ids = set()
            with zout.open("shapes.txt", "w", force_zip64=True) as out:
                first = True
                for chunk in iter_chunks(zf, "shapes"):
                    n_shapes_before += len(chunk)
                    sub = chunk[chunk["shape_id"].isin(shape_ids)]
                    n_shapes_after += len(sub)
                    seen_ids |= set(sub["shape_id"].unique())
                    if first or len(sub):
                        out.write(sub.to_csv(index=False, header=first, lineterminator="\n").encode("utf-8"))
                        first = False
            if shape_ids - seen_ids:
                LOG.warning("  %d shape_id référencés par des trajets sont absents de shapes.txt",
                            len(shape_ids - seen_ids))
        fi_member = find_member(zf, "feed_info")
        if fi_member:                                           # feed_info.txt tel quel
            zout.writestr("feed_info.txt", zf.read(fi_member))
    tmp_path.replace(out_path)
    zf.close()

    res = {
        "source_id": source_id, "path": str(out_path), "size_bytes": out_path.stat().st_size,
        "before": {"stops": n_stops_before, "routes": n_routes_before, "trips": n_trips_before,
                   "stop_times": n_stop_times_before, "shapes_rows": n_shapes_before,
                   "size_bytes": src.stat().st_size},
        "after": {"stops": len(stops_out), "routes": len(routes_out), "trips": len(kept_trips_df),
                  "stop_times": len(st), "services": len(service_ids), "shapes_rows": n_shapes_after},
        "zone_trips_before_long_distance": n_zone_trips,
        "long_distance": {"enabled": exclude_ld, "route_types": ld_types, "name_prefixes": ld_prefixes,
                          "n_routes_in_feed": len(ld_route_ids), "n_routes_excluded": len(ld_stats),
                          "n_trips_excluded": int(ld_stats["n_trips_in_zone"].sum()) if len(ld_stats) else 0},
        "n_missing_time_rows": n_missing_time, "n_trips_edge_no_time": len(bad_ids),
        "ld_stats": ld_stats, "trips": kept_trips_df[["trip_id", "service_id"]],
        "calendar": cal_out, "calendar_dates": cd_out, "trip_stats": trip_stats,
        "stop_ids_out": set(stops_out["stop_id"]),
    }
    LOG.info("  APRÈS découpe : %s arrêts (avant %s), %s trajets (avant %s), %s routes (avant %s), "
             "%s stop_times (avant %s)%s ; zip %s (source %s) — %.0f s",
             f"{res['after']['stops']:,}", f"{n_stops_before:,}", f"{res['after']['trips']:,}",
             f"{n_trips_before:,}", f"{res['after']['routes']:,}", f"{n_routes_before:,}",
             f"{len(st):,}", f"{n_stop_times_before:,}",
             "" if n_shapes_before is None else f", shapes {n_shapes_after:,} lignes (avant {n_shapes_before:,})",
             human(res["size_bytes"]), human(src.stat().st_size), time.perf_counter() - t_start)
    return res


# --------------------------------------------------------------------------- #
# 4. Contrôle du jour d'analyse
# --------------------------------------------------------------------------- #
def service_by_date(res: dict, per_route: pd.DataFrame | None = None) -> pd.DataFrame:
    """Par date du feed (calendar + calendar_dates) : trajets actifs, trajets dont le
    1er départ dans la zone tombe dans la fenêtre, départs (arrêts) dans la fenêtre.

    Avec per_route (trips : trip_id, route_id, service_id) : renvoie plutôt une table
    ligne (route_id) × date du nombre de trajets actifs."""
    cal, cd, trips = res["calendar"], res["calendar_dates"], res["trips"]
    parts = []
    if cal is not None and len(cal):
        parts += [pd.to_datetime(cal["start_date"], format="%Y%m%d"), pd.to_datetime(cal["end_date"], format="%Y%m%d")]
    if cd is not None and len(cd):
        parts += [pd.to_datetime(cd["date"], format="%Y%m%d")]
    if not parts:
        raise SystemExit(f"ERREUR : le GTFS {res['source_id']} n'a ni calendar ni calendar_dates.")
    d_min = min(p.min() for p in parts)
    d_max = max(p.max() for p in parts)
    dates = pd.date_range(d_min, d_max, freq="D")
    wd = dates.weekday.to_numpy()

    services = sorted(set(trips["service_id"]))
    sidx = {s: i for i, s in enumerate(services)}
    active = np.zeros((len(services), len(dates)), dtype=bool)
    if cal is not None:
        day_cols = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for r in cal.itertuples(index=False):
            i = sidx.get(r.service_id)
            if i is None:
                continue
            flags = np.array([getattr(r, c) == "1" for c in day_cols])
            start, end = pd.to_datetime(r.start_date, format="%Y%m%d"), pd.to_datetime(r.end_date, format="%Y%m%d")
            active[i] = (dates >= start) & (dates <= end) & flags[wd]
    if cd is not None and len(cd):
        for r in cd.itertuples(index=False):                    # 1 = ajout, 2 = suppression
            i = sidx.get(r.service_id)
            if i is None or r.exception_type not in ("1", "2"):
                continue
            j = (pd.to_datetime(r.date, format="%Y%m%d") - d_min).days
            active[i, j] = (r.exception_type == "1")

    if per_route is not None:
        pr = per_route[per_route["service_id"].isin(sidx)]
        rids = sorted(set(pr["route_id"]))
        ridx = {r: i for i, r in enumerate(rids)}
        m = np.zeros((len(rids), len(services)))
        np.add.at(m, (pr["route_id"].map(ridx).to_numpy(), pr["service_id"].map(sidx).to_numpy()), 1)
        out = pd.DataFrame((m @ active).astype(int), index=pd.Index(rids, name="route_id"), columns=dates)
        return out

    stats = trips.merge(res["trip_stats"], on="trip_id", how="left")
    si = stats["service_id"].map(sidx).to_numpy()
    n_trips = np.bincount(si, minlength=len(services))
    n_first = np.bincount(si, weights=stats["first_in_win"].fillna(0).to_numpy(), minlength=len(services))
    n_events = np.bincount(si, weights=stats["n_win"].fillna(0).to_numpy(), minlength=len(services))
    out = pd.DataFrame({"trips": n_trips @ active,
                        "window_trip_departures": (n_first @ active).astype(int),
                        "window_stop_departures": (n_events @ active).astype(int)},
                       index=dates)
    out.index.name = "date"
    return out


def parse_period_list(raw) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """[[début, fin], …] (bornes incluses) de la configuration."""
    return [(to_day(a), to_day(b)) for a, b in (raw or [])]


def flag_days(dates: pd.DatetimeIndex, cfg: dict) -> pd.DataFrame:
    """Colonnes is_school_holiday / is_public_holiday pour chaque date."""
    periods = parse_period_list(cfg["analysis"].get("school_holidays"))
    pub = {to_day(d) for d in (cfg["analysis"].get("public_holidays") or [])}
    school = np.zeros(len(dates), dtype=bool)
    for a, b in periods:
        school |= (dates >= a) & (dates <= b)
    return pd.DataFrame({"is_school_holiday": school,
                         "is_public_holiday": [d in pub for d in dates]}, index=dates)


def check_day(df: pd.DataFrame, day: pd.Timestamp, min_ratio: float, label: str):
    """Contrôle du jour sur un tableau par date ; renvoie (infos, liste de problèmes)."""
    problems = []
    ref_days = df[df["is_reference_day"]]
    reference = float(ref_days["trips"].median()) if len(ref_days) else float("nan")
    info = {"feed": label, "date": day.date().isoformat(), "weekday": day.day_name(),
            "feed_first_date": df.index.min().date().isoformat(),
            "feed_last_date": df.index.max().date().isoformat(),
            "reference_median_trips": reference, "n_reference_days": int(len(ref_days)),
            "min_service_ratio": min_ratio}
    if day not in df.index:
        problems.append(f"[{label}] la date {day.date()} est hors de la plage du feed "
                        f"({info['feed_first_date']} → {info['feed_last_date']})")
        return info, problems
    row = df.loc[day]
    info.update({"trips": int(row["trips"]), "is_school_holiday": bool(row["is_school_holiday"]),
                 "is_public_holiday": bool(row["is_public_holiday"]),
                 "window_trip_departures": int(row["window_trip_departures"]),
                 "window_stop_departures": int(row["window_stop_departures"])})
    if row["is_school_holiday"]:
        problems.append(f"[{label}] la date {day.date()} tombe pendant des vacances scolaires (analysis.school_holidays)")
    if row["is_public_holiday"]:
        problems.append(f"[{label}] la date {day.date()} est un jour férié (analysis.public_holidays)")
    if not len(ref_days) or not reference > 0:
        info["ratio"] = None
        problems.append(f"[{label}] pas de jour de référence exploitable (lun–ven hors vacances et fériés, "
                        f"médiane {reference})")
    else:
        info["ratio"] = float(row["trips"] / reference)
        if info["ratio"] < min_ratio:
            problems.append(f"[{label}] {int(row['trips']):,} trajets le {day.date()} = {info['ratio']:.3f} × la "
                            f"référence ({reference:,.0f}) < min_service_ratio {min_ratio}")
    return info, problems


def check_service(cfg: dict, results: dict, allow_low: bool) -> dict:
    """Contrôle du jour d'analyse sur les GTFS découpés (par feed et au total)."""
    a = cfg["analysis"]
    day = to_day(a["date"])
    min_ratio = float(a["min_service_ratio"])
    per_feed = {sid: service_by_date(res) for sid, res in results.items()}
    ids = list(per_feed)

    # total : somme des feeds sur les dates communes à tous les feeds
    common = per_feed[ids[0]].index
    for sid in ids[1:]:
        common = common.intersection(per_feed[sid].index)
    total = sum(per_feed[sid].loc[common] for sid in ids)
    if len(ids) > 1:
        LOG.info("Total multi-feeds calculé sur la plage commune %s → %s", common.min().date(), common.max().date())

    tables = {"total": total, **per_feed} if len(ids) > 1 else {ids[0]: per_feed[ids[0]]}
    infos, problems = {}, []
    for label, df in tables.items():
        flags = flag_days(df.index, cfg)
        df[["is_school_holiday", "is_public_holiday"]] = flags
        df["is_reference_day"] = (df.index.weekday < 5) & ~df["is_school_holiday"] & ~df["is_public_holiday"]
        info, probs = check_day(df, day, min_ratio, label)
        infos[label] = info
        # un feed individuel vide ne bloque pas (il n'a pas de service dans la zone) ; le total oui
        if label != "total" and len(ids) > 1 and not info["reference_median_trips"] > 0:
            LOG.warning("Feed %s sans service de référence dans la zone : ignoré dans le contrôle", label)
            continue
        problems += probs

    # CSV : colonnes de base sur le total ; une colonne par feed si plusieurs feeds
    main = tables["total"] if len(ids) > 1 else per_feed[ids[0]]
    csv = pd.DataFrame({"date": main.index.strftime("%Y-%m-%d"), "weekday": main.index.day_name(),
                        "trips": main["trips"].to_numpy(),
                        "is_school_holiday": main["is_school_holiday"].to_numpy(),
                        "is_public_holiday": main["is_public_holiday"].to_numpy(),
                        "is_reference_day": main["is_reference_day"].to_numpy(),
                        "window_trip_departures": main["window_trip_departures"].to_numpy(),
                        "window_stop_departures": main["window_stop_departures"].to_numpy()})
    if len(ids) > 1:
        for sid in ids:
            csv[f"trips_{sid}"] = per_feed[sid].loc[main.index, "trips"].to_numpy()
    csv_path = outputs_dir(cfg) / "qa_gtfs_service_by_date.csv"
    csv.to_csv(csv_path, index=False)
    LOG.info("Écrit : %s", csv_path)

    for label, info in infos.items():
        if "trips" in info:
            LOG.info("Contrôle du jour [%s] %s (%s) : %s trajets actifs, référence (médiane de %d jours lun–ven "
                     "hors vacances/fériés) = %s, ratio = %s (seuil %.2f) ; départs 1er arrêt de zone dans "
                     "%s–%s : %s trajets, %s départs aux arrêts",
                     label, info["date"], info["weekday"], f"{info['trips']:,}", info["n_reference_days"],
                     f"{info['reference_median_trips']:,.0f}",
                     "n/a" if info.get("ratio") is None else f"{info['ratio']:.3f}", min_ratio,
                     a["window_start"], a["window_end"], f"{info['window_trip_departures']:,}",
                     f"{info['window_stop_departures']:,}")
        else:
            LOG.info("Contrôle du jour [%s] %s : hors plage du feed", label, info["date"])

    top = infos["total"] if len(ids) > 1 else infos[ids[0]]
    ok = not problems
    value = {"date": top["date"], "weekday": top["weekday"], "trips": top.get("trips"),
             "reference_median": top["reference_median_trips"], "ratio": top.get("ratio"),
             "min_service_ratio": min_ratio, "n_reference_days": top["n_reference_days"],
             "window": [a["window_start"], a["window_end"]],
             "window_trip_departures": top.get("window_trip_departures"),
             "window_stop_departures": top.get("window_stop_departures"),
             "feed_range": [top["feed_first_date"], top["feed_last_date"]],
             "ok": ok, "allow_low_service": bool(allow_low), "problems": problems,
             "per_feed": infos if len(ids) > 1 else None}
    ratio_txt = "n/a" if top.get("ratio") is None else f"{top['ratio']:.3f}"
    trips_fr = f"{top['trips']:,} trajets actifs" if "trips" in top else "date hors de la plage du feed"
    trips_de = f"{top['trips']:,} aktive Fahrten" if "trips" in top else "Datum außerhalb des Feed-Zeitraums"
    log_assumption(
        cfg, "02", "D1_analysis_date",
        f"Jour d'analyse {top['date']} ({top['weekday']}) : {trips_fr} dans le GTFS "
        f"découpé, pour une référence de {top['reference_median_trips']:,.0f} (médiane des jours lun–ven hors "
        f"vacances scolaires et fériés du feed, {top['n_reference_days']} jours) ; ratio {ratio_txt}, seuil "
        f"{min_ratio}. {'Contrôle réussi.' if ok else 'Contrôle NON réussi : ' + ' ; '.join(problems)}",
        f"Stichtag {top['date']}: {trips_de} im zugeschnittenen GTFS, bei einem "
        f"Referenzwert von {top['reference_median_trips']:,.0f} (Median der Werktage Mo–Fr ohne Schulferien und "
        f"Feiertage im Feed, {top['n_reference_days']} Tage); Verhältnis {ratio_txt}, Schwelle {min_ratio}. "
        f"{'Prüfung bestanden.' if ok else 'Prüfung NICHT bestanden: ' + ' ; '.join(problems)}",
        value=value)
    if problems:
        for p in problems:
            (LOG.warning if allow_low else LOG.error)("Contrôle du jour : %s", p)
        if not allow_low:
            raise SystemExit("ERREUR : le jour d'analyse ne passe pas le contrôle du service (voir ci-dessus ; "
                             "relancer avec --allow-low-service pour passer outre).\n" + "\n".join(problems))
    return value


def route_trips_by_date(zpath: Path) -> pd.DataFrame:
    """Trajets actifs par ligne (route_id, lignes) et par date du feed (colonnes), GTFS découpé."""
    with zipfile.ZipFile(zpath) as zf:
        trips = read_table(zf, "trips")[["trip_id", "route_id", "service_id"]]
        cal, cd = read_table(zf, "calendar"), read_table(zf, "calendar_dates")
        routes = read_table(zf, "routes")
    d = service_by_date({"calendar": cal, "calendar_dates": cd, "trips": trips[["trip_id", "service_id"]],
                         "trip_stats": pd.DataFrame({"trip_id": [], "first_in_win": [], "n_win": []}),
                         "source_id": zpath.name}, per_route=trips)
    names = routes.set_index("route_id")
    label = names["route_short_name"].where(names["route_short_name"].fillna("") != "", names.get("route_long_name"))
    d.insert(0, "route_name", label.reindex(d.index).fillna(""))
    return d


def check_routes(cfg: dict, gtfs_dir: Path, allow_low: bool) -> dict:
    """Contrôle ligne par ligne du jour d'analyse (D2_route_coverage).

    Ligne « habituelle » : médiane ≥ 1 trajet sur les jours de référence (lun–ven hors
    vacances et fériés, comme D1). Problème si les lignes habituelles sans AUCUN trajet le
    jour d'analyse portent plus de analysis.max_missing_route_share des trajets habituels
    (somme des médianes). Le contrôle global D1 ne voit pas ce cas quand d'autres lignes
    compensent (flux « latest » incomplet pour certaines lignes, par ex.).
    """
    a = cfg["analysis"]
    day = to_day(a["date"])
    max_share = float(a.get("max_missing_route_share", DEFAULT_MAX_MISSING_ROUTE_SHARE))
    rows, per_feed, problems = [], {}, []
    for zpath in sorted(gtfs_dir.glob("*.zip")):
        d = route_trips_by_date(zpath)
        dates = pd.DatetimeIndex([c for c in d.columns if isinstance(c, pd.Timestamp)])
        flags = flag_days(dates, cfg)
        ref_cols = dates[(dates.weekday < 5) & ~flags["is_school_holiday"].to_numpy() & ~flags["is_public_holiday"].to_numpy()]
        if day not in dates or not len(ref_cols):
            problems.append(f"[{zpath.stem}] contrôle par ligne impossible (date hors feed ou aucun jour de référence)")
            continue
        med = d[ref_cols].median(axis=1)
        usual = med >= 1
        missing = usual & (d[day] == 0)
        share = float(med[missing].sum() / med[usual].sum()) if usual.any() else 0.0
        per_feed[zpath.stem] = {"n_usual_routes": int(usual.sum()), "n_missing_routes": int(missing.sum()),
                                "usual_trips": float(med[usual].sum()), "missing_usual_trips": float(med[missing].sum()),
                                "share_missing": round(share, 4), "n_reference_days": int(len(ref_cols))}
        for rid in d.index[missing]:
            rows.append({"feed": zpath.stem, "route_id": rid, "route_name": d.loc[rid, "route_name"],
                         "median_reference_trips": float(med[rid]), "trips_on_day": 0})
        LOG.info("Contrôle par ligne [%s] %s : %d lignes habituelles, %d sans aucun trajet ce jour (%.2f %% des "
                 "trajets habituels ; seuil %.2f %%)", zpath.stem, day.date(), int(usual.sum()), int(missing.sum()),
                 100 * share, 100 * max_share)
        if share > max_share:
            problems.append(f"[{zpath.stem}] {int(missing.sum())} lignes habituelles sans aucun trajet le {day.date()} "
                            f"({100 * share:.2f} % des trajets habituels > {100 * max_share:.2f} %)")
    out_csv = outputs_dir(cfg) / "qa_gtfs_routes_missing.csv"
    pd.DataFrame(rows, columns=["feed", "route_id", "route_name", "median_reference_trips", "trips_on_day"]) \
        .sort_values("median_reference_trips", ascending=False).to_csv(out_csv, index=False)
    LOG.info("Écrit : %s", out_csv)
    ok = not problems
    txt = "; ".join(f"{k} : {v['n_missing_routes']} sur {v['n_usual_routes']} lignes habituelles sans trajet "
                    f"({100 * v['share_missing']:.2f} % des trajets habituels)" for k, v in per_feed.items())
    txt_de = "; ".join(f"{k}: {v['n_missing_routes']} von {v['n_usual_routes']} üblichen Linien ohne Fahrt "
                       f"({100 * v['share_missing']:.2f} % der üblichen Fahrten)" for k, v in per_feed.items())
    log_assumption(
        cfg, "02", "D2_route_coverage",
        f"Contrôle par ligne du jour d'analyse {day.date()} (ligne habituelle = au moins 1 trajet en médiane des jours "
        f"de référence de D1 ; seuil {100 * max_share:.1f} % des trajets habituels sur des lignes absentes) : {txt}. "
        f"{'Contrôle réussi.' if ok else 'Contrôle NON réussi : ' + ' ; '.join(problems)} Liste : qa_gtfs_routes_missing.csv.",
        f"Linienprüfung des Stichtags {day.date()} (übliche Linie = mindestens 1 Fahrt im Median der Referenztage aus D1; "
        f"Schwelle {100 * max_share:.1f} % der üblichen Fahrten auf fehlenden Linien): {txt_de}. "
        f"{'Prüfung bestanden.' if ok else 'Prüfung NICHT bestanden: ' + ' ; '.join(problems)} Liste: qa_gtfs_routes_missing.csv.",
        value={"date": day.date().isoformat(), "max_missing_route_share": max_share, "per_feed": per_feed, "ok": ok})
    if problems:
        for p in problems:
            (LOG.warning if allow_low else LOG.error)("Contrôle par ligne : %s", p)
        if not allow_low:
            raise SystemExit("ERREUR : des lignes habituelles n'ont aucun trajet le jour d'analyse (voir ci-dessus ; "
                             "--allow-low-service pour passer outre).\n" + "\n".join(problems))
    return {"per_feed": per_feed, "ok": ok, "problems": problems}


def log_gtfs_assumptions(cfg: dict, results: dict, zone_info: dict) -> None:
    """Hypothèses G1 (grandes lignes) et G2 (découpe GTFS)."""
    routing = cfg.get("routing", {})
    exclude = bool(routing.get("exclude_long_distance_rail", False))
    ld = pd.concat([r["ld_stats"] for r in results.values()], ignore_index=True)
    ld_cols = ["feed", "route_id", "route_short_name", "route_long_name", "route_type", "n_trips_in_zone"]
    if "agency_id" in ld:
        ld_cols.insert(4, "agency_id")
    ld[ld_cols].to_csv(outputs_dir(cfg) / "qa_gtfs_long_distance_excluded.csv", index=False)
    n_routes, n_trips = len(ld), int(ld["n_trips_in_zone"].sum()) if len(ld) else 0
    labels = sorted({s.strip() for s in ld["_label"]}) if len(ld) else []
    first = next(iter(results.values()))["long_distance"]
    rule_fr = (f"route_type dans {first['route_types']} ou nom de route (court, à défaut long) commençant par "
               f"l'un de {first['name_prefixes']} (mot entier, insensible à la casse)")
    rule_de = (f"route_type in {first['route_types']} oder Liniennamen (kurz, sonst lang), die mit einem von "
               f"{first['name_prefixes']} beginnen (ganzes Wort, Groß-/Kleinschreibung egal)")
    if exclude:
        log_assumption(
            cfg, "02", "G1_long_distance_excluded",
            f"Trains grandes lignes exclus du réseau routé (non utilisables avec un abonnement local) : {rule_fr}. "
            f"Dans la zone : {n_routes} routes et {n_trips:,} trajets écartés (≥ 2 arrêts dans la zone).",
            f"Fernverkehr aus dem Routing-Netz ausgeschlossen (nicht mit Nahverkehrsabo nutzbar): {rule_de}. "
            f"Im Zuschnittsgebiet: {n_routes} Linien und {n_trips:,} Fahrten entfernt (≥ 2 Halte im Gebiet).",
            value={"enabled": True, "n_routes": n_routes, "n_trips": n_trips, "labels": labels,
                   "route_types": first["route_types"], "name_prefixes": first["name_prefixes"],
                   "per_feed": {sid: r["long_distance"] for sid, r in results.items()}})
    else:
        log_assumption(
            cfg, "02", "G1_long_distance_excluded",
            "Les trains grandes lignes ne sont PAS exclus (routing.exclude_long_distance_rail = false).",
            "Fernverkehr wird NICHT ausgeschlossen (routing.exclude_long_distance_rail = false).",
            value={"enabled": False})
    buffer_txt = (f"enveloppe convexe de l'union des tampons de {zone_info['buffer_km']:g} km "
                  f"(analysis.clip_buffer_km) autour des destinations dans les rayons et des origines"
                  if zone_info["mode"] == "tampon_enveloppe_convexe" else "zone fournie par --clip-area")
    buffer_de = (f"konvexe Hülle der Vereinigung der {zone_info['buffer_km']:g}-km-Puffer "
                 f"(analysis.clip_buffer_km) um die Ziele in den Radien und die Ursprungspunkte"
                 if zone_info["mode"] == "tampon_enveloppe_convexe" else "per --clip-area vorgegebenes Gebiet")
    log_assumption(
        cfg, "02", "G2_gtfs_clip",
        f"GTFS découpé sur la zone ({buffer_txt}) : arrêts situés dans la zone + leurs parent_station + les "
        f"enfants de ces parents ; stop_times limités à ces arrêts (stop_sequence et heures d'origine conservées) ; "
        f"trajets gardés s'ils ont ≥ 2 arrêts dans la zone ; routes, calendriers, agences et shapes limités à ce "
        f"qui est référencé. Seuls les fichiers standards (agency, stops, routes, trips, stop_times, calendar, "
        f"calendar_dates, shapes, feed_info) sont écrits.",
        f"GTFS auf das Gebiet zugeschnitten ({buffer_de}): Haltestellen im Gebiet + deren parent_station + die "
        f"Kinder dieser Elternknoten; stop_times auf diese Haltestellen beschränkt (ursprüngliche stop_sequence "
        f"und Zeiten bleiben erhalten); Fahrten bleiben, wenn sie ≥ 2 Halte im Gebiet haben; Linien, Kalender, "
        f"Agenturen und Shapes auf Referenziertes beschränkt. Nur Standarddateien (agency, stops, routes, trips, "
        f"stop_times, calendar, calendar_dates, shapes, feed_info) werden geschrieben.",
        value={"zone": zone_info,
               "feeds": {sid: {"before": r["before"], "after": r["after"],
                               "zone_trips_before_long_distance": r["zone_trips_before_long_distance"],
                               "n_trips_edge_no_time": r["n_trips_edge_no_time"],
                               "zip_size_bytes": r["size_bytes"]} for sid, r in results.items()}})


def check_origin_stops(cfg: dict, results: dict) -> None:
    """Contrôle de cohérence : les arrêts GTFS des points d'origine survivent à la découpe."""
    for pt in cfg.get("origin", {}).get("points", []):
        if pt.get("source") == "gtfs_stop" and pt.get("gtfs_stop_id"):
            present = [sid for sid, r in results.items() if pt["gtfs_stop_id"] in r["stop_ids_out"]]
            if present:
                LOG.info("Point d'origine '%s' : arrêt GTFS %s présent dans l'extrait (%s)",
                         pt["id"], pt["gtfs_stop_id"], ", ".join(present))
            else:
                LOG.warning("Point d'origine '%s' : arrêt GTFS %s ABSENT des extraits GTFS", pt["id"],
                            pt["gtfs_stop_id"])


# --------------------------------------------------------------------------- #
# Programme principal
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = cli("Étape 02 — découpe OSM / GTFS autour de la zone d'analyse et contrôle du jour.")
    parser.add_argument("--clip-area", help="zone de découpe (GeoJSON) à utiliser à la place de celle calculée")
    parser.add_argument("--allow-low-service", action="store_true",
                        help="ne pas échouer si le jour d'analyse a un service faible / hors période")
    parser.add_argument("--routes-check-only", action="store_true",
                        help="refait seulement le contrôle par ligne (D2) sur le GTFS déjà découpé")
    parser.add_argument("--skip-osm", action="store_true",
                        help="ne pas refaire l'extrait OSM (mise au point du GTFS)")
    args = parser.parse_args()
    cfg = load_config(args.config)
    interim = interim_dir(cfg)
    LOG.info("Origine %s — région %s (%s)", cfg["origin"]["slug"], cfg["region"]["id"], cfg["region"]["country"])

    if args.routes_check_only:
        check_routes(cfg, interim / "gtfs", args.allow_low_service)
        return

    zone, zone_info = build_clip_area(cfg, args.clip_area, interim / "clip_area.geojson")

    if args.skip_osm:
        LOG.info("OSM : étape sautée (--skip-osm)")
    else:
        log_mirrors(cfg)
        clip_osm(cfg, interim / "clip_area.geojson", interim)

    gtfs_dir = interim / "gtfs"
    gtfs_dir.mkdir(parents=True, exist_ok=True)
    results = {sid: clip_gtfs(cfg, sid, zone, gtfs_dir) for sid in cfg["inputs"]["gtfs"]}
    check_origin_stops(cfg, results)
    log_gtfs_assumptions(cfg, results, zone_info)
    check_service(cfg, results, args.allow_low_service)
    check_routes(cfg, gtfs_dir, args.allow_low_service)
    LOG.info("Terminé. GTFS : %s", ", ".join(r["path"] for r in results.values()))


if __name__ == "__main__":
    main()
