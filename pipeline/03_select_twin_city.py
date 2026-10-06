"""
Choix de la commune allemande de comparaison — indicateurs « So nah, so fern ».

Pour Garches (origine) et chaque commune candidate de la région de Francfort,
le script calcule les MÊMES indicateurs à partir de données officielles :

  - population, surface, densité ;
  - distance géodésique (WGS84) du point de la commune à celui de la ville-centre ;
  - desserte dans un rayon `station_catchment_km` autour du point de la commune,
    le jour d'analyse, tirée des GTFS (zones d'arrêt ferrées, lignes ferrées,
    départs ferrés et bus dans la fenêtre du matin) ;
  - un score de similarité à Garches : moyenne des |log(candidate / Garches)|
    sur les métriques de `similarity_metrics` (plus petit = plus proche).

Le script NE choisit PAS la commune : il produit des chiffres comparables et
leur contrôle (anomalies, sensibilité au rayon). Le choix est justifié à part.

Usage (depuis la racine du repo) :
    uv run python pipeline/03_select_twin_city.py --config config/twin_selection.yaml

Ce fichier-là n'a pas la structure d'une « origine » (pas de bloc `origin`) :
il est lu directement avec yaml ; la config de référence qu'il désigne
(`reference_config`, ici Garches) passe par load_config(), qui fusionne
default.yaml (fenêtre horaire, CRS métrique) et ajoute les sources.

Sorties (outputs/twin_selection/) — données intermédiaires dans
data/interim/twin_selection/ :
    twin_candidates.csv               une ligne par commune, Garches en premier
    twin_candidates.md                tableau lisible + une phrase par commune
    twin_candidates.png               petits multiples (titres en allemand)
    twin_candidates_rail_stations.csv gares ferrées trouvées (détail, contrôle)
    twin_candidates_rail_lines.csv    lignes ferrées trouvées (détail, contrôle)
    twin_candidates_sensitivity.csv   indicateurs et classement selon le rayon
    assumptions.jsonl                 hypothèses (via common.log_assumption)

Définitions retenues (journalisées dans assumptions.jsonl) :
  * « ferré lourd ou guidé » et « bus » : types GTFS listés dans MODES_DEFAUT
    (surchargeables par un bloc `transit_modes` dans le fichier de config) ;
  * le grand-ligne (ICE, IC, EC…) est exclu des lignes ferrées, mais ses
    passages dans le rayon sont recensés pour contrôle ;
  * zone d'arrêt = ancêtre le plus haut de l'arrêt (parent_station), sinon l'arrêt ;
  * ligne = (agence, mode, route_short_name) : plusieurs route_id portant le même
    nom comptent pour UNE ligne (cf. LIGNE_CLE) ;
  * événement de départ = ligne de stop_times d'un trajet actif ce jour-là,
    hors dernier arrêt du trajet, dont l'heure de départ (en secondes depuis
    minuit du jour de service, donc « 31:00 » = 111 600 s) tombe dans
    [window_start ; window_end], bornes incluses.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.compute as pc  # noqa: E402
import pyarrow.csv as pacsv  # noqa: E402
import yaml  # noqa: E402
from pyproj import Geod  # noqa: E402

from common import ROOT, get_logger, load_config, log_assumption, source_path  # noqa: E402

log = get_logger("03_select_twin_city")

# --------------------------------------------------------------------------- #
# Constantes TECHNIQUES (schémas de fichiers, valeurs de repli) — pas de valeur
# métier propre au choix de la commune : celles-ci sont dans twin_selection.yaml.
# --------------------------------------------------------------------------- #
OUT_SLUG = "twin_selection"                       # outputs/ et data/interim/
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
FR_WEEKDAYS = dict(zip(WEEKDAYS, ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]))

# Schéma des fichiers sources
FR_POP_MEMBER = "donnees_communes.csv"            # dans le zip INSEE (sep ';')
DE_GPKG_NAME = "DE_VG250.gpkg"                    # dans le zip BKG
DE_LAYER_GEM, DE_LAYER_PK = "vg250_gem", "vg250_pk"
DE_GF_LAND = 4                                    # GF = 4 : territoire (hors mer)

# Définition des modes : par défaut celle demandée pour l'étude ; un bloc
# `transit_modes` dans twin_selection.yaml la remplace (même structure).
MODES_DEFAUT = {
    # « ferré lourd ou guidé » : libellé de mode -> types GTFS (standard + étendus)
    "rail_modes": {
        "tram":     {"types": [0],  "ranges": [[900, 999]]},
        "metro":    {"types": [1],  "ranges": [[400, 499]]},
        "rail":     {"types": [2],  "ranges": [[100, 199]]},
        "monorail": {"types": [12], "ranges": []},
    },
    "bus": {"types": [3], "ranges": [[700, 799]]},
    # grand-ligne : exclu des lignes ferrées (non utilisable avec un abonnement local)
    "long_distance": {
        "name_prefixes": ["ICE", "IC", "EC", "ECE", "EN", "NJ", "RJ", "RJX", "FLX", "TGV"],
        "types": [101, 102],
    },
}
# Plage de sensibilité du rayon (± %) pour le contrôle « gare à la limite »
SENSIBILITE_PCT_DEFAUT = 25

# Colonnes lues dans stop_times (les deux feeds les ont)
ST_COLS = ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type"]


# --------------------------------------------------------------------------- #
# Petits outils
# --------------------------------------------------------------------------- #
def to_date(x) -> date:
    """yaml charge 2026-10-13 en date ; on accepte aussi une chaîne ISO."""
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return datetime.strptime(str(x), "%Y-%m-%d").date()


def hms_to_s(txt: str) -> int:
    """'07:30' ou '07:30:00' -> secondes depuis minuit."""
    p = [int(v) for v in str(txt).split(":")]
    return p[0] * 3600 + p[1] * 60 + (p[2] if len(p) > 2 else 0)


def series_to_s(s: pd.Series) -> pd.Series:
    """Heures GTFS 'HH:MM:SS' (HH peut dépasser 24) -> secondes ; vide -> NaN."""
    parts = s.fillna("").str.strip().str.split(":", expand=True)
    if parts.shape[1] < 3:
        return pd.Series(np.nan, index=s.index)
    h, m, sec = (pd.to_numeric(parts[i], errors="coerce") for i in range(3))
    return h * 3600 + m * 60 + sec


def natural_key(txt: str) -> list:
    """Tri naturel : S2 < S10, U3 < U12."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(txt))]


def fr_num(x, nd: int = 0) -> str:
    """Nombre au format français (espace insécable en milliers, virgule décimale)."""
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/c"
    return f"{x:,.{nd}f}".replace(",", " ").replace(".", ",")


def de_num(x, nd: int = 0) -> str:
    """Nombre au format allemand (point en milliers, virgule décimale)."""
    s = f"{x:,.{nd}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def in_types(rt: pd.Series, spec: dict) -> pd.Series:
    """route_type ∈ spec["types"] ou dans l'une des plages [a, b] de spec["ranges"]."""
    m = rt.isin(spec.get("types", []))
    for lo, hi in spec.get("ranges", []):
        m |= rt.between(lo, hi)
    return m


def geod_km(geod: Geod, lon0: float, lat0: float, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Distance géodésique WGS84 (km) entre un point et un tableau de points."""
    _, _, d = geod.inv(np.full(len(lon), lon0), np.full(len(lat), lat0), lon, lat)
    return d / 1000.0


# --------------------------------------------------------------------------- #
# Communes : population, surface, point, distance au centre
# --------------------------------------------------------------------------- #
def load_fr(ref: dict, anomalies: list[str]) -> tuple[dict, dict]:
    """Garches (origine) et Paris (ville-centre) depuis ADMIN EXPRESS + INSEE."""
    inp = ref["inputs"]
    code, core_code = ref["origin"]["commune_code"], ref["core_city"]["commune_code"]

    com = gpd.read_parquet(
        source_path(ref, inp["communes"]),
        columns=["code_insee", "nom_officiel", "population", "superficie_cadastrale", "geometrie"],
        filters=[("code_insee", "in", [code, core_code])],
    ).set_index("code_insee")
    pts = gpd.read_parquet(
        source_path(ref, inp["commune_points"]),
        columns=["code_insee_de_la_commune", "geometrie"],
        filters=[("code_insee_de_la_commune", "in", [code, core_code])],
    )
    if pts.code_insee_de_la_commune.duplicated().any() or len(pts) != 2:
        raise RuntimeError("chef-lieu IGN : un point attendu par commune (Garches, ville-centre)")
    pts = pts.set_index("code_insee_de_la_commune").geometry

    # Garde-fous : les codes de la config doivent désigner les bonnes communes
    for c, expected in ((code, ref["origin"]["name"]), (core_code, ref["core_city"]["name"])):
        if com.loc[c, "nom_officiel"] != expected:
            raise RuntimeError(f"IGN : code {c} = « {com.loc[c, 'nom_officiel']} », "
                               f"attendu « {expected} » (config)")

    # Population de Garches : INSEE (populations de référence), sinon rien d'inventé
    with zipfile.ZipFile(source_path(ref, inp["population"])) as z, z.open(FR_POP_MEMBER) as f:
        insee = pd.read_csv(f, sep=";", dtype=str, usecols=["COM", "PMUN"])
    pop = int(insee.loc[insee.COM == code, "PMUN"].iloc[0])
    pop_ign = int(com.loc[code, "population"])
    if pop != pop_ign:
        anomalies.append(f"Population {ref['origin']['name']} : INSEE PMUN = {pop} "
                         f"≠ IGN ADMIN EXPRESS = {pop_ign} (INSEE retenue).")

    # Surface : aire du polygone en projection métrique (Lambert-93)
    area = com.loc[[code]].to_crs(ref["crs"]["metric_fr"]).area.iloc[0] / 1e6
    cad = float(com.loc[code, "superficie_cadastrale"]) / 100.0     # ha -> km²
    if abs(area - cad) / cad > 0.05:
        anomalies.append(f"Surface {ref['origin']['name']} : polygone {area:.2f} km² "
                         f"vs superficie cadastrale {cad:.2f} km² (écart > 5 %).")

    pt = pts.loc[code]
    pt_core = pts.loc[core_code]
    commune = dict(code=code, name=ref["origin"]["name"], country="FR", role="Garches (référence)",
                   lon=pt.x, lat=pt.y, population=pop, area_km2=area,
                   pop_source=f"INSEE, populations de référence 2023 (PMUN), {code}")
    core = dict(name=ref["core_city"]["name"], lon=pt_core.x, lat=pt_core.y)
    return commune, core


def locate_vg250(ref: dict, unit_id: str, work_dir: Path) -> Path:
    """DE_VG250.gpkg : copie déjà extraite dans data/interim/ si elle existe, sinon extraction."""
    interim = ROOT / "data" / "interim"
    for cand in list(interim.glob(f"_unzipped/**/{DE_GPKG_NAME}")) + [work_dir / DE_GPKG_NAME]:
        if cand.exists():
            return cand
    zpath = source_path(ref, unit_id)
    with zipfile.ZipFile(zpath) as z:
        member = next(n for n in z.namelist() if n.endswith(DE_GPKG_NAME))
        log.info("Extraction de %s (%s) …", member, zpath.name)
        out = work_dir / DE_GPKG_NAME
        with z.open(member) as src, open(out, "wb") as dst:
            while chunk := src.read(1 << 24):
                dst.write(chunk)
    return out


def load_de(ref: dict, tw: dict, work_dir: Path) -> tuple[list[dict], dict]:
    """Communes candidates + ville-centre depuis VG250-EW (GF = 4)."""
    de = tw["de"]
    wanted = dict(de["candidates"])
    core_ags, core_name = de["core_city"]["ags"], de["core_city"]["name"]
    all_ags = list(wanted) + [core_ags]
    gpkg = locate_vg250(ref, de["units"], work_dir)
    where = "AGS IN (" + ",".join(f"'{a}'" for a in all_ags) + ")"

    gem = gpd.read_file(gpkg, layer=DE_LAYER_GEM, where=where)
    gem = gem[gem["GF"] == DE_GF_LAND].set_index("AGS")
    pk = gpd.read_file(gpkg, layer=DE_LAYER_PK, where=where)
    # un seul « Kern der Gemeinde » par AGS ; s'il y en avait plusieurs, on garde celui sans OTL
    if pk.AGS.duplicated().any():
        pk = pk[pk["OTL"].isna() | (pk["OTL"] == "")]
    if pk.AGS.duplicated().any():
        raise RuntimeError("vg250_pk : plusieurs points par AGS, ambigu")
    pk = pk.set_index("AGS").to_crs("EPSG:4326")

    for ags, expected in list(wanted.items()) + [(core_ags, core_name)]:
        if ags not in gem.index:
            raise RuntimeError(f"VG250 : AGS {ags} ({expected}) introuvable avec GF = {DE_GF_LAND}")
        if gem.loc[ags, "GEN"] != expected:
            raise RuntimeError(f"VG250 : AGS {ags} = « {gem.loc[ags, 'GEN']} », attendu « {expected} » (config)")
        if ags not in pk.index:
            raise RuntimeError(f"VG250 : pas de point « Kern der Gemeinde » pour {ags}")

    communes = []
    for ags, name in wanted.items():
        g = gem.loc[ags]
        communes.append(dict(
            code=ags, name=name, country="DE", role="Kandidatin",
            lon=pk.loc[ags].geometry.x, lat=pk.loc[ags].geometry.y,
            population=int(g["EWZ"]), area_km2=float(g["KFL"]),
            pop_source="Destatis, Einwohnerzahl 31.12.2024 (VG250-EW, EWZ)"))
    core = dict(name=core_name, lon=pk.loc[core_ags].geometry.x, lat=pk.loc[core_ags].geometry.y)
    return communes, core


# --------------------------------------------------------------------------- #
# GTFS : lecture, classification, services actifs
# --------------------------------------------------------------------------- #
def read_gtfs_csv(z: zipfile.ZipFile, name: str, cols: list[str]) -> pd.DataFrame:
    """Lit une table GTFS (tout en texte), uniquement les colonnes demandées présentes."""
    if name not in z.namelist():
        return pd.DataFrame(columns=cols)
    with z.open(name) as f:
        return pd.read_csv(f, dtype=str, usecols=lambda c: c in cols, keep_default_na=False)


def classify_routes(routes: pd.DataFrame, modes: dict) -> pd.DataFrame:
    """Ajoute à routes : rt, mode, cls ('rail' | 'bus' | 'longdist' | None), line_name, line_key."""
    r = routes.copy()
    r["rt"] = pd.to_numeric(r["route_type"], errors="coerce")
    name = r["route_short_name"].str.strip()
    # nom de ligne : route_short_name ; à défaut route_long_name, puis route_id
    r["line_name"] = name.where(name != "", r["route_long_name"].str.strip())
    r["line_name"] = r["line_name"].where(r["line_name"] != "", r["route_id"])

    r["mode"] = None
    for label, spec in modes["rail_modes"].items():
        r.loc[in_types(r["rt"], spec), "mode"] = label
    is_rail = r["mode"].notna()
    is_bus = in_types(r["rt"], modes["bus"])

    ld = modes["long_distance"]
    is_ld = is_rail & (r["line_name"].str.startswith(tuple(ld["name_prefixes"]))
                       | r["rt"].isin(ld["types"]))
    r["cls"] = None
    r.loc[is_bus, "cls"] = "bus"
    r.loc[is_rail, "cls"] = "rail"
    r.loc[is_ld, "cls"] = "longdist"
    r.loc[r["cls"] == "bus", "mode"] = "bus"
    # LIGNE_CLE : (agence, mode, nom court). Plusieurs route_id de même nom -> 1 ligne.
    r["line_key"] = r["agency_id"].fillna("") + "|" + r["mode"].fillna("") + "|" + r["line_name"]
    return r


def active_services(cal: pd.DataFrame, cal_dates: pd.DataFrame, day: date) -> set[str]:
    """service_id actifs le jour `day` : calendar (jour + plage) puis calendar_dates (1 ajoute, 2 retire)."""
    ds = day.strftime("%Y%m%d")
    wd = WEEKDAYS[day.weekday()]
    ok = (cal[wd] == "1") & (cal["start_date"] <= ds) & (cal["end_date"] >= ds)
    active = set(cal.loc[ok, "service_id"])
    ex = cal_dates[cal_dates["date"] == ds]
    active |= set(ex.loc[ex["exception_type"] == "1", "service_id"])
    active -= set(ex.loc[ex["exception_type"] == "2", "service_id"])
    return active


def service_ratio(cal, cal_dates, trips, day: date) -> tuple[int, float, int]:
    """Nb de trajets le jour `day`, médiane des mêmes jours de semaine du feed, nb de ces jours."""
    per_service = trips["service_id"].value_counts()
    lo = datetime.strptime(cal["start_date"].min(), "%Y%m%d").date()
    hi = datetime.strptime(cal["end_date"].max(), "%Y%m%d").date()
    counts = {}
    for d in pd.date_range(lo, hi):
        if d.weekday() == day.weekday():
            s = active_services(cal, cal_dates, d.date())
            counts[d.date()] = int(per_service.reindex(list(s)).fillna(0).sum())
    med = float(np.median(list(counts.values())))
    return counts[day], med, len(counts)


# --------------------------------------------------------------------------- #
# GTFS : arrêts, balayage de stop_times
# --------------------------------------------------------------------------- #
def read_stops(z: zipfile.ZipFile) -> pd.DataFrame:
    """stops.txt + zone d'arrêt (`root` = ancêtre le plus haut, sinon l'arrêt lui-même)."""
    s = read_gtfs_csv(z, "stops.txt",
                      ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"])
    s["lat"] = pd.to_numeric(s["stop_lat"], errors="coerce")
    s["lon"] = pd.to_numeric(s["stop_lon"], errors="coerce")
    parent = dict(zip(s["stop_id"], s["parent_station"]))
    root = s["stop_id"].copy()
    for _ in range(3):                                  # hiérarchie : 3 niveaux au plus
        p = root.map(parent).fillna("")
        root = root.where(p == "", p)
    s["root"] = root
    return s


def scan_stop_times(z: zipfile.ZipFile, trip_ids: pd.Series, stop_ids: pd.Series, label: str) -> pd.DataFrame:
    """Une passe sur stop_times.txt (lecture par blocs, jamais le fichier entier en mémoire).

    - ne garde que les trajets actifs `trip_ids` ;
    - mémorise le stop_sequence MAXIMAL de chaque trajet actif (pour exclure le
      dernier arrêt), puis les lignes aux arrêts `stop_ids` (ceux des rayons).
    Renvoie les lignes aux arrêts, avec la colonne max_seq.
    """
    trip_set = pa.array(trip_ids.unique(), type=pa.string())
    stop_set = pa.array(stop_ids.unique(), type=pa.string())
    conv = pacsv.ConvertOptions(
        include_columns=ST_COLS,
        column_types={"trip_id": pa.string(), "arrival_time": pa.string(),
                      "departure_time": pa.string(), "stop_id": pa.string(),
                      "stop_sequence": pa.int32(), "pickup_type": pa.int32()})
    read = pacsv.ReadOptions(block_size=1 << 26)         # blocs de 64 Mio
    kept, maxseq, n_rows, n_active = [], [], 0, 0
    t0 = time.time()
    with z.open("stop_times.txt") as f:
        for i, batch in enumerate(pacsv.open_csv(f, read_options=read, convert_options=conv)):
            tbl = pa.Table.from_batches([batch])
            n_rows += tbl.num_rows
            tbl = tbl.filter(pc.is_in(tbl["trip_id"], value_set=trip_set))
            n_active += tbl.num_rows
            if tbl.num_rows == 0:
                continue
            maxseq.append(tbl.group_by("trip_id").aggregate([("stop_sequence", "max")]))
            kept.append(tbl.filter(pc.is_in(tbl["stop_id"], value_set=stop_set)))
            if i % 10 == 9:
                log.info("  %s : %.1f M lignes lues (%.0f s)", label, n_rows / 1e6, time.time() - t0)
    log.info("  %s : %.1f M lignes lues, %.1f M de trajets actifs (%.0f s)",
             label, n_rows / 1e6, n_active / 1e6, time.time() - t0)
    # un trajet peut chevaucher deux blocs : maximum des maxima partiels
    ms = pa.concat_tables(maxseq).group_by("trip_id").aggregate([("stop_sequence_max", "max")])
    ms = ms.to_pandas().rename(columns={"stop_sequence_max_max": "max_seq"})
    rows = pa.concat_tables(kept).to_pandas()
    return rows.merge(ms[["trip_id", "max_seq"]], on="trip_id", how="left")


def feed_rows(country: str, zpath: Path, day: date, communes: list[dict], r_max: float,
              modes: dict, work_dir: Path, anomalies: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Prépare un feed : arrêts dans les rayons, trajets actifs, lignes de stop_times utiles.

    Renvoie (rows, stops, info). `rows` : une ligne par passage à un arrêt d'un rayon,
    enrichie (zone d'arrêt, ligne, mode, classe, dep_s, is_dep).
    """
    log.info("[%s] lecture du GTFS %s", country, zpath.name)
    geod = Geod(ellps="WGS84")
    with zipfile.ZipFile(zpath) as z:
        routes = classify_routes(
            read_gtfs_csv(z, "routes.txt", ["route_id", "agency_id", "route_short_name",
                                            "route_long_name", "route_type"]), modes)
        trips = read_gtfs_csv(z, "trips.txt", ["route_id", "service_id", "trip_id"])
        cal = read_gtfs_csv(z, "calendar.txt", ["service_id", *WEEKDAYS, "start_date", "end_date"])
        cal_dates = read_gtfs_csv(z, "calendar_dates.txt", ["service_id", "date", "exception_type"])
        stops = read_stops(z)

        # Services actifs et plausibilité du jour d'analyse
        services = active_services(cal, cal_dates, day)
        n_day, n_med, n_same = service_ratio(cal, cal_dates, trips, day)
        info = dict(date=day, weekday=WEEKDAYS[day.weekday()], n_services=len(services),
                    n_trips_day=n_day, n_trips_median=n_med, n_same_weekdays=n_same,
                    feed_start=cal["start_date"].min(), feed_end=cal["end_date"].max())
        if not (info["feed_start"] <= day.strftime("%Y%m%d") <= info["feed_end"]):
            raise RuntimeError(f"[{country}] {day} hors de la validité du GTFS "
                               f"({info['feed_start']} → {info['feed_end']})")

        # Arrêts dans le rayon maximal de chaque commune (distance géodésique)
        ok = stops["lat"].notna() & stops["lon"].notna()
        sx = stops.loc[ok]
        per_commune = {}
        for c in communes:
            d = geod_km(geod, c["lon"], c["lat"], sx["lon"].to_numpy(), sx["lat"].to_numpy())
            near = sx.loc[d <= r_max, ["stop_id"]].assign(dist_km=d[d <= r_max])
            per_commune[c["code"]] = near.set_index("stop_id")["dist_km"]
            log.info("  %s : %d arrêts GTFS dans %.2f km", c["name"], len(near), r_max)
        near_ids = pd.Series(sorted(set().union(*[set(v.index) for v in per_commune.values()])))

        # Trajets actifs de classe utile (ferré, bus, grand-ligne), puis une passe sur stop_times
        t = trips[trips["service_id"].isin(services)].merge(
            routes[["route_id", "agency_id", "cls", "mode", "line_name", "line_key"]],
            on="route_id", how="inner")
        t = t[t["cls"].notna()]
        info["n_trips_used"] = len(t)

        # Cache : le balayage de stop_times est la seule étape longue
        key = hashlib.sha256(json.dumps([zpath.stat().st_size, zpath.stat().st_mtime, str(day),
                                         list(near_ids), len(t)]).encode()).hexdigest()[:16]
        cache, meta = work_dir / f"stop_rows_{country}.parquet", work_dir / f"stop_rows_{country}.json"
        if cache.exists() and meta.exists() and json.loads(meta.read_text())["key"] == key:
            log.info("[%s] lignes stop_times relues depuis le cache %s", country, cache.name)
            rows = pd.read_parquet(cache)
        else:
            rows = scan_stop_times(z, t["trip_id"], near_ids, country)
            rows.to_parquet(cache)
            meta.write_text(json.dumps({"key": key}))

    # Enrichissement
    rows = rows.merge(t[["trip_id", "agency_id", "route_id", "cls", "mode", "line_name", "line_key"]],
                      on="trip_id", how="left")
    rows = rows.merge(stops[["stop_id", "stop_name", "root"]], on="stop_id", how="left")
    dep_s = series_to_s(rows["departure_time"])
    arr_s = series_to_s(rows["arrival_time"])
    info["n_rows_no_time"] = int(dep_s.isna().sum())
    rows["dep_s"] = dep_s.fillna(arr_s)                    # repli : heure d'arrivée
    info["n_rows_no_time_at_all"] = int(rows["dep_s"].isna().sum())
    rows["is_dep"] = rows["stop_sequence"] < rows["max_seq"]   # hors dernier arrêt du trajet
    info["stop_names"] = dict(zip(stops["stop_id"], stops["stop_name"]))
    if info["n_rows_no_time_at_all"]:
        anomalies.append(f"[{country}] {info['n_rows_no_time_at_all']} passages sans heure "
                         "(ni départ ni arrivée) ignorés pour le fenêtrage.")
    # distances (commune, arrêt) : table longue pour la jointure par commune
    dist = pd.concat(per_commune, names=["code", "stop_id"]).rename("dist_km").reset_index()
    return rows, dist, info


# --------------------------------------------------------------------------- #
# Indicateurs de desserte
# --------------------------------------------------------------------------- #
def station_table(rail_dep: pd.DataFrame, names: dict, win: tuple[int, int]) -> pd.DataFrame:
    """Une ligne par zone d'arrêt ferrée desservie (jour entier), avec distance min et départs."""
    if rail_dep.empty:
        return pd.DataFrame(columns=["station_id", "station_name", "dist_km", "lines",
                                     "departures_day", "departures_window", "served_stop_name"])
    inwin = rail_dep["dep_s"].between(*win)
    g = rail_dep.assign(inwin=inwin).groupby("root")
    out = pd.DataFrame({
        "dist_km": g["dist_km"].min(),
        "lines": g["line_name"].agg(lambda s: ", ".join(sorted(set(s), key=natural_key))),
        "departures_day": g.size(),
        "departures_window": g["inwin"].sum().astype(int),
        "served_stop_name": g["stop_name"].agg(lambda s: s.mode().iloc[0]),   # nom du quai le plus fréquent
    })
    out["station_name"] = [names.get(i, rail_dep.loc[rail_dep.root == i, "stop_name"].iloc[0])
                           for i in out.index]
    return out.rename_axis("station_id").reset_index().sort_values("dist_km")


def line_table(rail_dep: pd.DataFrame, win: tuple[int, int]) -> pd.DataFrame:
    """Une ligne par ligne ferrée (agence, mode, nom court) : route_id regroupés, gares, départs."""
    if rail_dep.empty:
        return pd.DataFrame(columns=["line_key", "agency_id", "mode", "line_name", "route_ids",
                                     "stations", "departures_day", "departures_window"])
    g = rail_dep.assign(inwin=rail_dep["dep_s"].between(*win)).groupby("line_key")
    out = pd.DataFrame({
        "agency_id": g["agency_id"].first(), "mode": g["mode"].first(), "line_name": g["line_name"].first(),
        "route_ids": g["route_id"].agg(lambda s: ", ".join(sorted(set(s)))),
        "stations": g["root"].nunique(),
        "departures_day": g.size(), "departures_window": g["inwin"].sum().astype(int),
    })
    return out.reset_index().sort_values("line_name", key=lambda s: s.map(natural_key))


def indicators(rows_c: pd.DataFrame, r: float, win: tuple[int, int]) -> dict:
    """Indicateurs de desserte pour un rayon r (km) ; rows_c = passages aux arrêts du rayon max."""
    sub = rows_c[(rows_c["dist_km"] <= r) & rows_c["is_dep"]]
    rail, bus = sub[sub["cls"] == "rail"], sub[sub["cls"] == "bus"]
    rail_w = rail[rail["dep_s"].between(*win)]
    bus_w = bus[bus["dep_s"].between(*win)]
    return dict(
        rail_stations_catchment=rail["root"].nunique(),
        rail_lines_catchment=rail["line_key"].nunique(),
        rail_departures_catchment=len(rail_w),
        bus_departures_catchment=len(bus_w),
        rail_stations_window=rail_w["root"].nunique(),
        rail_lines_window=rail_w["line_key"].nunique(),
        rail_lines_list=", ".join(sorted(set(rail["line_name"]), key=natural_key)),
        n_line_names=rail["line_name"].nunique(),
    )


# --------------------------------------------------------------------------- #
# Similarité
# --------------------------------------------------------------------------- #
def add_similarity(df: pd.DataFrame, metrics: list[str]) -> pd.DataFrame:
    """|log(candidate/Garches)| par métrique, moyenne = similarity_score (petit = proche)."""
    df = df.copy()
    ref = df.iloc[0]                                      # Garches est toujours en tête
    flags = [[] for _ in range(len(df))]
    for m in metrics:
        vals = []
        for i, v in enumerate(df[m]):
            g = ref[m]
            if i == 0:
                vals.append(np.nan)
            elif v <= 0 or g <= 0:                        # valeur nulle -> log((v+1)/(g+1))
                vals.append(abs(np.log((v + 1) / (g + 1))))
                flags[i].append(m)
            else:
                vals.append(abs(np.log(v / g)))
        df[f"sim_{m}"] = vals
    df["zero_adjusted_metrics"] = [", ".join(f) for f in flags]
    df["similarity_score"] = df[[f"sim_{m}" for m in metrics]].mean(axis=1)
    df["rank"] = df["similarity_score"].rank(method="min").astype("Int64")
    return df


# --------------------------------------------------------------------------- #
# Sorties
# --------------------------------------------------------------------------- #
def md(x) -> str:
    """Texte sûr dans une cellule de tableau markdown (le « | » des noms d'arrêts GTFS)."""
    return str(x).replace("|", "\\|")


def pl(n: int, sing: str, plur: str) -> str:
    return f"{n} {sing if n <= 1 else plur}"


def sentence(row: pd.Series, g: pd.Series, core_names: dict, r: float, w0: str, w1: str) -> str:
    """Une phrase factuelle par commune (rapports à Garches), sans conclure sur le choix."""
    def x(v, ref_v):
        return "n/c" if ref_v == 0 else f"×{fr_num(v / ref_v, 2)}"
    lst = f" ({row.rail_lines_list})" if row.rail_lines_list else ""
    if row["role"].startswith("Garches"):
        return (f"Garches (référence) : {fr_num(row.population)} hab., densité {fr_num(row.density_per_km2)} hab./km², "
                f"à {fr_num(row.dist_core_km, 1)} km de {core_names['FR']} ; dans {fr_num(r, 1)} km : "
                f"{pl(row.rail_stations_catchment, 'gare ferrée', 'gares ferrées')} et "
                f"{pl(row.rail_lines_catchment, 'ligne ferrée', 'lignes ferrées')}{lst}, "
                f"{row.rail_departures_catchment} départs ferrés et {row.bus_departures_catchment} départs de bus "
                f"entre {w0} et {w1}.")
    zero = (f" ; valeur nulle traitée en log((v+1)/(g+1)) pour : {row.zero_adjusted_metrics}"
            if row.zero_adjusted_metrics else "")
    return (f"{row['name']} : {fr_num(row.population)} hab. ({x(row.population, g.population)} Garches), "
            f"densité {fr_num(row.density_per_km2)} hab./km² ({x(row.density_per_km2, g.density_per_km2)}), "
            f"à {fr_num(row.dist_core_km, 1)} km de {core_names[row.country]} "
            f"(Garches : {fr_num(g.dist_core_km, 1)} km de {core_names['FR']}) ; dans {fr_num(r, 1)} km : "
            f"{pl(row.rail_stations_catchment, 'gare ferrée', 'gares ferrées')} (Garches : {g.rail_stations_catchment}) et "
            f"{pl(row.rail_lines_catchment, 'ligne ferrée', 'lignes ferrées')}{lst} (Garches : {g.rail_lines_catchment}), "
            f"{row.rail_departures_catchment} départs ferrés (Garches : {g.rail_departures_catchment}) et "
            f"{row.bus_departures_catchment} départs de bus (Garches : {g.bus_departures_catchment}) "
            f"entre {w0} et {w1} ; score de similarité {fr_num(row.similarity_score, 2)}, "
            f"rang {row['rank']}{zero}.")


def write_markdown(path: Path, df: pd.DataFrame, sens: pd.DataFrame, stations: pd.DataFrame,
                   longdist: dict, anomalies: list[str], ctx: dict) -> None:
    r, win_txt, w0, w1 = ctx["radius"], ctx["win_txt"], ctx["w0"], ctx["w1"]
    L = []
    L.append("# Choix de la commune jumelle — indicateurs comparés\n")
    L.append(f"*Généré par `pipeline/03_select_twin_city.py` le {datetime.now():%d/%m/%Y %H:%M}. "
             "Aucun choix n'est fait ici : ce sont des chiffres comparables, identiques pour toutes les communes.*\n")
    L.append("## Paramètres\n")
    L.append(f"- Rayon de chalandise : **{fr_num(r, 1)} km** autour du point de la commune "
             "(chef-lieu IGN pour Garches ; « Kern der Gemeinde » VG250 pour les candidates), distance géodésique WGS84.")
    L.append(f"- Fenêtre de départs : **{win_txt}** (bornes incluses) ; une heure GTFS ≥ 24:00 (ex. 31:00) est hors fenêtre.")
    for cc, inf in ctx["feeds"].items():
        L.append(f"- Jour d'analyse {cc} : **{inf['date']:%d/%m/%Y}** ({FR_WEEKDAYS[inf['weekday']]}) ; GTFS valide "
                 f"{inf['feed_start']} → {inf['feed_end']} ; {fr_num(inf['n_trips_day'])} trajets ce jour "
                 f"(médiane des {inf['n_same_weekdays']} mêmes jours de semaine du feed : "
                 f"{fr_num(inf['n_trips_median'])}, rapport {fr_num(inf['n_trips_day'] / inf['n_trips_median'], 3)}).")
    L.append("- Ferré lourd ou guidé : route_type " + ctx["modes_txt"] + " ; " + ctx["ld_fr"] + ".")
    L.append("- Ligne = (agence, mode, `route_short_name`) : plusieurs `route_id` de même nom = une seule ligne.")
    L.append("- Départ = ligne de `stop_times` d'un trajet actif ce jour-là, hors dernier arrêt du trajet, dans la fenêtre. "
             "Gares et lignes (colonnes « jour ») : desservies à n'importe quelle heure du jour ; "
             "entre parenthèses, la même chose restreinte à la fenêtre.")
    L.append("- Score de similarité = moyenne des |log(candidate / Garches)| sur : " +
             ", ".join(f"`{m}`" for m in ctx["metrics"]) + " (plus petit = plus proche).\n")

    L.append("## Tableau\n")
    hdr = ["Rang", "Commune", "Pays", "Habitants", "Surface km²", "Densité /km²", "Dist. centre km",
           "Gares ferrées jour (fenêtre)", "Lignes ferrées jour (fenêtre)", f"Départs ferrés {win_txt}",
           f"Départs bus {win_txt}", "Lignes", "Score"]
    L.append("| " + " | ".join(hdr) + " |")
    L.append("|" + "|".join(["---"] * 3 + ["---:"] * 8 + ["---", "---:"]) + "|")
    for _, v in df.iterrows():
        L.append("| " + " | ".join([
            "réf." if pd.isna(v["rank"]) else str(v["rank"]), md(v["name"]), v["country"],
            fr_num(v.population), fr_num(v.area_km2, 2), fr_num(v.density_per_km2),
            fr_num(v.dist_core_km, 1),
            f"{v.rail_stations_catchment} ({v.rail_stations_window})",
            f"{v.rail_lines_catchment} ({v.rail_lines_window})",
            str(v.rail_departures_catchment), str(v.bus_departures_catchment),
            md(v.rail_lines_list or "—"),
            "—" if pd.isna(v.similarity_score) else fr_num(v.similarity_score, 3)]) + " |")
    L.append("")
    L.append("Détail des écarts par métrique (|log|) : voir `twin_candidates.csv` (colonnes `sim_*`).\n")

    L.append("## Ce que disent les chiffres\n")
    g = df.iloc[0]
    for _, v in df.iterrows():
        L.append("- " + sentence(v, g, ctx["core_names"], r, w0, w1))
    L.append("")

    L.append("## Gares ferrées trouvées (dans le rayon et en bordure)\n")
    L.append(f"Distance = distance minimale d'un quai desservi au point de la commune ; "
             f"au-delà de {fr_num(r, 1)} km la gare n'est pas comptée.\n")
    L.append("| Commune | Gare (zone d'arrêt) | Dist. km | Lignes | Dép. fenêtre | Dép. jour | Dans le rayon |")
    L.append("|---|---|---:|---|---:|---:|---|")
    for _, s in stations.iterrows():
        L.append(f"| {md(s['commune'])} | {md(s.station_name)} | {fr_num(s.dist_km, 2)} | {md(s.lines)} | "
                 f"{s.departures_window} | {s.departures_day} | {'oui' if s.dist_km <= r else 'non'} |")
    L.append("")

    L.append("## Grand-ligne exclu (passages dans le rayon, jour entier)\n")
    for name, txt in longdist.items():
        L.append(f"- **{md(name)}** : {txt}")
    L.append("")

    L.append("## Sensibilité au rayon\n")
    L.append(f"Mêmes indicateurs avec un rayon de ± {ctx['sens_pct']} % ; score et rang sont recalculés à chaque rayon "
             "(Garches au même rayon ; population et distance au centre inchangées).\n")
    L.append("| Commune | Rayon km | Gares | Lignes | Dép. ferrés | Dép. bus | Score | Rang |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for _, s in sens.iterrows():
        L.append(f"| {md(s['name'])} | {fr_num(s.radius_km, 2)} | {s.rail_stations_catchment} | "
                 f"{s.rail_lines_catchment} | {s.rail_departures_catchment} | {s.bus_departures_catchment} | "
                 f"{'—' if pd.isna(s.similarity_score) else fr_num(s.similarity_score, 3)} | "
                 f"{'réf.' if pd.isna(s['rank']) else s['rank']} |")
    L.append("")

    L.append("## Anomalies et points d'attention\n")
    for a in anomalies or ["Aucune anomalie détectée."]:
        L.append(f"- {a}")
    L.append("")
    path.write_text("\n".join(L), encoding="utf-8")


def make_figure(path: Path, df: pd.DataFrame, ctx: dict) -> None:
    """Petits multiples : une barre par commune, Garches mise en évidence, titres en allemand."""
    r, w0, w1 = ctx["radius"], ctx["w0"], ctx["w1"]
    panels = [
        ("population", "Einwohner (Destatis 2024; Garches: INSEE 2023)", 0),
        ("dist_core_km", "Entfernung zum Zentrum (Luftlinie, km)", 1),
        ("rail_lines_catchment", f"Bahnlinien im Umkreis von {de_num(r, 1)} km", 0),
        ("rail_departures_catchment", f"Bahnabfahrten {w0}–{w1} im Umkreis von {de_num(r, 1)} km", 0),
    ]
    accent, neutral, ink, muted = "#c2410c", "#8aa0b8", "#1f2937", "#6b7280"
    fig, axes = plt.subplots(2, 2, figsize=(12.5, 8.2))
    for ax, (col, title, nd) in zip(axes.ravel(), panels):
        d = df.sort_values(col, ascending=True)
        is_ref = d["role"].str.startswith("Garches").to_numpy()
        y = np.arange(len(d))
        ax.barh(y, d[col], height=0.62, color=np.where(is_ref, accent, neutral), zorder=3)
        ax.set_yticks(y)
        ax.set_yticklabels([f"{n} (FR)" if c == "FR" else n for n, c in zip(d["name"], d["country"])],
                           fontsize=9.5, color=ink)
        for lab, ref_flag in zip(ax.get_yticklabels(), is_ref):
            if ref_flag:
                lab.set_fontweight("bold")
        top = d[col].max() if d[col].max() > 0 else 1
        for yi, v in zip(y, d[col]):
            ax.text(v + top * 0.012, yi, de_num(v, nd), va="center", fontsize=9, color=ink, zorder=4)
        ax.set_xlim(0, top * 1.16)
        ax.set_title(title, loc="left", fontsize=11, color=ink, fontweight="bold")
        ax.grid(axis="x", color="#e5e7eb", linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)
        ax.tick_params(axis="x", labelsize=8.5, colors=muted, length=0)
        ax.tick_params(axis="y", length=0)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.spines["bottom"].set_color("#d1d5db")
        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: de_num(v, 0)))
    fig.suptitle("So nah, so fern – Garches und Kandidatinnen im Vergleich", x=0.01, ha="left",
                 fontsize=14, fontweight="bold", color=ink)
    fig.text(0.01, 0.925, "Garches (orange) = Referenz. Gleiche Kennzahlen für alle Gemeinden; "
             "Schiene = Tram, U-/S-Bahn, Regionalzug, ohne Fernverkehr (ICE, IC, EC …).",
             fontsize=9.5, color=muted, ha="left")
    fig.text(0.01, 0.012, ctx["foot_de"], fontsize=8, color=muted, ha="left")
    fig.tight_layout(rect=(0, 0.03, 1, 0.915), h_pad=2.2, w_pad=3)
    fig.savefig(path, dpi=160)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Programme principal
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Indicateurs comparés Garches / communes candidates DE")
    ap.add_argument("--config", required=True, help="ex. config/twin_selection.yaml")
    args = ap.parse_args()
    cfg_path = Path(args.config)
    cfg_path = cfg_path if cfg_path.is_absolute() else ROOT / cfg_path
    tw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    ref = load_config(tw["reference_config"])             # Garches + default.yaml + sources

    out_dir = ROOT / "outputs" / OUT_SLUG
    work_dir = ROOT / "data" / "interim" / OUT_SLUG
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    pseudo = {"origin": {"slug": OUT_SLUG}}               # pour log_assumption -> outputs/twin_selection/

    radius = float(tw["station_catchment_km"])
    sens_pct = float(tw.get("catchment_sensitivity_pct", SENSIBILITE_PCT_DEFAUT))
    radii = sorted({round(radius * (1 - sens_pct / 100), 4), radius, round(radius * (1 + sens_pct / 100), 4)})
    r_max = max(radii)
    w0, w1 = ref["analysis"]["window_start"], ref["analysis"]["window_end"]
    win = (hms_to_s(w0), hms_to_s(w1))
    win_txt = f"{w0}–{w1}"
    modes = tw.get("transit_modes", MODES_DEFAUT)
    metrics = list(tw["similarity_metrics"])
    anomalies: list[str] = []
    day_fr, day_de = to_date(ref["analysis"]["date"]), to_date(tw["de"]["date"])

    # ---- Communes : population, surface, points --------------------------------
    fr_commune, fr_core = load_fr(ref, anomalies)
    de_communes, de_core = load_de(ref, tw, work_dir)
    communes = [fr_commune] + de_communes
    geod = Geod(ellps="WGS84")
    core_for = {"FR": fr_core, "DE": de_core}
    for c in communes:
        k = core_for[c["country"]]
        c["dist_core_km"] = float(geod_km(geod, k["lon"], k["lat"], np.array([c["lon"]]), np.array([c["lat"]]))[0])
        c["density_per_km2"] = c["population"] / c["area_km2"]
    log.info("Communes chargées : %s", ", ".join(c["name"] for c in communes))

    # ---- Desserte : un balayage de stop_times par pays -----------------------------
    feeds, rows_by_country, dist_by_country = {}, {}, {}
    for cc, day, src in (("FR", day_fr, ref["inputs"]["gtfs"][0]), ("DE", day_de, tw["de"]["gtfs"])):
        cs = [c for c in communes if c["country"] == cc]
        rows, dist, info = feed_rows(cc, source_path(ref, src), day, cs, r_max, modes, work_dir, anomalies)
        rows_by_country[cc], dist_by_country[cc], feeds[cc] = rows, dist, info
        ratio = info["n_trips_day"] / info["n_trips_median"]
        if ratio < float(ref["analysis"]["min_service_ratio"]):
            anomalies.append(
                f"[{cc}] {day} : {info['n_trips_day']} trajets, soit {ratio:.3f} × la médiane des mêmes jours "
                f"de semaine du feed ({info['n_trips_median']:.0f}) — sous min_service_ratio "
                f"({ref['analysis']['min_service_ratio']}) ; le feed peut être incomplet ou le jour atypique.")

    if feeds["FR"]["weekday"] != feeds["DE"]["weekday"]:
        anomalies.append(f"Les jours d'analyse n'ont pas le même jour de semaine : FR {day_fr} "
                         f"({FR_WEEKDAYS[feeds['FR']['weekday']]}), DE {day_de} ({FR_WEEKDAYS[feeds['DE']['weekday']]}).")

    # ---- Indicateurs par commune et par rayon ---------------------------------------
    results, sens_rows, st_tables, ln_tables, longdist = [], [], [], [], {}
    for c in communes:
        cc = c["country"]
        d = dist_by_country[cc]
        d = d[d["code"] == c["code"]][["stop_id", "dist_km"]]
        rows_c = rows_by_country[cc].merge(d, on="stop_id", how="inner")   # passages aux arrêts du rayon max
        names = feeds[cc]["stop_names"]
        base = indicators(rows_c, radius, win)

        # Stations et lignes (détail) sur le rayon max, pour repérer les gares en bordure
        dep = rows_c[rows_c["is_dep"]]
        rail_all = dep[dep["cls"] == "rail"]
        st = station_table(rail_all, names, win)
        ln = line_table(rail_all[rail_all["dist_km"] <= radius], win)
        st.insert(0, "commune", c["name"])
        ln.insert(0, "commune", c["name"])
        st_tables.append(st)
        ln_tables.append(ln)

        stl = st[st["dist_km"] <= radius]
        base["rail_stations_list"] = "; ".join(f"{n} ({dk:.1f} km)" for n, dk in zip(stl.station_name, stl.dist_km))

        # --- contrôles de cohérence propres à la commune
        rail_in = rail_all[rail_all["dist_km"] <= radius]
        n_route = rail_in["route_id"].nunique()
        if n_route != base["rail_lines_catchment"]:
            anomalies.append(f"{c['name']} : {n_route} route_id ferrés regroupés en "
                             f"{base['rail_lines_catchment']} ligne(s) (agence, mode, nom court) ; "
                             f"détail dans twin_candidates_rail_lines.csv.")
        if base["n_line_names"] != base["rail_lines_catchment"]:
            anomalies.append(f"{c['name']} : {base['rail_lines_catchment']} lignes (agence, mode, nom) mais "
                             f"{base['n_line_names']} noms distincts — un même nom existe pour plusieurs agences/modes.")
        # Doublons possibles : plusieurs zones d'arrêt de même nom (nom de zone sans suffixe
        # « | … » ou nom du quai desservi) = un seul lieu compté plusieurs fois.
        n_extra = 0
        for label, key in (("nom de zone", stl["station_name"].str.replace(r"\s*\|.*$", "", regex=True)),
                           ("nom du quai", stl["served_stop_name"])):
            cnt = stl.groupby(key.str.lower().str.strip())["station_id"].nunique()
            for nm in cnt[cnt > 1].index:
                grp = stl[key.str.lower().str.strip() == nm]
                n_extra = max(n_extra, len(grp) - 1)
                anomalies.append(
                    f"{c['name']} : « {grp['served_stop_name'].iloc[0]} » correspond à {len(grp)} zones d'arrêt "
                    f"distinctes ({', '.join(grp.station_id)} ; {label} identique ; distances au point : "
                    f"{' et '.join(fr_num(v, 3) for v in grp.dist_km)} km) — possible double comptage : "
                    f"{base['rail_stations_catchment'] - n_extra} gare(s) si fusionnées au lieu de "
                    f"{base['rail_stations_catchment']} (non fusionnées dans le CSV, définition = zones d'arrêt distinctes).")
                break
            if n_extra:
                break
        # Lignes présentes dans la journée mais sans aucun départ dans la fenêtre
        rail_day = set(rail_in["line_name"])
        rail_win = set(rail_in.loc[rail_in["dep_s"].between(*win), "line_name"])
        if rail_day - rail_win:
            anomalies.append(f"{c['name']} : {len(rail_day)} lignes dans la journée mais {len(rail_win)} dans la fenêtre "
                             f"{win_txt} (sans départ dans la fenêtre : "
                             f"{', '.join(sorted(rail_day - rail_win, key=natural_key))}). "
                             "La métrique `rail_lines_catchment` suit la définition « ce jour-là » (jour entier).")
        edge = st[(st["dist_km"] > radii[0]) & (st["dist_km"] <= radii[-1])]
        if len(edge):
            anomalies.append(f"{c['name']} : gare(s) ferrée(s) en bordure du rayon ({fr_num(radius, 1)} km ± "
                             f"{fr_num(sens_pct)} %) : " + "; ".join(
                f"{n} {fr_num(dk, 2)} km ({'dedans' if dk <= radius else 'dehors'})"
                for n, dk in zip(edge.station_name, edge.dist_km)) + ".")
        if base["rail_stations_catchment"] == 0:
            nearest = st["dist_km"].min() if len(st) else None
            anomalies.append(f"{c['name']} : aucune gare ferrée dans {fr_num(radius, 1)} km"
                             + (f" (la plus proche trouvée : {fr_num(nearest, 2)} km)." if nearest is not None
                                else f" ni dans {fr_num(r_max, 2)} km."))
        n_nopick = int(((dep["cls"].isin(["rail", "bus"])) & (dep["dist_km"] <= radius)
                        & dep["dep_s"].between(*win) & (dep["pickup_type"] == 1)).sum())
        if n_nopick:
            anomalies.append(f"{c['name']} : {n_nopick} départs de la fenêtre ont pickup_type = 1 "
                             "(montée interdite) ; ils sont comptés comme départs (définition demandée).")
        n_late = int(((dep["dist_km"] <= radius) & (dep["dep_s"] >= 86400)).sum())
        if n_late:
            log.info("%s : %d départs ≥ 24:00 dans le rayon (hors fenêtre).", c["name"], n_late)

        # --- grand-ligne exclu : passages recensés dans le rayon (jour entier)
        ld = dep[(dep["cls"] == "longdist") & (dep["dist_km"] <= radius)]
        if len(ld):
            tok = ld["line_name"].str.extract(r"^([A-Za-z]+)")[0].fillna(ld["line_name"])
            pref = tuple(modes["long_distance"]["name_prefixes"])
            parts = []
            for t_, grp in ld.groupby(tok):
                flag = "" if t_ in pref else " [préfixe non exact : à contrôler]"
                gs = ", ".join(sorted(grp["stop_name"].unique(), key=natural_key))
                parts.append(f"{t_} ({len(grp)} départs, {grp['line_name'].nunique()} nom(s) de ligne ; "
                             f"arrêts : {gs}){flag}")
            longdist[c["name"]] = "; ".join(parts)
            if any("[préfixe non exact" in p for p in parts):
                anomalies.append(f"{c['name']} : lignes exclues dont le nom ne commence pas exactement par un "
                                 "préfixe de la liste (ex. « ECM » exclu par « EC ») — voir section grand-ligne.")
        else:
            longdist[c["name"]] = "aucun passage grand-ligne dans le rayon."

        # --- sensibilité au rayon
        for rr in radii:
            s = base if rr == radius else indicators(rows_c, rr, win)
            sens_rows.append(dict(code=c["code"], name=c["name"], country=cc, radius_km=rr,
                                  **{k: s[k] for k in ("rail_stations_catchment", "rail_lines_catchment",
                                                       "rail_departures_catchment", "bus_departures_catchment")},
                                  population=c["population"], dist_core_km=c["dist_core_km"]))
        results.append({**c, **base})
        log.info("%s : %d gares, %d lignes (%s), %d dép. ferrés, %d dép. bus", c["name"],
                 base["rail_stations_catchment"], base["rail_lines_catchment"], base["rail_lines_list"],
                 base["rail_departures_catchment"], base["bus_departures_catchment"])

    # ---- Tableau final + similarité -----------------------------------------------
    df = pd.DataFrame(results)
    df["gtfs_date"] = [day_fr if c == "FR" else day_de for c in df["country"]]
    df = add_similarity(df, metrics)
    base_cols = ["code", "name", "country", "role", "population", "area_km2", "density_per_km2", "pop_source",
                 "dist_core_km", "gtfs_date", "rail_stations_catchment", "rail_lines_catchment",
                 "rail_departures_catchment", "bus_departures_catchment", "rail_lines_list",
                 "rail_stations_list", "rail_stations_window", "rail_lines_window"]
    sim_cols = [f"sim_{m}" for m in metrics] + ["zero_adjusted_metrics", "similarity_score", "rank"]
    csv = df[base_cols + sim_cols].rename(columns={"code": "commune_id"}).copy()
    for col, nd in (("area_km2", 3), ("density_per_km2", 1), ("dist_core_km", 3)):
        csv[col] = csv[col].round(nd)
    for col in [c for c in csv.columns if c.startswith("sim_")] + ["similarity_score"]:
        csv[col] = csv[col].round(4)
    csv.to_csv(out_dir / "twin_candidates.csv", index=False, encoding="utf-8")

    # Sensibilité : score et rang recalculés à chaque rayon (Garches au même rayon)
    sens = []
    for rr in radii:
        sub = pd.DataFrame([s for s in sens_rows if s["radius_km"] == rr])
        sens.append(add_similarity(sub, metrics)[["code", "name", "country", "radius_km",
                                                  "rail_stations_catchment", "rail_lines_catchment",
                                                  "rail_departures_catchment", "bus_departures_catchment",
                                                  "similarity_score", "rank"]])
    sens = pd.concat(sens, ignore_index=True)
    sens.round({"similarity_score": 4}).to_csv(out_dir / "twin_candidates_sensitivity.csv", index=False)

    stations = pd.concat(st_tables, ignore_index=True)
    stations.round({"dist_km": 3}).to_csv(out_dir / "twin_candidates_rail_stations.csv", index=False)
    pd.concat(ln_tables, ignore_index=True).to_csv(out_dir / "twin_candidates_rail_lines.csv", index=False)

    # Stabilité du classement selon le rayon
    firsts = []
    for rr in radii:
        sr = sens[(sens["radius_km"] == rr) & sens["rank"].notna()].sort_values("rank")
        firsts.append(f"{fr_num(rr, 2)} km → " + " > ".join(f"{n} ({fr_num(v, 2)})" for n, v in
                                                          zip(sr["name"], sr["similarity_score"])))
    order = [tuple(sens[(sens["radius_km"] == rr) & sens["rank"].notna()].sort_values("rank")["code"])
             for rr in radii]
    if len(set(order)) > 1:
        anomalies.append("Le classement par score de similarité dépend du rayon de chalandise : " + " ; ".join(firsts) + ".")
    else:
        anomalies.append("Le classement par score de similarité est identique pour tous les rayons testés : "
                         + " ; ".join(firsts) + ".")

    mt = []
    for lab, spec in modes["rail_modes"].items():
        mt.append(f"{lab} = " + ", ".join([str(t) for t in spec.get("types", [])]
                                           + [f"{a}–{b}" for a, b in spec.get("ranges", [])]))
    ctx = dict(radius=radius, win_txt=win_txt, w0=w0, w1=w1, feeds=feeds, metrics=metrics, sens_pct=fr_num(sens_pct),
               core_names={"FR": fr_core["name"], "DE": de_core["name"]},
               modes_txt="; ".join(mt),
               ld_fr="grand-ligne exclu : noms commençant par " + ", ".join(modes["long_distance"]["name_prefixes"])
               + " ou route_type " + ", ".join(map(str, modes["long_distance"]["types"])),
               ld_de="Fernverkehr ausgeschlossen: Namen mit Präfix " + ", ".join(modes["long_distance"]["name_prefixes"])
               + " oder route_type " + ", ".join(map(str, modes["long_distance"]["types"])),
               foot_de=(f"Quellen: IGN Admin Express / INSEE (FR), BKG VG250-EW / Destatis (DE), GTFS IDFM und gtfs.de; "
                        f"Stichtage {day_fr:%d.%m.%Y} (FR) und {day_de:%d.%m.%Y} (DE); Umkreis {de_num(radius, 1)} km "
                        f"um den Gemeindepunkt."))
    write_markdown(out_dir / "twin_candidates.md", df, sens, stations, longdist, anomalies, ctx)
    make_figure(out_dir / "twin_candidates.png", df, ctx)

    # ---- Hypothèses journalisées -------------------------------------------------------
    log_assumption(pseudo, "03_select_twin_city", "T1_catchment",
                   f"Desserte comptée dans un rayon de {radius} km (distance géodésique) autour du point de la commune.",
                   f"Angebot im Umkreis von {radius} km (geodätisch) um den Gemeindepunkt gezählt.", radius)
    log_assumption(pseudo, "03_select_twin_city", "T2_modes",
                   f"Ferré lourd ou guidé : route_type {ctx['modes_txt']} ; {ctx['ld_fr']} ; bus : route_type "
                   + ", ".join([str(t) for t in modes["bus"]["types"]] + [f"{a}–{b}" for a, b in modes["bus"]["ranges"]]) + ".",
                   f"Schiene: route_type {ctx['modes_txt']}; {ctx['ld_de']}; Bus: route_type "
                   + ", ".join([str(t) for t in modes["bus"]["types"]] + [f"{a}–{b}" for a, b in modes["bus"]["ranges"]]) + ".",
                   modes)
    log_assumption(pseudo, "03_select_twin_city", "T3_line_key",
                   "Une ligne = (agence, mode, route_short_name) : plusieurs route_id de même nom comptent pour une ligne.",
                   "Eine Linie = (Agentur, Modus, route_short_name): mehrere route_id mit gleichem Namen zählen einmal.")
    log_assumption(pseudo, "03_select_twin_city", "T4_departures",
                   f"Départs = lignes de stop_times hors dernier arrêt, jour de service actif, heure dans [{w0} ; {w1}] "
                   "(bornes incluses) ; heures ≥ 24:00 hors fenêtre.",
                   f"Abfahrten = stop_times ohne Endhalt, aktiver Betriebstag, Zeit in [{w0} ; {w1}] (inkl.); ≥ 24:00 außerhalb.",
                   [w0, w1])
    log_assumption(pseudo, "03_select_twin_city", "T5_dates",
                   f"Jours d'analyse : FR {day_fr}, DE {day_de}.", f"Stichtage: FR {day_fr}, DE {day_de}.",
                   [str(day_fr), str(day_de)])
    log.info("Terminé — sorties dans %s", out_dir.relative_to(ROOT))


if __name__ == "__main__":
    sys.exit(main())
