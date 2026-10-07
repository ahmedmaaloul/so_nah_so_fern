"""
Étape 06 — Temps de trajet en voiture (OSRM, sans trafic).

    uv run python pipeline/06_car_times.py --config config/garches.yaml [--rebuild]

Construit un graphe OSRM (profil « car », algorithme MLD) à partir de l'extrait OSM
de l'origine (osm_clip.osm.pbf de l'étape 02), lance un osrm-routed temporaire dans
Docker et demande la matrice durées/distances origines × destinations (/table).
Le graphe est mis en cache dans interim_dir/osrm/ et reconstruit avec --rebuild ou
si l'extrait OSM est plus récent.

Points : les mêmes que 04 (origins.geojson, destinations du rayon), sans le
déplacement « îlots piétons » de 04 (propre au réseau piéton de R5) ; OSRM accroche
chaque point à la route carrossable la plus proche, l'écart est contrôlé.

Paramètres de CONTRÔLE (repli ici, surchargeables dans la config) :
  car.osrm_image      (repli : ghcr.io/project-osrm/osrm-backend:v5.27.1)
  car.osrm_port       (repli : 5000)
  qa.max_plausible_car_speed_kmh (repli : 130)

Sorties :
  processed_dir/tt_car.parquet   from_id, to_id, car_time (min), car_distance_km
                                 [+ car_time_peak (min), motorway_share si car.peak est défini]
  outputs_dir/qa_tt_car.json     contrôles qualité
  outputs_dir/assumptions.jsonl  C1_car_engine, C2_car_snapping, C3_car_peak
"""
from __future__ import annotations

import importlib
import json
import socket
import subprocess
import time

import numpy as np
import pandas as pd
import requests

from common import cli, get_logger, interim_dir, load_config, log_assumption, outputs_dir, processed_dir

t04 = importlib.import_module("04_transit_times")      # load_od (sans démarrer r5py)

LOG = get_logger("06_car_times")

DEFAULT_IMAGE = "ghcr.io/project-osrm/osrm-backend:v5.27.1"
DEFAULT_PORT = 5000
DEFAULT_MAX_CAR_SPEED_KMH = 130
PROFILE = "/opt/car.lua"                 # profil voiture livré avec l'image OSRM
BASE = "osm_clip.osrm"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", *args]
    LOG.info("$ %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise SystemExit(f"Échec ({res.returncode}) : {' '.join(cmd)}\n{res.stderr.strip()[-2000:]}")
    return res


def osrm_version(image: str) -> str:
    return docker("run", "--rm", image, "osrm-routed", "--version").stdout.strip()


def build_graph(cfg: dict, image: str, rebuild: bool) -> tuple[dict, float]:
    """osrm-extract / partition / customize sur l'extrait OSM de l'origine (cache)."""
    idir = interim_dir(cfg)
    osm = idir / "osm_clip.osm.pbf"
    if not osm.exists():
        raise SystemExit(f"{osm} absent : lancer l'étape 02.")
    odir = idir / "osrm"
    odir.mkdir(exist_ok=True)
    done = odir / f"{BASE}.mldgr"
    if done.exists() and not rebuild and done.stat().st_mtime > osm.stat().st_mtime:
        LOG.info("Graphe OSRM en cache : %s", odir)
        return {"cached": True}, 0.0
    link = odir / "osm_clip.osm.pbf"
    link.unlink(missing_ok=True)
    link.hardlink_to(osm) if osm.stat().st_dev == odir.stat().st_dev else link.symlink_to(osm.resolve())
    t0 = time.time()
    vol = ["-v", f"{odir.resolve()}:/data"]
    timings = {}
    for step, args in (("extract", ["-p", PROFILE, "/data/osm_clip.osm.pbf"]),
                       ("partition", [f"/data/{BASE}"]),
                       ("customize", [f"/data/{BASE}"])):
        t = time.time()
        docker("run", "--rm", *vol, image, f"osrm-{step}", *args)
        timings[step] = round(time.time() - t, 1)
        LOG.info("osrm-%s : %.0f s", step, timings[step])
    return {"cached": False, "timings_s": timings}, time.time() - t0


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def start_server(cfg: dict, image: str, port: int, n_points: int) -> str:
    if not port_free(port):
        raise SystemExit(f"Port {port} déjà occupé : changer car.osrm_port.")
    odir = (interim_dir(cfg) / "osrm").resolve()
    name = f"osrm_{cfg['origin']['slug']}"
    docker("rm", "-f", name, check=False)
    docker("run", "-d", "--rm", "--name", name, "--network", "host", "-v", f"{odir}:/data", image,
           "osrm-routed", "--algorithm", "mld", "--port", str(port),
           "--max-table-size", str(max(100, n_points)), f"/data/{BASE}")
    for _ in range(120):
        try:
            requests.get(f"http://127.0.0.1:{port}/nearest/v1/driving/0,0", timeout=2)
            return name
        except requests.ConnectionError:
            time.sleep(1)
    logs = docker("logs", name, check=False).stderr[-2000:]
    docker("rm", "-f", name, check=False)
    raise SystemExit(f"osrm-routed ne répond pas sur le port {port}\n{logs}")


def table(port: int, origins, dest) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Matrice OSRM ; renvoie (OD : durée, distance) et (écart d'accrochage par point)."""
    pts = pd.concat([origins[["id", "geometry"]].assign(role="origin"),
                     dest[["id", "geometry"]].assign(role="destination")], ignore_index=True)
    coords = ";".join(f"{g.x:.6f},{g.y:.6f}" for g in pts.geometry)
    no = len(origins)
    src = ";".join(str(i) for i in range(no))
    dst = ";".join(str(i) for i in range(no, len(pts)))
    url = (f"http://127.0.0.1:{port}/table/v1/driving/{coords}"
           f"?sources={src}&destinations={dst}&annotations=duration,distance")
    r = requests.get(url, timeout=600)
    r.raise_for_status()
    js = r.json()
    if js.get("code") != "Ok":
        raise SystemExit(f"OSRM /table : {js.get('code')} {js.get('message')}")
    dur, dist = np.array(js["durations"], dtype=float), np.array(js["distances"], dtype=float)
    rows = [(origins["id"].iloc[i], dest["id"].iloc[j], dur[i, j], dist[i, j])
            for i in range(no) for j in range(len(dest))]
    od = pd.DataFrame(rows, columns=["from_id", "to_id", "dur_s", "dist_m"])
    snap = pd.DataFrame({"id": pts["id"], "role": pts["role"],
                         "snap_m": [w["distance"] for w in js["sources"]] + [w["distance"] for w in js["destinations"]]})
    return od, snap


def motorway_split(port: int, origins, dest) -> pd.DataFrame:
    """Par OD : durée de l'itinéraire OSRM (/route) et part passée sur autoroute.

    Une étape (step) compte comme autoroute si l'une de ses intersections porte la
    classe « motorway » (highway=motorway et bretelles, profil car d'OSRM).
    """
    rows = []
    with requests.Session() as sess:
        for _, o in origins.iterrows():
            for _, d in dest.iterrows():
                url = (f"http://127.0.0.1:{port}/route/v1/driving/{o.geometry.x:.6f},{o.geometry.y:.6f};"
                       f"{d.geometry.x:.6f},{d.geometry.y:.6f}?steps=true&overview=false")
                js = sess.get(url, timeout=60).json()
                if js.get("code") != "Ok" or not js.get("routes"):
                    rows.append((o["id"], d["id"], np.nan, np.nan))
                    continue
                r = js["routes"][0]
                mw = sum(st["duration"] for leg in r["legs"] for st in leg["steps"]
                         if any("motorway" in it.get("classes", []) for it in st["intersections"]))
                rows.append((o["id"], d["id"], r["duration"], mw))
    return pd.DataFrame(rows, columns=["from_id", "to_id", "route_s", "motorway_s"])


def peak_params(cfg: dict):
    """(source, {motorway, other}) en % pour la région de l'origine, ou None."""
    peak = (cfg.get("car") or {}).get("peak") or {}
    c = (peak.get("congestion_pct") or {}).get(cfg["region"]["id"])
    return (peak.get("source"), c) if c else None


def stats(x: pd.Series) -> dict:
    x = x.dropna()
    return {"n": int(len(x)), "median": round(float(x.median()), 1), "p90": round(float(x.quantile(0.9)), 1),
            "max": round(float(x.max()), 1)}


def main() -> None:
    parser = cli("Étape 06 : temps en voiture (OSRM, sans trafic).")
    parser.add_argument("--rebuild", action="store_true", help="reconstruit le graphe OSRM")
    args = parser.parse_args()
    cfg = load_config(args.config)
    car = cfg.get("car") or {}
    if not car.get("enabled", True):
        LOG.info("car.enabled = false : étape sautée")
        return
    if car.get("engine", "osrm") != "osrm":
        raise SystemExit(f"car.engine = {car.get('engine')} non géré (seulement osrm).")
    image = car.get("osrm_image", DEFAULT_IMAGE)
    port = int(car.get("osrm_port", DEFAULT_PORT))
    vmax = float((cfg.get("qa") or {}).get("max_plausible_car_speed_kmh", DEFAULT_MAX_CAR_SPEED_KMH))
    t_start = time.time()

    origins, dest = t04.load_od(cfg)
    LOG.info("%d origines, %d destinations", len(origins), len(dest))
    version = osrm_version(image)
    build, t_build = build_graph(cfg, image, args.rebuild)

    name = start_server(cfg, image, port, len(origins) + len(dest))
    peak = peak_params(cfg)
    try:
        t0 = time.time()
        od, snap = table(port, origins, dest)
        t_table = time.time() - t0
        split = motorway_split(port, origins, dest) if peak else None
    finally:
        docker("rm", "-f", name, check=False)

    od["car_time"] = od["dur_s"] / 60.0
    od["car_distance_km"] = od["dist_m"] / 1000.0
    cols = ["from_id", "to_id", "car_time", "car_distance_km"]
    if peak:
        # Heure de pointe : part autoroute / hors autoroute du temps fluide (/route), appliquée au temps de /table
        od = od.merge(split, on=["from_id", "to_id"], how="left")
        od["motorway_share"] = (od["motorway_s"] / od["route_s"]).where(od["route_s"] > 0, 0.0)
        cm, co = float(peak[1]["motorway"]) / 100, float(peak[1]["other"]) / 100
        od["car_time_peak"] = od["car_time"] * (od["motorway_share"] * (1 + cm) + (1 - od["motorway_share"]) * (1 + co))
        cols += ["car_time_peak", "motorway_share"]
    out = od[cols]
    pdir = processed_dir(cfg)
    out.to_parquet(pdir / "tt_car.parquet", index=False)
    LOG.info("Écrit : %s (%d OD, %d sans temps) — matrice en %.1f s", pdir / "tt_car.parquet", len(out),
             int(out["car_time"].isna().sum()), t_table)

    # Contrôles
    dist_cols = {o: f"dist_km_{o}" for o in origins["id"]}
    geo = dest.melt(id_vars="id", value_vars=[c for c in dist_cols.values() if c in dest.columns],
                    var_name="col", value_name="geo_km")
    geo["from_id"] = geo["col"].str.removeprefix("dist_km_")
    q = out.merge(geo.rename(columns={"id": "to_id"})[["from_id", "to_id", "geo_km"]], on=["from_id", "to_id"])
    q["speed_kmh"] = q["geo_km"] / (q["car_time"] / 60)
    q["route_speed_kmh"] = q["car_distance_km"] / (q["car_time"] / 60)
    q["detour"] = q["car_distance_km"] / q["geo_km"]
    names = dest.set_index("id")["name"]
    fast = q[q["route_speed_kmh"] > vmax]
    qa = {
        "origin": cfg["origin"]["slug"], "osrm": version, "image": image, "build": build,
        "n_od": int(len(out)), "n_nan": int(out["car_time"].isna().sum()),
        "car_time_by_origin": {o: stats(g["car_time"]) for o, g in out.groupby("from_id")},
        "snap_m": {"origins": stats(snap.loc[snap.role == "origin", "snap_m"]),
                   "destinations": stats(snap.loc[snap.role == "destination", "snap_m"]),
                   "destinations_gt_200m": snap[(snap.role == "destination") & (snap.snap_m > 200)]
                   .assign(name=lambda d: d["id"].map(names)).round(0).to_dict("records")},
        "route_speed_kmh": stats(q["route_speed_kmh"]),
        "speed_as_crow_flies_kmh": stats(q["speed_kmh"]),
        "detour_ratio": stats(q["detour"].replace(np.inf, np.nan)),
        "route_speed_gt_threshold": {"threshold_kmh": vmax, "n": int(len(fast))},
        "timings_s": {"build": round(t_build, 1), "table": round(t_table, 1), "total": round(time.time() - t_start, 1)},
    }
    if peak:
        dev = (od["route_s"] - od["dur_s"]).abs() / od["dur_s"].replace(0, np.nan)
        qa["peak"] = {"source": peak[0], "congestion_pct": peak[1],
                      "car_time_peak_by_origin": {o: stats(g["car_time_peak"]) for o, g in od.groupby("from_id")},
                      "motorway_share_by_origin": {o: stats(100 * g["motorway_share"]) for o, g in od.groupby("from_id")},
                      "route_vs_table_rel_diff": stats(100 * dev)}
        LOG.info("Pointe (%s) : voiture %s ; part autoroute %% %s ; écart /route vs /table %% %s", peak[0],
                 qa["peak"]["car_time_peak_by_origin"], qa["peak"]["motorway_share_by_origin"],
                 qa["peak"]["route_vs_table_rel_diff"])
    (outputs_dir(cfg) / "qa_tt_car.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Voiture par origine : %s", qa["car_time_by_origin"])
    LOG.info("Accrochage destinations : %s ; > 200 m : %d", qa["snap_m"]["destinations"],
             len(qa["snap_m"]["destinations_gt_200m"]))
    LOG.info("Vitesse sur itinéraire : %s ; vol d'oiseau : %s ; détour : %s ; > %.0f km/h : %d OD",
             qa["route_speed_kmh"], qa["speed_as_crow_flies_kmh"], qa["detour_ratio"], vmax, len(fast))

    log_assumption(
        cfg, "06", "C1_car_engine",
        (f"Temps voiture : {version} (image {image}), profil « car » par défaut (vitesses par type de route et "
         f"limitations OSM), algorithme MLD, sur l'extrait OSM de l'étape 02 (zone de découpe = rayon + marge). Temps "
         f"SANS trafic, sans recherche de stationnement ni marche jusqu'au véhicule : le rapport TC/voiture est donc "
         f"un majorant de l'écart réel aux heures de pointe."),
        (f"Pkw-Zeiten: {version} (Image {image}), Standardprofil „car“ (Geschwindigkeiten nach Straßentyp und "
         f"OSM-Tempolimits), Algorithmus MLD, auf dem OSM-Auszug aus Schritt 02 (Zuschnitt = Radius + Puffer). Zeiten "
         f"OHNE Verkehr, ohne Parkplatzsuche und Fußweg zum Fahrzeug: Das Verhältnis ÖV/Pkw überzeichnet daher den "
         f"realen Abstand in der Hauptverkehrszeit."),
        {"osrm": version, "image": image, "profile": PROFILE, "algorithm": "mld"})
    log_assumption(
        cfg, "06", "C2_car_snapping",
        (f"Mêmes points que 04 (sans déplacement « îlots piétons ») ; OSRM les accroche à la route carrossable la plus "
         f"proche. Écart d'accrochage des destinations : {qa['snap_m']['destinations']} m ; "
         f"{len(qa['snap_m']['destinations_gt_200m'])} à plus de 200 m."),
        (f"Gleiche Punkte wie in 04 (ohne Verschiebung „Fußweg-Inseln“); OSRM rastet sie auf die nächste befahrbare "
         f"Straße ein. Abstand der Ziele: {qa['snap_m']['destinations']} m; "
         f"{len(qa['snap_m']['destinations_gt_200m'])} mit mehr als 200 m."),
        qa["snap_m"])
    if peak:
        cm, co = peak[1]["motorway"], peak[1]["other"]
        log_assumption(
            cfg, "06", "C3_car_peak",
            (f"Voiture en heure de pointe du matin : temps fluide OSRM multiplié, par classe de route, par (1 + niveau "
             f"de congestion) ; autoroutes (classe OSRM « motorway ») +{cm} %, autres routes +{co} % ({peak[0]} ; "
             f"niveau de congestion = temps en plus par rapport au trafic fluide, moyenne de la zone métropolitaine à "
             f"8 h). Même facteur partout dans la zone (pas de variation locale) ; ni stationnement ni marche jusqu'au "
             f"véhicule. Part autoroute des trajets (gare) : {qa['peak']['motorway_share_by_origin'].get('gare')} %."),
            (f"Pkw in der Morgenspitze: OSRM-Freiflusszeit je Straßenklasse mal (1 + Stauniveau); Autobahnen "
             f"(OSRM-Klasse „motorway“) +{cm} %, übrige Straßen +{co} % ({peak[0]}; Stauniveau = zusätzliche Fahrzeit "
             f"gegenüber Freifluss, Mittel der Metropolregion um 8 Uhr). Gleicher Faktor im ganzen Gebiet (keine lokale "
             f"Variation); ohne Parkplatzsuche und Fußweg zum Fahrzeug. Autobahnanteil der Fahrten (Bahnhof): "
             f"{qa['peak']['motorway_share_by_origin'].get('gare')} %."),
            qa["peak"])
    LOG.info("Terminé en %.0f s.", time.time() - t_start)


if __name__ == "__main__":
    main()
