"""
Étape 01 — Unités d'analyse (destinations) du pipeline « So nah, so fern ».

Construit, pour l'origine donnée en --config, la table des unités géographiques
(communes de la région + découpage optionnel de la ville-centre en districts),
leurs points représentatifs, les distances à vol d'oiseau aux points d'origine
et un contrôle qualité des points contre les mairies OpenStreetMap.

    uv run python pipeline/01_prepare_units.py --config config/garches.yaml

Le script s'aiguille sur cfg["region"]["country"] (FR / DE). Aucune valeur
métier n'est écrite ici : rayon, tampon, codes communes, noms, sources et points
d'origine viennent de la configuration. Seules figurent en tête de fichier des
constantes de FORMAT des sources (noms de couches / colonnes) et deux conventions
méthodologiques (priorité des classes OSM `place`, seuil du contrôle H2).

Sorties (contrat des scripts suivants), dans processed_dir(cfg) :
  units.gpkg                 couches units_polygons et units_points (EPSG:4326)
  destinations.parquet       GeoParquet, un point par unité, distances et drapeaux
  origins.geojson            un point par entrée de origin.points
et dans outputs_dir(cfg) :
  qa_points_vs_osm_townhall.csv   contrôle H2 (points vs mairies OSM)
  assumptions.jsonl               hypothèses consignées (log_assumption)
"""
from __future__ import annotations

import json
import re
import subprocess
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyproj
import shapely
from shapely.geometry import Point, shape

from common import (ROOT, cli, get_logger, interim_dir, load_config, log_assumption,
                    osmium_bin, outputs_dir, processed_dir, source_path)

LOG = get_logger("01_prepare_units")

# --------------------------------------------------------------------------- #
# Constantes de format et conventions (PAS des paramètres d'analyse)
# --------------------------------------------------------------------------- #
WGS84 = "EPSG:4326"
GEOD = pyproj.Geod(ellps="WGS84")            # distances géodésiques, ellipsoïde WGS84

CENTRE_POINT_ID = "centre"                   # id du point d'origine qui définit dist_km_centre
INSEE_MEMBER = "donnees_communes.csv"        # fichier du zip INSEE (population municipale)

# VG250-EW (BKG) : couches et nom du gpkg dans le zip
VG250_GEM_LAYER, VG250_PK_LAYER = "vg250_gem", "vg250_pk"
UNZIP_ROOT = ROOT / "data" / "interim" / "_unzipped"

# Stadtteile de Francfort : préfixe des identifiants et regex « X (inkl. Y) »
DISTRICT_DE_PREFIX = "FFM-STT-"
INKL_RE = re.compile(r"^(?P<main>.+?)\s*\(inkl\.\s*(?P<parts>[^)]+)\)\s*$")
TOTAL_ROW_RE = re.compile(r"^\s*insgesamt\s*$", re.IGNORECASE)

# Convention : priorité des nœuds OSM `place` pour le point d'un Stadtteil
OSM_PLACE_PRIORITY = ["suburb", "quarter", "village", "town", "neighbourhood"]
# Contrôle H2 : seuil d'écart point / mairie OSM signalé (mètres)
QA_FAR_M = 500
# Contrôle de plausibilité des polygones des districts : distance max emprise / ville-centre (km)
SANITY_MAX_KM = 50
# Fusion de districts (core_city.merge_districts) : écart max entre les points des membres (m)
MERGE_POINT_TOL_M = 50
# Valeurs admises de analysis.destination_point
DEST_POINT_MODES = ("official", "osm_townhall")

UNIT_COLS = ["unit_id", "name", "level", "parent_id", "population", "pop_source",
             "pop_ref_date", "area_km2"]


# --------------------------------------------------------------------------- #
# Petits outils
# --------------------------------------------------------------------------- #
def norm(text: str) -> str:
    """Normalise un nom pour comparaison : casse, tirets, espaces, ponctuation ignorés."""
    t = unicodedata.normalize("NFKC", str(text)).casefold()
    return re.sub(r"[\W_]+", "", t)


def year_in(text: str) -> int:
    """Millésime (20xx) lu dans un nom de fichier / dossier de source."""
    m = re.search(r"(?<!\d)(20\d{2})(?!\d)", str(text))
    if not m:
        raise ValueError(f"millésime introuvable dans « {text} »")
    return int(m.group(1))


def fmt_int(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def metric_crs_of(cfg: dict) -> str:
    """CRS métrique : region.metric_crs, sinon celui du pays dans crs.metric_xx."""
    if cfg["region"].get("metric_crs"):
        return cfg["region"]["metric_crs"]
    return cfg["crs"]["metric_de" if cfg["region"]["country"] == "DE" else "metric_fr"]


def geod_km(lon1, lat1, lon2, lat2) -> np.ndarray:
    """Distance géodésique (WGS84), en km ; accepte scalaires et tableaux."""
    arrs = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (lon1, lat1, lon2, lat2)))
    _, _, dist_m = GEOD.inv(*arrs)
    dist_m = np.where((arrs[0] == arrs[2]) & (arrs[1] == arrs[3]), 0.0, dist_m)   # évite les 1e-13
    return np.atleast_1d(dist_m) / 1000.0


def rep_points(polys: gpd.GeoSeries, metric: str) -> gpd.GeoSeries:
    """Points représentatifs (toujours à l'intérieur du polygone), calculés en CRS métrique."""
    return polys.to_crs(metric).representative_point().to_crs(WGS84)


def to_wgs84(gdf):
    """Reprojette en EPSG:4326 (OGC:CRS84 = mêmes coordonnées lon/lat)."""
    if gdf.crs is None:
        raise ValueError("CRS manquant dans une source")
    return gdf.to_crs(WGS84)


def ensure_valid(gdf: gpd.GeoDataFrame, what: str) -> gpd.GeoDataFrame:
    bad = ~gdf.geometry.is_valid
    if bad.any():
        LOG.warning("%s : %d géométrie(s) invalide(s) réparée(s) (make_valid)", what, bad.sum())
        gdf = gdf.copy()
        gdf.loc[bad, "geometry"] = gdf.loc[bad, "geometry"].apply(shapely.make_valid)
    return gdf


# --------------------------------------------------------------------------- #
# OSM : extraits osmium (mairies, lieux-dits) lus en GeoDataFrame
# --------------------------------------------------------------------------- #
def osm_features(cfg: dict, kind: str, expressions: list[str], keys: list[str]) -> gpd.GeoDataFrame:
    """Extrait `expressions` (osmium tags-filter) de chaque pbf de inputs.osm, puis lit le
    résultat (osmium export -> geojsonseq).

    Les extraits sont mis en cache dans interim_dir(cfg) et réutilisés tant que le pbf
    source n'est pas plus récent. Les doublons entre pbf voisins (objets à cheval sur la
    limite d'un extrait Geofabrik) sont supprimés sur l'identifiant OSM.
    """
    frames = []
    for sid in cfg["inputs"]["osm"]:
        pbf = source_path(cfg, sid)
        stem = pbf.name.split(".")[0]
        filtered = interim_dir(cfg) / f"{kind}_{stem}.osm.pbf"
        seq = interim_dir(cfg) / f"{kind}_{stem}.geojsonseq"
        if not (seq.exists() and seq.stat().st_mtime >= pbf.stat().st_mtime):
            LOG.info("osmium : extraction %s depuis %s", kind, pbf.name)
            for cmd in (
                [osmium_bin(), "tags-filter", str(pbf), *expressions, "-o", str(filtered), "--overwrite"],
                [osmium_bin(), "export", str(filtered), "-f", "geojsonseq", "-u", "type_id",
                 "-o", str(seq), "--overwrite"],
            ):
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    raise RuntimeError(f"osmium a échoué ({' '.join(cmd[:2])}) : {res.stderr.strip()}")
        frames.append(read_geojsonseq(seq, keys))
    out = pd.concat(frames, ignore_index=True) if frames else read_geojsonseq(None, keys)
    out = gpd.GeoDataFrame(out, geometry="geometry", crs=WGS84)
    n0 = len(out)
    out = out.drop_duplicates("osm_id").reset_index(drop=True)
    LOG.info("OSM %s : %d objets (%d doublons inter-extraits écartés)", kind, len(out), n0 - len(out))
    return out


def read_geojsonseq(path, keys: list[str]) -> gpd.GeoDataFrame:
    """Lit un geojsonseq d'osmium (séparateur RS \\x1e possible) : id OSM, tags `keys`, géométrie."""
    rows, geoms = [], []
    if path is not None:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip().lstrip("\x1e").strip()
                if not line:
                    continue
                feat = json.loads(line)
                if not feat.get("geometry"):
                    continue
                props = feat.get("properties") or {}
                rows.append({"osm_id": feat.get("id"), **{k: props.get(k) for k in keys}})
                geoms.append(shape(feat["geometry"]))
    df = pd.DataFrame(rows, columns=["osm_id", *keys])
    return gpd.GeoDataFrame(df, geometry=gpd.GeoSeries(geoms, crs=WGS84), crs=WGS84)


# --------------------------------------------------------------------------- #
# Fusion de districts de la ville-centre (core_city.merge_districts)
# --------------------------------------------------------------------------- #
def merge_districts(cfg: dict, units: gpd.GeoDataFrame, core_code: str) -> tuple[gpd.GeoDataFrame, list]:
    """Fusionne des districts de la ville-centre en une seule unité (clé optionnelle de la config).

    Chaque groupe {unit_id, name, members} devient un district : géométrie = union des membres,
    population = somme, pop_source / pop_ref_date inchangés (identiques pour tous les membres),
    point = point du membre dont le polygone CONTIENT son propre point (les points des membres
    doivent être à moins de MERGE_POINT_TOL_M mètres les uns des autres, sinon erreur).
    Les districts hors groupe ne sont pas touchés.
    """
    groups = cfg["core_city"].get("merge_districts") or []
    if not groups:
        return units, []
    if not cfg["core_city"].get("split_into_districts"):
        raise ValueError("core_city.merge_districts exige core_city.split_into_districts: true")
    districts = units[(units["level"] == "district") & (units["parent_id"] == core_code)]
    seen, merged, info = set(), [], []
    for g in groups:
        gid, gname, members = str(g["unit_id"]), str(g["name"]), [str(m) for m in g["members"]]
        if len(set(members)) != len(members) or seen & set(members):
            raise ValueError(f"merge_districts « {gid} » : membre en double (dans le groupe ou entre groupes)")
        if gid in set(units["unit_id"]) - set(members):
            raise ValueError(f"merge_districts : unit_id « {gid} » déjà utilisé par une autre unité")
        rows = districts[districts["unit_id"].isin(members)].set_index("unit_id").loc[
            [m for m in members if m in set(districts["unit_id"])]]
        if len(rows) != len(members):
            raise ValueError(f"merge_districts « {gid} » : membres introuvables parmi les districts de "
                             f"{core_code} : {sorted(set(members) - set(rows.index))}")
        seen |= set(members)
        # tous les points des membres doivent être quasi confondus
        pts = list(rows["_pt"])
        spread_m = max(float(geod_km(a.x, a.y, b.x, b.y)[0]) * 1000 for a in pts for b in pts)
        if spread_m >= MERGE_POINT_TOL_M:
            raise ValueError(f"merge_districts « {gid} » : points des membres espacés de {spread_m:.0f} m "
                             f"(≥ {MERGE_POINT_TOL_M} m) — ce ne sont pas des districts à mairie commune")
        inside = [m for m in members if rows.loc[m, "geometry"].covers(rows.loc[m, "_pt"])]
        if not inside:
            raise ValueError(f"merge_districts « {gid} » : aucun membre ne contient son propre point")
        ref = rows.loc[inside[0]]
        for col in ("pop_source", "pop_ref_date"):
            if rows[col].nunique() != 1:
                raise ValueError(f"merge_districts « {gid} » : {col} différent selon les membres")
        merged.append({
            "unit_id": gid, "name": gname, "level": "district", "parent_id": core_code,
            "population": int(rows["population"].sum()), "pop_source": ref["pop_source"],
            "pop_ref_date": ref["pop_ref_date"], "point_source": ref["point_source"],
            "point_official": bool(ref["point_official"]), "_pt": ref["_pt"],
            "geometry": shapely.union_all(list(rows["geometry"]))})
        info.append({"unit_id": gid, "name": gname, "members": members,
                     "population": merged[-1]["population"], "point_member": inside[0],
                     "point_spread_m": round(spread_m, 1)})
        LOG.info("Districts fusionnés : %s (%s) = %s ; population %s ; point du membre %s (écart max "
                 "entre points : %.1f m)", gname, gid, "+".join(members), fmt_int(merged[-1]["population"]),
                 inside[0], spread_m)
    out = pd.concat([units[~units["unit_id"].isin(seen)],
                     gpd.GeoDataFrame(merged, geometry="geometry", crs=WGS84)], ignore_index=True)
    return gpd.GeoDataFrame(out, geometry="geometry", crs=WGS84), info


# --------------------------------------------------------------------------- #
# Branche FR (Admin Express + INSEE)
# --------------------------------------------------------------------------- #
def read_insee_pmun(cfg: dict) -> tuple[pd.Series, int]:
    """Population municipale INSEE (PMUN) indexée par code commune, et millésime."""
    path = source_path(cfg, cfg["inputs"]["population"])
    with zipfile.ZipFile(path) as z, z.open(INSEE_MEMBER) as f:
        df = pd.read_csv(f, sep=";", dtype=str, usecols=["COM", "PMUN"])
    if df["COM"].duplicated().any():
        raise ValueError(f"{INSEE_MEMBER} : codes COM en double")
    pmun = pd.Series(pd.to_numeric(df["PMUN"], errors="raise").values, index=df["COM"].values)
    return pmun, year_in(path.name)


def load_fr(cfg: dict, metric: str) -> dict:
    inp = cfg["inputs"]
    ae_year = year_in(source_path(cfg, inp["communes"]).parent.name)
    core_code = str(cfg["core_city"]["commune_code"])

    # --- communes -------------------------------------------------------------
    com = gpd.read_parquet(source_path(cfg, inp["communes"]),
                           columns=["code_insee", "nom_officiel", "population",
                                    "date_du_recensement", "geometrie"])
    com = to_wgs84(com.rename_geometry("geometry"))
    com["unit_id"] = com["code_insee"].astype(str)
    if com["unit_id"].duplicated().any():
        raise ValueError("Admin Express : code_insee en double dans commune.parquet")
    com = ensure_valid(com, "communes")

    # --- population : INSEE PMUN en priorité, sinon Admin Express ---------------
    pmun, insee_year = read_insee_pmun(cfg)
    ae_pop = com["population"].astype("int64")
    insee_pop = com["unit_id"].map(pmun)
    has_insee = insee_pop.notna()
    both = int(has_insee.sum())
    equal = int((ae_pop[has_insee] == insee_pop[has_insee].astype("int64")).sum())
    missing = sorted(com.loc[~has_insee, "unit_id"])
    LOG.info("Population : AE == INSEE PMUN pour %d/%d communes communes aux deux sources (%.2f %%)",
             equal, both, 100 * equal / both)
    LOG.info("Population : %d communes AE absentes du fichier INSEE (repli AE) : %s",
             len(missing), ", ".join(missing))
    com["population"] = np.where(has_insee, insee_pop, ae_pop).astype("int64")
    com["pop_source"] = np.where(has_insee, f"insee_pmun_{insee_year}", f"ign_admin_express_{ae_year}")
    com["pop_ref_date"] = np.where(has_insee, f"{insee_year}-01-01",
                                   com["date_du_recensement"].astype(str))

    # --- points : chef-lieu IGN, sinon point représentatif ----------------------
    chl = gpd.read_parquet(source_path(cfg, inp["commune_points"]),
                           columns=["code_insee_de_la_commune", "geometrie"])
    chl = to_wgs84(chl.rename_geometry("geometry"))
    if chl["code_insee_de_la_commune"].duplicated().any():
        LOG.warning("chef_lieu_de_commune : codes en double, le premier est retenu")
        chl = chl.drop_duplicates("code_insee_de_la_commune")
    chl_pt = chl.set_index("code_insee_de_la_commune").geometry
    com["_pt"] = com["unit_id"].map(chl_pt)
    com["point_source"] = np.where(com["_pt"].notna(), "ign_chef_lieu", "representative_point")
    com["point_official"] = com["_pt"].notna()
    fallback = com["_pt"].isna()
    if fallback.any():
        LOG.warning("%d commune(s) sans chef-lieu IGN -> point représentatif : %s", fallback.sum(),
                    ", ".join(com.loc[fallback, "unit_id"]))
        com.loc[fallback, "_pt"] = rep_points(com.loc[fallback, "geometry"], metric).values
    com["name"] = com["nom_officiel"]
    com["level"], com["parent_id"] = "commune", ""

    keep_cols = [*UNIT_COLS[:-1], "point_source", "point_official", "_pt", "geometry"]
    communes_info = com.set_index("unit_id")[["name", "_pt"]]
    core = com.loc[com["unit_id"] == core_code]
    if core.empty:
        raise ValueError(f"commune-centre {core_code} introuvable dans Admin Express")
    core = core.iloc[0]
    if norm(core["name"]) != norm(cfg["core_city"]["name"]):
        raise ValueError(f"commune {core_code} = « {core['name']} », attendu « {cfg['core_city']['name']} »")
    core_point = core["_pt"]
    facts = {"ae_year": ae_year, "insee_year": insee_year, "n_communes_both": both,
             "n_equal": equal, "n_missing_insee": len(missing), "missing_insee": missing,
             "district_pop_check": None}
    units = com[keep_cols]

    # --- découpage de la ville-centre en arrondissements ----------------------
    if cfg["core_city"].get("split_into_districts"):
        arr = gpd.read_parquet(source_path(cfg, inp["districts"]),
                               columns=["code_insee", "nom_officiel", "population",
                                        "code_insee_de_la_commune_de_rattach", "geometrie"])
        arr = to_wgs84(arr.rename_geometry("geometry"))
        arr = arr[arr["code_insee_de_la_commune_de_rattach"].astype(str) == core_code].copy()
        if arr.empty:
            raise ValueError(f"aucun arrondissement municipal rattaché à {core_code}")
        arr = ensure_valid(arr, "arrondissements")
        arr["unit_id"] = arr["code_insee"].astype(str)
        arr["name"] = arr["nom_officiel"]
        arr["level"], arr["parent_id"] = "district", core_code
        arr["population"] = arr["population"].astype("int64")
        arr["pop_source"] = f"ign_admin_express_{ae_year}"
        arr["pop_ref_date"] = str(core["date_du_recensement"])
        chd = gpd.read_parquet(source_path(cfg, inp["district_points"]),
                               columns=["code_insee_de_l_arrondissement_municipal", "geometrie"])
        chd = to_wgs84(chd.rename_geometry("geometry"))
        chd_pt = chd.drop_duplicates("code_insee_de_l_arrondissement_municipal") \
                    .set_index("code_insee_de_l_arrondissement_municipal").geometry
        arr["_pt"] = arr["unit_id"].map(chd_pt)
        arr["point_source"] = np.where(arr["_pt"].notna(), "ign_chef_lieu_arrondissement",
                                       "representative_point")
        arr["point_official"] = arr["_pt"].notna()
        fb = arr["_pt"].isna()
        if fb.any():
            LOG.warning("%d arrondissement(s) sans chef-lieu IGN -> point représentatif : %s",
                        fb.sum(), ", ".join(arr.loc[fb, "unit_id"]))
            arr.loc[fb, "_pt"] = rep_points(arr.loc[fb, "geometry"], metric).values
        # Contrôle : somme des arrondissements == population de la commune-centre (AE)
        s, ref = int(arr["population"].sum()), int(ae_pop[com["unit_id"] == core_code].iloc[0])
        LOG.info("Population : somme des %d arrondissements de %s = %s ; commune %s (Admin Express) = %s -> %s",
                 len(arr), core["name"], fmt_int(s), core_code, fmt_int(ref),
                 "égal" if s == ref else "DIFFÉRENT")
        if s != ref:
            LOG.warning("somme des arrondissements ≠ population de la commune-centre")
        facts["district_pop_check"] = {"n": len(arr), "sum_districts": s, "core_commune_ae": ref}
        units = pd.concat([units[units["unit_id"] != core_code], arr[keep_cols]], ignore_index=True)
    units = gpd.GeoDataFrame(units, geometry="geometry", crs=WGS84)
    units, facts["merged_districts"] = merge_districts(cfg, units, core_code)
    return {"units": units, "core_point": core_point, "core_name": core["name"],
            "communes_info": communes_info, "facts": facts}


# --------------------------------------------------------------------------- #
# Branche DE (VG250-EW + Stadtteile de Francfort + nœuds OSM `place`)
# --------------------------------------------------------------------------- #
def vg250_gpkg(cfg: dict) -> tuple:
    """Chemin du gpkg VG250 (extrait du zip si absent / de taille différente) et date de stand."""
    zpath = source_path(cfg, cfg["inputs"]["communes"])
    with zipfile.ZipFile(zpath) as z:
        gp = [i for i in z.infolist() if i.filename.endswith(".gpkg")]
        if len(gp) != 1:
            raise RuntimeError(f"{zpath.name} : un seul .gpkg attendu, trouvé {len(gp)}")
        target = UNZIP_ROOT / gp[0].filename
        if not target.exists() or target.stat().st_size != gp[0].file_size:
            LOG.info("extraction de %s vers %s", gp[0].filename, UNZIP_ROOT)
            z.extract(gp[0], UNZIP_ROOT)
        akt = [i.filename for i in z.infolist()
               if i.filename.endswith("aktualitaet.txt") and "dokumentation" not in i.filename]
        stand = datetime.strptime(z.read(akt[0]).decode("utf-8").strip(), "%d.%m.%Y").date()
    return target, stand


def load_frankfurt_districts(cfg: dict, core_point: Point, ref_year: int, metric: str) -> gpd.GeoDataFrame:
    """Stadtteile de la ville-centre : polygones WFS + population Melderegister.

    Renvoie un DataFrame d'unités (colonnes UNIT_COLS + _pt provisoire) avec, en plus,
    `match_name` / `match_geom` (nom et polygone servant à chercher le nœud OSM).
    """
    inp = cfg["inputs"]
    # --- polygones : le CRS annoncé est contrôlé sur les coordonnées réelles -----
    stt = gpd.read_file(source_path(cfg, inp["districts"]))
    if stt.crs is None:
        stt = stt.set_crs(WGS84)
    xmin, ymin, xmax, ymax = stt.total_bounds
    in_lonlat = -180 <= xmin <= xmax <= 180 and -90 <= ymin <= ymax <= 90
    if stt.crs.is_geographic != in_lonlat:
        raise ValueError(f"Stadtteile : CRS annoncé {stt.crs} incohérent avec les coordonnées réelles "
                         f"(emprise {xmin:.4f}, {ymin:.4f}, {xmax:.4f}, {ymax:.4f})")
    stt = ensure_valid(to_wgs84(stt), "Stadtteile")
    # le centre de l'emprise doit tomber près de la ville-centre (axes lon/lat inversés, mauvais CRS…)
    bx0, by0, bx1, by1 = stt.total_bounds
    gap = float(geod_km((bx0 + bx1) / 2, (by0 + by1) / 2, core_point.x, core_point.y)[0])
    if gap > SANITY_MAX_KM:
        raise ValueError(f"Stadtteile : emprise à {gap:.0f} km du point de la ville-centre (CRS erroné ?)")
    stt["STT_NAME"] = stt["STT_NAME"].astype(str).str.strip()
    if stt["STT_NAME"].duplicated().any() or stt["STT_ID"].duplicated().any():
        raise ValueError("Stadtteile : STT_NAME ou STT_ID en double")
    by_name = stt.set_index("STT_NAME")

    # --- population Melderegister -------------------------------------------------
    pop = pd.read_csv(source_path(cfg, inp["district_population"]), sep=";", encoding="utf-8-sig")
    years = sorted(pop["Jahr"].unique())
    year = ref_year if ref_year in years else years[-1]
    if year != ref_year:
        LOG.warning("Melderegister : année %d absente, repli sur %d", ref_year, year)
    pop = pop[pop["Jahr"] == year]
    is_total = pop["Stadtteil"].astype(str).str.match(TOTAL_ROW_RE)
    total_row = int(pop.loc[is_total, "Einwohner_insg"].sum()) if is_total.any() else None
    pop = pop[~is_total].copy()
    pop["Stadtteil"] = pop["Stadtteil"].astype(str).str.strip()
    if pop["Stadtteil"].duplicated().any():
        raise ValueError("Melderegister : Stadtteil en double")

    # --- jointure par nom exact ; « X (inkl. Y) » = union des polygones X et Y ------
    rows, used, unmatched = [], set(), []
    for _, r in pop.iterrows():
        m = INKL_RE.match(r["Stadtteil"])
        main = m.group("main").strip() if m else r["Stadtteil"]
        parts = [p.strip() for p in re.split(r"\s*(?:,|\bund\b)\s*", m.group("parts"))] if m else []
        missing = [n for n in [main, *parts] if n not in by_name.index]
        if missing:
            unmatched.append((r["Stadtteil"], missing))
            continue
        used.update([main, *parts])
        geom = shapely.union_all([by_name.loc[n, "geometry"] for n in [main, *parts]])
        rows.append({
            "unit_id": f"{DISTRICT_DE_PREFIX}{by_name.loc[main, 'STT_ID']}", "name": r["Stadtteil"],
            "level": "district", "parent_id": str(cfg["core_city"]["commune_code"]),
            "population": int(r["Einwohner_insg"]), "pop_source": "frankfurt_melderegister",
            "pop_ref_date": f"{year}-12-31", "geometry": geom,
            "match_name": main, "match_geom": by_name.loc[main, "geometry"]})
    leftovers = sorted(set(by_name.index) - used)
    if unmatched or leftovers:
        raise ValueError(f"jointure Stadtteile/Melderegister impossible : CSV sans polygone = {unmatched}, "
                         f"polygones sans ligne CSV = {leftovers}")
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=WGS84)
    s = int(out["population"].sum())
    if total_row is not None and total_row != s:
        LOG.warning("Melderegister : ligne « insgesamt » (%s) ≠ somme des Stadtteile (%s)",
                    fmt_int(total_row), fmt_int(s))
    LOG.info("Stadtteile : %d unités (%d polygones WFS), Σ Melderegister %d = %s",
             len(out), len(stt), year, fmt_int(s))

    # --- points : nœud OSM `place` du même nom, dans le polygone --------------------
    places = osm_features(cfg, "places", [f"n/place={','.join(OSM_PLACE_PRIORITY)}"], ["name", "place"])
    places = places[places["name"].notna() & places["place"].isin(OSM_PLACE_PRIORITY)].copy()
    places["name_norm"] = places["name"].map(norm)
    places["rank"] = places["place"].map({p: i for i, p in enumerate(OSM_PLACE_PRIORITY)})
    cand = gpd.GeoDataFrame(out[["unit_id", "match_name"]], geometry=out["match_geom"].values, crs=WGS84)
    cand["name_norm"] = cand["match_name"].map(norm)
    j = gpd.sjoin(places, cand[["unit_id", "name_norm", "geometry"]], how="inner", predicate="within",
                  lsuffix="osm", rsuffix="stt")
    j = j[j["name_norm_osm"] == j["name_norm_stt"]].copy()
    reps = rep_points(cand.set_index("unit_id").geometry, metric)
    j["d"] = [float(geod_km(g.x, g.y, reps[u].x, reps[u].y)[0]) for g, u in zip(j.geometry, j["unit_id"])]
    best = j.sort_values(["rank", "d", "osm_id"]).drop_duplicates("unit_id").set_index("unit_id")
    out["_pt"] = out["unit_id"].map(best.geometry)
    out["point_source"] = np.where(out["_pt"].notna(), "osm_place", "representative_point")
    out["point_official"] = False
    fb = out["_pt"].isna()
    if fb.any():
        LOG.warning("%d Stadtteil(e) sans nœud OSM place correspondant -> point représentatif : %s",
                    fb.sum(), ", ".join(out.loc[fb, "name"]))
        out.loc[fb, "_pt"] = out.loc[fb, "unit_id"].map(reps).values
    return out


def load_de(cfg: dict, metric: str) -> dict:
    gpkg, stand = vg250_gpkg(cfg)
    core_code = str(cfg["core_city"]["commune_code"])

    # --- communes (GF == 4 : terre ferme) ; dissolution par AGS si doublons --------
    gem = gpd.read_file(gpkg, layer=VG250_GEM_LAYER, where="GF = 4", columns=["AGS", "GEN", "EWZ", "KFL"])
    gem = to_wgs84(gem)
    if gem["AGS"].duplicated().any():
        dup = gem[gem["AGS"].duplicated(keep=False)]
        if (dup.groupby("AGS")["EWZ"].nunique() > 1).any():
            raise ValueError("VG250 : lignes de même AGS avec des EWZ différents")
        LOG.warning("VG250 : %d AGS portés par plusieurs lignes -> dissolution", dup["AGS"].nunique())
        gem = gem.dissolve(by="AGS", aggfunc={"GEN": "first", "EWZ": "first", "KFL": "sum"}).reset_index()
    gem = ensure_valid(gem, "communes VG250")
    gem["unit_id"] = gem["AGS"].astype(str)
    gem["name"] = gem["GEN"]
    gem["level"], gem["parent_id"] = "commune", ""
    if gem["EWZ"].isna().any():
        raise ValueError("VG250 : EWZ manquant")
    gem["population"] = gem["EWZ"].astype("int64")
    gem["pop_source"], gem["pop_ref_date"] = "destatis_vg250_ew", stand.isoformat()

    # --- points : « Kern der Gemeinde » (vg250_pk) -----------------------------------
    pk = gpd.read_file(gpkg, layer=VG250_PK_LAYER, columns=["AGS", "GEN"])
    pk = to_wgs84(pk)
    if pk["AGS"].duplicated().any():
        LOG.warning("vg250_pk : AGS en double, le premier point est retenu")
        pk = pk.drop_duplicates("AGS")
    gem["_pt"] = gem["unit_id"].map(pk.set_index("AGS").geometry)
    gem["point_source"] = np.where(gem["_pt"].notna(), "bkg_vg250_pk", "representative_point")
    gem["point_official"] = gem["_pt"].notna()
    fb = gem["_pt"].isna()
    if fb.any():
        LOG.warning("%d commune(s) sans point vg250_pk -> point représentatif : %s", fb.sum(),
                    ", ".join(gem.loc[fb, "unit_id"]))
        gem.loc[fb, "_pt"] = rep_points(gem.loc[fb, "geometry"], metric).values

    keep_cols = [*UNIT_COLS[:-1], "point_source", "point_official", "_pt", "geometry"]
    communes_info = gem.set_index("unit_id")[["name", "_pt"]]
    core = gem.loc[gem["unit_id"] == core_code]
    if core.empty:
        raise ValueError(f"commune-centre {core_code} introuvable dans VG250")
    core = core.iloc[0]
    if norm(core["name"]) != norm(cfg["core_city"]["name"]):
        raise ValueError(f"commune {core_code} = « {core['name']} », attendu « {cfg['core_city']['name']} »")
    facts = {"stand": stand.isoformat(), "district_pop_check": None}
    units = gem[keep_cols]

    # --- découpage de la ville-centre en Stadtteile ------------------------------------
    if cfg["core_city"].get("split_into_districts"):
        dist = load_frankfurt_districts(cfg, core["_pt"], stand.year, metric)
        ewz = int(core["population"])
        s = int(dist["population"].sum())
        LOG.info("Population : Σ Stadtteile (Melderegister) = %s ; EWZ Destatis %s = %s (écart %+.1f %%)",
                 fmt_int(s), core["name"], fmt_int(ewz), 100 * (s - ewz) / ewz)
        facts["district_pop_check"] = {"n": len(dist), "sum_districts": s, "core_commune_ewz": ewz,
                                       "melderegister_year_ref": str(dist["pop_ref_date"].iloc[0])}
        units = pd.concat([units[units["unit_id"] != core_code], dist[keep_cols]], ignore_index=True)
    units = gpd.GeoDataFrame(units, geometry="geometry", crs=WGS84)
    units, facts["merged_districts"] = merge_districts(cfg, units, core_code)
    return {"units": units, "core_point": core["_pt"], "core_name": core["name"],
            "communes_info": communes_info, "facts": facts}


# --------------------------------------------------------------------------- #
# Points d'origine (communs aux deux pays)
# --------------------------------------------------------------------------- #
def resolve_origins(cfg: dict, communes_info: pd.DataFrame) -> pd.DataFrame:
    """Un point par entrée de origin.points : commune_point ou gtfs_stop."""
    ocfg = cfg["origin"]
    rows = []
    stops = None
    for p in ocfg["points"]:
        if p["source"] == "commune_point":
            code = str(ocfg["commune_code"])
            if code not in communes_info.index:
                raise ValueError(f"origin.commune_code {code} introuvable parmi les communes")
            c = communes_info.loc[code]
            if norm(c["name"]) != norm(ocfg["name"]):
                raise ValueError(f"commune {code} = « {c['name']} », origin.name = « {ocfg['name']} »")
            lon, lat, ref = c["_pt"].x, c["_pt"].y, code
        elif p["source"] == "gtfs_stop":
            if stops is None:
                gtfs = source_path(cfg, cfg["inputs"]["gtfs"][0])
                with zipfile.ZipFile(gtfs) as z, z.open("stops.txt") as f:
                    stops = pd.read_csv(f, dtype=str, usecols=["stop_id", "stop_name", "stop_lon", "stop_lat"])
            hit = stops[stops["stop_id"] == str(p["gtfs_stop_id"])]
            if hit.empty:
                raise ValueError(f"stop_id {p['gtfs_stop_id']} introuvable dans stops.txt")
            s = hit.iloc[0]
            if not re.search(p["gtfs_stop_name_regex"], str(s["stop_name"])):
                raise ValueError(f"arrêt {p['gtfs_stop_id']} = « {s['stop_name']} » ne correspond pas à "
                                 f"{p['gtfs_stop_name_regex']}")
            lon, lat, ref = float(s["stop_lon"]), float(s["stop_lat"]), str(p["gtfs_stop_id"])
        else:
            raise ValueError(f"origin.points[{p['id']}] : source inconnue « {p['source']} »")
        rows.append({"id": p["id"], "label_de": p["label_de"], "label_fr": p["label_fr"],
                     "source": p["source"], "source_ref": ref, "lon": float(lon), "lat": float(lat)})
    ids = [r["id"] for r in rows]
    if CENTRE_POINT_ID not in ids:
        raise ValueError(f"origin.points doit contenir un point d'id « {CENTRE_POINT_ID} »")
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Contrôle qualité H2 : points des destinations vs mairies OSM
# --------------------------------------------------------------------------- #
def osm_townhalls(cfg: dict, metric: str) -> gpd.GeoDataFrame:
    """Mairies OSM (amenity=townhall) de inputs.osm, en points EPSG:4326 : osm_id, name, geometry."""
    th = osm_features(cfg, "townhall", ["nwr/amenity=townhall"], ["name", "amenity"])
    th = th[th["amenity"] == "townhall"].copy()
    th["geometry"] = th.to_crs(metric).centroid.to_crs(WGS84)     # nœuds : inchangé ; ways/relations : centroïde
    return th[["osm_id", "name", "geometry"]].reset_index(drop=True)


def nearest_townhall_in_polygon(th: gpd.GeoDataFrame, polys: gpd.GeoDataFrame, pts) -> pd.DataFrame:
    """Pour chaque unité de `polys`, la mairie OSM la plus proche de son point `pts` PARMI celles
    situées dans son polygone. Renvoie un DataFrame indexé par unit_id : dist_m, name, osm_id,
    geometry (uniquement les unités qui ont au moins une mairie dans leur polygone)."""
    j = gpd.sjoin(th, polys[["unit_id", "geometry"]], how="inner", predicate="within")
    tgt = pd.Series(list(pts), index=polys["unit_id"].values).reindex(j["unit_id"])
    j["dist_m"] = geod_km(j.geometry.x.values, j.geometry.y.values,
                          [g.x for g in tgt], [g.y for g in tgt]) * 1000
    return j.sort_values(["dist_m", "osm_id"]).drop_duplicates("unit_id").set_index("unit_id")


def qa_vs_townhall(cfg: dict, polys: gpd.GeoDataFrame, pts: gpd.GeoSeries, metric: str) -> pd.DataFrame:
    """Distance (m) entre le point de chaque unité et la mairie OSM la plus proche DANS son polygone.

    Contrôle seulement : aucun point n'est modifié.
    """
    best = nearest_townhall_in_polygon(osm_townhalls(cfg, metric), polys, pts)
    qa = pd.DataFrame({"unit_id": polys["unit_id"].values, "name": polys["name"].values,
                       "point_source": polys["point_source"].values})
    qa["dist_m"] = qa["unit_id"].map(best["dist_m"]).round(1)
    qa["townhall_name"] = qa["unit_id"].map(best["name"])
    return qa


def use_osm_townhall_points(cfg: dict, units: gpd.GeoDataFrame, metric: str):
    """Option analysis.destination_point = osm_townhall (analyse de sensibilité).

    Pour chaque unité qui a une mairie OSM dans son polygone, le point devient la plus proche du
    point officiel (point_source = osm_townhall, point_official = False) ; sinon le point officiel
    est conservé. Renvoie (units, shift) ; `shift` : unit_id, name, official_point_source,
    shift_m (NaN si aucune mairie), townhall_name, osm_id.
    """
    best = nearest_townhall_in_polygon(osm_townhalls(cfg, metric), units, units["_pt"])
    has = units["unit_id"].isin(best.index)
    units = units.copy()
    shift = pd.DataFrame({"unit_id": units["unit_id"].values, "name": units["name"].values,
                          "official_point_source": units["point_source"].values})
    shift["shift_m"] = shift["unit_id"].map(best["dist_m"]).round(1)
    shift["townhall_name"] = shift["unit_id"].map(best["name"])
    shift["osm_id"] = shift["unit_id"].map(best["osm_id"])
    new_pt = dict(zip(best.index, best.geometry))
    units["_pt"] = [new_pt.get(u, p) for u, p in zip(units["unit_id"], units["_pt"])]
    units.loc[has, "point_source"] = "osm_townhall"
    units.loc[has, "point_official"] = False
    LOG.info("Point de destination = mairie OSM : %d unités sur %d ont une mairie OSM dans leur "
             "polygone (les autres gardent le point officiel)", has.sum(), len(units))
    return units, shift


# --------------------------------------------------------------------------- #
# Assemblage : distances, périmètre, écriture
# --------------------------------------------------------------------------- #
def build_units(cfg: dict, data: dict, origins: pd.DataFrame, metric: str):
    """Ajoute distances géodésiques et drapeaux, applique le périmètre, calcule les surfaces.

    Renvoie (units, pts) : unités (polygones, EPSG:4326) et GeoSeries de leurs points,
    alignés ligne à ligne.
    """
    R, B = cfg["analysis"]["radius_km"], cfg["analysis"]["clip_buffer_km"]
    units = data["units"].reset_index(drop=True)
    pts = gpd.GeoSeries(units.pop("_pt").values, crs=WGS84)
    lon, lat = pts.x.values, pts.y.values
    core = data["core_point"]

    # distances géodésiques (WGS84) à chaque point d'origine et au point de la ville-centre entière
    for _, o in origins.iterrows():
        units[f"dist_km_{o['id']}"] = geod_km(lon, lat, o["lon"], o["lat"])
    units["dist_km_core"] = geod_km(lon, lat, core.x, core.y)

    # périmètre : point à ≤ R + tampon du centre d'origine OU de la ville-centre
    keep = ((units[f"dist_km_{CENTRE_POINT_ID}"] <= R + B) | (units["dist_km_core"] <= R + B)).values
    LOG.info("Périmètre : %d unités conservées sur %d (≤ %g + %g km du point « %s » ou de la ville-centre)",
             keep.sum(), len(units), R, B, CENTRE_POINT_ID)
    units, pts = units[keep].reset_index(drop=True), pts[keep].reset_index(drop=True)
    units = ensure_valid(units, "unités conservées")
    units["area_km2"] = units.to_crs(metric).area / 1e6
    units["is_origin_commune"] = units["unit_id"] == str(cfg["origin"]["commune_code"])
    if not units["is_origin_commune"].any():
        raise ValueError("la commune d'origine (origin.commune_code) n'est pas dans les unités conservées")
    units["in_radius"] = units[f"dist_km_{CENTRE_POINT_ID}"] <= R
    units["in_core_radius"] = units["dist_km_core"] <= R
    order = units["unit_id"].argsort(kind="stable").values            # tri déterministe par unit_id
    units, pts = units.iloc[order].reset_index(drop=True), pts.iloc[order].reset_index(drop=True)
    if units["population"].isna().any() or units["unit_id"].duplicated().any():
        raise ValueError("unités : population manquante ou unit_id en double")
    return units, pts


def write_outputs(cfg: dict, units: gpd.GeoDataFrame, pts: gpd.GeoSeries, origins: pd.DataFrame,
                  out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)

    polys = units[[*UNIT_COLS, "geometry"]].copy()
    polys["population"] = polys["population"].astype("int64")
    points = gpd.GeoDataFrame(units[["unit_id", "name", "point_source", "point_official"]].copy(),
                              geometry=pts.values, crs=WGS84)
    gpkg = out / "units.gpkg"
    gpkg.unlink(missing_ok=True)                  # repartir d'un fichier propre (pas d'unités périmées)
    polys.to_file(gpkg, layer="units_polygons", driver="GPKG")
    points.to_file(gpkg, layer="units_points", driver="GPKG")

    dist_cols = [f"dist_km_{i}" for i in origins["id"]] + ["dist_km_core"]
    dest = units[[*UNIT_COLS, "point_source", "point_official", "is_origin_commune",
                  *dist_cols, "in_radius", "in_core_radius"]].copy()
    dest["population"] = dest["population"].astype("int64")
    gpd.GeoDataFrame(dest, geometry=pts.values, crs=WGS84).to_parquet(out / "destinations.parquet",
                                                                      index=False)

    org = gpd.GeoDataFrame(origins[["id", "label_de", "label_fr", "source", "source_ref"]].copy(),
                           geometry=gpd.points_from_xy(origins["lon"], origins["lat"]), crs=WGS84)
    (out / "origins.geojson").unlink(missing_ok=True)
    org.to_file(out / "origins.geojson", driver="GeoJSON")
    LOG.info("Écrit : %s (units_polygons, units_points), destinations.parquet, origins.geojson", gpkg)


def run_qa(cfg: dict, units: gpd.GeoDataFrame, pts: gpd.GeoSeries, metric: str) -> dict:
    """Contrôle H2 sur les unités in_radius ; écrit le CSV et renvoie les statistiques."""
    sel = units["in_radius"].values
    qa = qa_vs_townhall(cfg, units[sel].reset_index(drop=True), pts[sel].reset_index(drop=True), metric)
    qa = qa.sort_values(["dist_m", "unit_id"], ascending=[False, True], na_position="last")
    qa.to_csv(outputs_dir(cfg) / "qa_points_vs_osm_townhall.csv", index=False, encoding="utf-8")
    d = qa["dist_m"].dropna()
    stats = {"n_units": int(len(qa)), "n_with_townhall": int(len(d)),
             "n_without_townhall": int(qa["dist_m"].isna().sum()),
             "median_m": round(float(d.median()), 1) if len(d) else None,
             "p90_m": round(float(np.percentile(d, 90)), 1) if len(d) else None,
             "far_threshold_m": QA_FAR_M, "n_far": int((d > QA_FAR_M).sum())}
    far = qa[qa["dist_m"] > QA_FAR_M]
    LOG.info("H2 point vs mairie OSM : %d unités in_radius, %d avec mairie OSM dans le polygone, "
             "%d sans ; médiane %s m, p90 %s m, %d > %d m", stats["n_units"], stats["n_with_townhall"],
             stats["n_without_townhall"], stats["median_m"], stats["p90_m"], stats["n_far"], QA_FAR_M)
    for _, r in far.iterrows():
        LOG.info("  > %d m : %s (%s) %.0f m [%s] mairie OSM « %s »", QA_FAR_M, r["name"], r["unit_id"],
                 r["dist_m"], r["point_source"], r["townhall_name"] if pd.notna(r["townhall_name"]) else "(sans nom)")
    return stats


# --------------------------------------------------------------------------- #
# Journal des hypothèses
# --------------------------------------------------------------------------- #
def log_assumptions(cfg: dict, data: dict, units: gpd.GeoDataFrame, origins: pd.DataFrame,
                    qa: dict) -> None:
    country, f = cfg["region"]["country"], data["facts"]
    R, B = cfg["analysis"]["radius_km"], cfg["analysis"]["clip_buffer_km"]
    core_name, core_code = data["core_name"], str(cfg["core_city"]["commune_code"])
    chk = f.get("district_pop_check")
    n_dist = int((units["level"] == "district").sum())
    ps = units["point_source"].value_counts().to_dict()
    ps_txt = ", ".join(f"{k} : {v}" for k, v in ps.items())

    # --- population ----------------------------------------------------------------
    if country == "FR":
        ok = chk is not None and chk["sum_districts"] == chk["core_commune_ae"]
        fr = (f"Population = population municipale INSEE (PMUN, millésime {f['insee_year']}, au 01/01/{f['insee_year']}) ; "
              f"à défaut (ville-centre, arrondissements, {f['n_missing_insee']} codes absents du fichier INSEE) "
              f"population du fichier Admin Express {f['ae_year']}, qui reprend la population INSEE. "
              f"Égalité AE = INSEE pour {f['n_equal']}/{f['n_communes_both']} communes communes aux deux sources"
              + (f" ; somme des {chk['n']} arrondissements = population de {core_name} (AE) : "
                 f"{'oui' if ok else 'NON'}." if chk else "."))
        de = (f"Einwohner = Bevölkerung am Ort der Hauptwohnung (PMUN) des INSEE, Jahrgang {f['insee_year']} "
              f"(Stand 01.01.{f['insee_year']}); ersatzweise (Kernstadt, Arrondissements, "
              f"{f['n_missing_insee']} im INSEE-Datensatz fehlende Codes) die Einwohnerzahl aus Admin Express "
              f"{f['ae_year']}, die die INSEE-Zahl übernimmt. Übereinstimmung AE = INSEE bei "
              f"{f['n_equal']}/{f['n_communes_both']} Gemeinden"
              + (f"; Summe der {chk['n']} Arrondissements = Einwohnerzahl {core_name} (AE): "
                 f"{'ja' if ok else 'NEIN'}." if chk else "."))
    else:
        fr = (f"Population des communes = Destatis (champ EWZ de VG250-EW, au {f['stand']}). "
              + (f"Population des {chk['n']} Stadtteile de {core_name} = Melderegister de la Ville de Francfort "
                 f"(résidence principale, au {chk['melderegister_year_ref']})." if chk else ""))
        de = (f"Einwohner der Gemeinden = Destatis (Feld EWZ in VG250-EW, Stand {f['stand']}). "
              + (f"Einwohner der {chk['n']} Stadtteile von {core_name} = Melderegister der Stadt Frankfurt "
                 f"(Hauptwohnsitz, Stand {chk['melderegister_year_ref']})." if chk else ""))
    log_assumption(cfg, "01", "U1_population_source", fr.strip(), de.strip(),
                   value={k: v for k, v in f.items() if k != "missing_insee"} | {"n_units": len(units)})

    if country == "DE" and chk:
        gap = 100 * (chk["sum_districts"] - chk["core_commune_ewz"]) / chk["core_commune_ewz"]
        log_assumption(
            cfg, "01", "U1b_core_city_population_gap",
            f"Somme des Stadtteile (Melderegister) = {fmt_int(chk['sum_districts'])} ≠ EWZ Destatis de {core_name} "
            f"= {fmt_int(chk['core_commune_ewz'])} (écart {gap:+.1f} %) : deux sources différentes "
            f"(registre communal, résidence principale, vs. chiffres Destatis). Les populations des Stadtteile "
            f"et celles des autres communes (Destatis) ne sont donc pas strictement comparables.",
            f"Summe der Stadtteile (Melderegister) = {fmt_int(chk['sum_districts'])} ≠ EWZ Destatis für {core_name} "
            f"= {fmt_int(chk['core_commune_ewz'])} (Abweichung {gap:+.1f} %): unterschiedliche Quellen "
            f"(kommunales Melderegister, Hauptwohnsitz, vs. Destatis). Die Einwohnerzahlen der Stadtteile und "
            f"der übrigen Gemeinden (Destatis) sind daher nicht streng vergleichbar.",
            value={"sum_districts": chk["sum_districts"], "core_commune_ewz": chk["core_commune_ewz"],
                   "gap_pct": round(gap, 2)})

    # --- points ----------------------------------------------------------------------
    src_fr = {"FR": "chef-lieu de commune IGN (Admin Express) ; arrondissements : chef-lieu d'arrondissement municipal IGN",
              "DE": "« Kern der Gemeinde » BKG (VG250, couche vg250_pk) ; Stadtteile : nœud OSM `place` de même nom "
                    "situé dans le polygone (suburb > quarter > village > town > neighbourhood)"}[country]
    src_de = {"FR": "Gemeinde-Hauptort (chef-lieu) des IGN (Admin Express); Arrondissements: chef-lieu d'arrondissement municipal des IGN",
              "DE": "„Kern der Gemeinde“ des BKG (VG250, Ebene vg250_pk); Stadtteile: OSM-Knoten `place` gleichen "
                    "Namens im Polygon (suburb > quarter > village > town > neighbourhood)"}[country]
    log_assumption(cfg, "01", "U5_point_source",
                   f"Point représentatif de chaque destination = {src_fr}. Repli : point représentatif du polygone "
                   f"(non officiel). Répartition des unités conservées : {ps_txt}.",
                   f"Repräsentativer Punkt je Ziel = {src_de}. Ersatz: repräsentativer Punkt des Polygons "
                   f"(nicht amtlich). Verteilung der behaltenen Einheiten: {ps_txt}.",
                   value=ps)

    # --- découpage de la ville-centre ----------------------------------------------------
    kind_fr, kind_de = ("arrondissements municipaux", "Arrondissements") if country == "FR" else ("Stadtteile", "Stadtteile")
    merged = f.get("merged_districts") or []
    if cfg["core_city"].get("split_into_districts"):
        n_raw = chk["n"] if chk else n_dist
        n_final = n_raw - sum(len(m["members"]) - 1 for m in merged)
        fusion_fr = f" ; regroupés en {n_final} unités (voir U2b_merged_districts)" if merged else ""
        fusion_de = f"; zu {n_final} Einheiten zusammengefasst (siehe U2b_merged_districts)" if merged else ""
        fr = (f"La ville-centre {core_name} ({core_code}) est remplacée par ses {n_raw} {kind_fr} "
              f"(level = district, parent_id = {core_code}{fusion_fr}) pour ne pas écraser sa structure interne en "
              f"un seul point ; dist_km_core reste mesurée au point de la commune entière.")
        de = (f"Die Kernstadt {core_name} ({core_code}) wird durch ihre {n_raw} {kind_de} ersetzt "
              f"(level = district, parent_id = {core_code}{fusion_de}), damit ihre Binnenstruktur nicht auf einen "
              f"Punkt reduziert wird; dist_km_core wird weiterhin zum Punkt der gesamten Gemeinde gemessen.")
    else:
        fr = f"La ville-centre {core_name} ({core_code}) n'est pas découpée : une seule unité."
        de = f"Die Kernstadt {core_name} ({core_code}) wird nicht unterteilt: eine einzige Einheit."
    log_assumption(cfg, "01", "U2_core_city_split", fr, de, value={"split": bool(n_dist), "n_districts": n_dist})

    # --- fusion de districts (core_city.merge_districts) ---------------------------------------
    if merged:
        fr = " ".join(
            f"Les districts {', '.join(m['members'])} de {core_name} sont fusionnés en une seule unité "
            f"« {m['name']} » ({m['unit_id']}) : ils partagent une seule mairie (points des membres à moins de "
            f"{MERGE_POINT_TOL_M} m, écart observé {m['point_spread_m']} m). Géométrie = union des membres, "
            f"population = somme ({fmt_int(m['population'])}), point = celui du membre {m['point_member']} dont "
            f"le polygone le contient." for m in merged)
        de = " ".join(
            f"Die Bezirke {', '.join(m['members'])} von {core_name} werden zu einer Einheit "
            f"„{m['name']}“ ({m['unit_id']}) zusammengefasst: sie teilen sich ein Rathaus (Punkte der Mitglieder "
            f"weniger als {MERGE_POINT_TOL_M} m auseinander, beobachtet {m['point_spread_m']} m). Geometrie = "
            f"Vereinigung der Mitglieder, Einwohner = Summe ({fmt_int(m['population'])}), Punkt = der des Mitglieds "
            f"{m['point_member']}, dessen Polygon ihn enthält." for m in merged)
        log_assumption(cfg, "01", "U2b_merged_districts", fr, de, value=merged)
    else:
        drop_assumption(cfg, "U2b_merged_districts")        # pas de fusion configurée : pas d'entrée périmée

    # --- distance géodésique -----------------------------------------------------------------
    log_assumption(cfg, "01", "U3_geodesic_distance",
                   "Les distances « à vol d'oiseau » sont des distances géodésiques sur l'ellipsoïde WGS84 "
                   "(pyproj.Geod.inv), en km, entre le point de l'unité et chaque point d'origine "
                   "(dist_km_<id>) ou le point de la ville-centre entière (dist_km_core).",
                   "Die „Luftlinie“ ist die geodätische Entfernung auf dem WGS84-Ellipsoid (pyproj.Geod.inv) in km "
                   "zwischen dem Punkt der Einheit und jedem Ursprungspunkt (dist_km_<id>) bzw. dem Punkt der "
                   "gesamten Kernstadt (dist_km_core).")

    # --- périmètre ------------------------------------------------------------------------------
    n_r, n_c = int(units["in_radius"].sum()), int(units["in_core_radius"].sum())
    log_assumption(cfg, "01", "U4_unit_perimeter",
                   f"Unités conservées : point à ≤ {R:g} + {B:g} km (rayon + tampon) du point « {CENTRE_POINT_ID} » "
                   f"d'origine ou du point de la ville-centre. in_radius : ≤ {R:g} km du point « {CENTRE_POINT_ID} » "
                   f"({n_r} unités) ; in_core_radius : ≤ {R:g} km de la ville-centre ({n_c} unités).",
                   f"Behaltene Einheiten: Punkt höchstens {R:g} + {B:g} km (Radius + Puffer) vom Ursprungspunkt "
                   f"„{CENTRE_POINT_ID}“ oder vom Punkt der Kernstadt entfernt. in_radius: ≤ {R:g} km vom Punkt "
                   f"„{CENTRE_POINT_ID}“ ({n_r} Einheiten); in_core_radius: ≤ {R:g} km von der Kernstadt ({n_c} Einheiten).",
                   value={"radius_km": R, "clip_buffer_km": B, "n_units": len(units), "n_in_radius": n_r,
                          "n_in_core_radius": n_c})

    # --- points d'origine -------------------------------------------------------------------------
    desc = "; ".join(f"{o['id']} = {o['label_fr']} ({o['source']}, {o['source_ref']}, "
                     f"{o['lat']:.6f} N, {o['lon']:.6f} E)" for _, o in origins.iterrows())
    desc_de = "; ".join(f"{o['id']} = {o['label_de']} ({o['source']}, {o['source_ref']}, "
                        f"{o['lat']:.6f} N, {o['lon']:.6f} E)" for _, o in origins.iterrows())
    log_assumption(cfg, "01", "H1_origin_points",
                   f"Points d'origine : {desc}.", f"Ursprungspunkte: {desc_de}.",
                   value=origins[["id", "source", "source_ref", "lon", "lat"]].to_dict("records"))

    # --- contrôle H2 ----------------------------------------------------------------------------------
    log_assumption(cfg, "01", "H2_destination_point",
                   f"Contrôle des points de destination : pour les {qa['n_units']} unités dans le rayon, distance entre "
                   f"le point retenu et la mairie OSM (amenity=townhall) la plus proche dans le polygone : médiane "
                   f"{qa['median_m']} m, p90 {qa['p90_m']} m, {qa['n_far']} unités > {QA_FAR_M} m, "
                   f"{qa['n_without_townhall']} sans mairie OSM. Contrôle seul : les points ne sont pas modifiés.",
                   f"Prüfung der Zielpunkte: für die {qa['n_units']} Einheiten im Radius Abstand zwischen dem gewählten "
                   f"Punkt und dem nächstgelegenen OSM-Rathaus (amenity=townhall) im Polygon: Median "
                   f"{qa['median_m']} m, P90 {qa['p90_m']} m, {qa['n_far']} Einheiten > {QA_FAR_M} m, "
                   f"{qa['n_without_townhall']} ohne OSM-Rathaus. Nur eine Kontrolle: die Punkte werden nicht verändert.",
                   value=qa)


def drop_assumption(cfg: dict, code: str) -> None:
    """Retire du journal une entrée devenue sans objet (même format que common.log_assumption)."""
    path = outputs_dir(cfg) / "assumptions.jsonl"
    if not path.exists():
        return
    entries = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    kept = [e for e in entries if e.get("code") != code]
    if len(kept) != len(entries):
        path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept), encoding="utf-8")


def report_osm_townhall_mode(cfg: dict, units: gpd.GeoDataFrame, shift: pd.DataFrame, mode: str) -> dict:
    """Déplacement des points (officiel -> mairie OSM) : CSV, statistiques, journal d'hypothèses."""
    cols = ["unit_id", "name", "level", "in_radius"]
    sh = units[cols].merge(shift.drop(columns="name"), on="unit_id", how="left")
    sh["point_used"] = np.where(sh["shift_m"].notna(), "osm_townhall", sh["official_point_source"])
    sens_dir = outputs_dir(cfg) / f"sensitivity_{mode}"
    sens_dir.mkdir(parents=True, exist_ok=True)
    sh.sort_values(["shift_m", "unit_id"], ascending=[False, True], na_position="last") \
      .to_csv(sens_dir / "point_shift_vs_official.csv", index=False, encoding="utf-8")

    r = sh[sh["in_radius"]]
    d = r["shift_m"].dropna()
    stats = {"n_units": int(len(r)), "n_with_townhall": int(len(d)),
             "n_kept_official": int(len(r) - len(d)), "n_point_changed": int((d > 0).sum()),
             "median_m": round(float(d.median()), 1) if len(d) else None,
             "p90_m": round(float(np.percentile(d, 90)), 1) if len(d) else None,
             "max_m": round(float(d.max()), 1) if len(d) else None,
             **{f"n_gt_{t}m": int((d > t).sum()) for t in (10, 100, 500, 1000)}}
    # écart d'appartenance à in_radius vs la sortie officielle (si elle existe déjà)
    official = processed_dir(cfg) / "destinations.parquet"
    if official.exists():
        o = pd.read_parquet(official, columns=["unit_id", "in_radius"])
        a, b = set(o.loc[o["in_radius"], "unit_id"]), set(units.loc[units["in_radius"], "unit_id"])
        stats["in_radius_gained_vs_official"], stats["in_radius_lost_vs_official"] = len(b - a), len(a - b)
    LOG.info("Mode %s : %d unités in_radius, %d ont une mairie OSM dans leur polygone (point remplacé), "
             "%d gardent le point officiel ; décalage médiane %s m, p90 %s m, max %s m ; > 10 m : %d, "
             "> 100 m : %d, > 500 m : %d, > 1000 m : %d", mode, stats["n_units"], stats["n_with_townhall"],
             stats["n_kept_official"], stats["median_m"], stats["p90_m"], stats["max_m"],
             stats["n_gt_10m"], stats["n_gt_100m"], stats["n_gt_500m"], stats["n_gt_1000m"])
    if "in_radius_gained_vs_official" in stats:
        LOG.info("  appartenance à in_radius vs sortie officielle : +%d / -%d unités",
                 stats["in_radius_gained_vs_official"], stats["in_radius_lost_vs_official"])
    log_assumption(
        cfg, "01", "H2b_destination_point_osm_townhall",
        f"Analyse de sensibilité (analysis.destination_point = {mode}) : le point de destination de chaque unité "
        f"est la mairie OSM (amenity=townhall) la plus proche du point officiel parmi celles situées dans son "
        f"polygone ; sans mairie OSM, le point officiel est conservé. Sur {stats['n_units']} unités dans le rayon, "
        f"{stats['n_with_townhall']} changent de point (décalage médian {stats['median_m']} m, p90 "
        f"{stats['p90_m']} m, max {stats['max_m']} m). Origines et point de la ville-centre restent officiels. "
        f"Sorties dans processed/<origine>/sensitivity_{mode}/.",
        f"Sensitivitätsanalyse (analysis.destination_point = {mode}): Zielpunkt jeder Einheit ist das dem "
        f"amtlichen Punkt nächstgelegene OSM-Rathaus (amenity=townhall) innerhalb ihres Polygons; ohne OSM-Rathaus "
        f"bleibt der amtliche Punkt. Von {stats['n_units']} Einheiten im Radius wechseln {stats['n_with_townhall']} "
        f"den Punkt (Median {stats['median_m']} m, P90 {stats['p90_m']} m, max. {stats['max_m']} m). Ursprungspunkte "
        f"und Punkt der Kernstadt bleiben amtlich. Ausgaben in processed/<Ursprung>/sensitivity_{mode}/.",
        value=stats)
    return stats


# --------------------------------------------------------------------------- #
# Programme principal
# --------------------------------------------------------------------------- #
def main() -> None:
    args = cli("Étape 01 : unités d'analyse, points représentatifs, distances").parse_args()
    cfg = load_config(args.config)
    country = cfg["region"]["country"]
    metric = metric_crs_of(cfg)
    mode = cfg["analysis"].get("destination_point", "official")
    if mode not in DEST_POINT_MODES:
        raise ValueError(f"analysis.destination_point « {mode} » invalide (attendu : {', '.join(DEST_POINT_MODES)})")
    LOG.info("Origine %s (%s), CRS métrique %s, point de destination : %s",
             cfg["origin"]["slug"], country, metric, mode)

    if country == "FR":
        data = load_fr(cfg, metric)
    elif country == "DE":
        data = load_de(cfg, metric)
    else:
        raise ValueError(f"region.country « {country} » non géré (FR ou DE)")

    origins = resolve_origins(cfg, data["communes_info"])
    for _, o in origins.iterrows():
        LOG.info("Origine « %s » (%s) : lon %.6f, lat %.6f", o["id"], o["source"], o["lon"], o["lat"])

    # Les origines (communes_info) restent aux points officiels ; seul le point des destinations change.
    shift = None
    if mode == "osm_townhall":
        data["units"], shift = use_osm_townhall_points(cfg, data["units"], metric)

    units, pts = build_units(cfg, data, origins, metric)
    if mode == "official":
        write_outputs(cfg, units, pts, origins, processed_dir(cfg))
        qa = run_qa(cfg, units, pts, metric)
        log_assumptions(cfg, data, units, origins, qa)
    else:
        # sensibilité : sorties officielles et journal officiel non écrasés — dossier de la variante
        # (run_tag, ex. config/sensitivity/*.yaml) ou, à défaut, sous-dossier dédié
        out = processed_dir(cfg) if cfg.get("run_tag") else processed_dir(cfg) / f"sensitivity_{mode}"
        write_outputs(cfg, units, pts, origins, out)
        report_osm_townhall_mode(cfg, units, shift, mode)

    LOG.info("Unités : %d (dont %d districts) ; in_radius : %d ; in_core_radius : %d ; population in_radius : %s",
             len(units), (units["level"] == "district").sum(), units["in_radius"].sum(),
             units["in_core_radius"].sum(), fmt_int(units.loc[units["in_radius"], "population"].sum()))


if __name__ == "__main__":
    main()
