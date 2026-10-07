# So nah, so fern

**Was Kilometer über den Nahverkehr verschweigen** · [Version française](README.fr.md)

Interaktive Karte zur Lücke zwischen Luftlinie und tatsächlicher Tür-zu-Tür-Reisezeit im öffentlichen Verkehr – ausgehend von Garches (Hauts-de-Seine, 12 km westlich von Paris) und im Vergleich mit einer ähnlichen Gemeinde im Rhein-Main-Gebiet (Kronberg im Taunus, Kontrolle: Bad Soden am Taunus).

Paris ist Hauptstadt und Mittelpunkt eines strahlenförmigen Netzes, Frankfurt eine Regionalmetropole: Im Umkreis von 30 km leben 10,13 Mio. gegenüber 2,85 Mio. Menschen. Die Karte zeigt daher neben Zeit je Kilometer auch die Zeit ins Zentrum der Kernstadt und die in 30, 45 und 60 Minuten erreichbare Bevölkerung.

Alle Zahlen stammen aus der Berechnung; jede Annahme wird in `outputs/<ursprung>/assumptions.jsonl` protokolliert.

## Methode in Kürze

| Schritt | Inhalt |
|---|---|
| Ursprünge | Bahnhof und Ortsmitte der Ursprungsgemeinde |
| Ziele | amtlicher Punkt (IGN chef-lieu / BKG „Kern der Gemeinde“) jeder Gemeinde im Umkreis von 30 km; Kernstadt in Bezirke geteilt (Paris: 17 Einheiten, „Paris Centre“ = 1.–4.; Frankfurt: 45 Stadtteile) |
| Zeit | r5py (R5), Werktag in der Schulzeit, Abfahrt jede Minute 07:30–09:00, Median (+ p25/p75), Fußweg 4,5 km/h inklusive Wartezeit |
| Kennzahlen | effektive Geschwindigkeit, Paradoxie-Index (Rang Zeit − Rang Entfernung), Abweichung von der erwarteten Zeit (log-log-Regression), Verhältnis ÖV/Auto, „tote Zonen“ (< 8 km, > 60 min) |

Alle Parameter: [`config/default.yaml`](config/default.yaml). Ursprung: [`config/garches.yaml`](config/garches.yaml), [`config/kronberg.yaml`](config/kronberg.yaml). Quellen und Lizenzen: [`config/sources.yaml`](config/sources.yaml).

## Installation

Benötigt: Python ≥ 3.12 mit [uv](https://docs.astral.sh/uv/), Java ≥ 21 (für R5), [osmium-tool](https://osmcode.org/osmium-tool/), ~2 GB Speicherplatz, **≥ 16 GB RAM** (R5 braucht ~11 GB Heap für die Île-de-France).

```bash
# Linux (Debian/Ubuntu)
sudo apt-get install -y openjdk-21-jdk-headless osmium-tool
uv sync
```

Alternativ osmium-tool über conda-forge in `.tools/osmium` (wird von der Pipeline bevorzugt):
`conda create -p ./.tools/osmium -c conda-forge osmium-tool`

## Pipeline

```bash
uv run python pipeline/00_download.py                                   # alle Quellen (~5 GB) + MANIFEST.json (Spiegel, falls Geofabrik nicht erreichbar)
uv run python pipeline/01_prepare_units.py --config config/garches.yaml # Gemeinden, Punkte, Einwohner, Distanzen
uv run python pipeline/02_clip_osm_gtfs.py --config config/garches.yaml # OSM/GTFS-Zuschnitt, Prüfung des Stichtags
uv run python pipeline/03_select_twin_city.py --config config/twin_selection.yaml
uv run python pipeline/04_transit_times.py --config config/garches.yaml # Reisezeitmatrix (r5py), Fußweg-Inseln
uv run python pipeline/05_transfers.py --config config/garches.yaml     # Umstiege (Matrizen mit k Fahrzeugen)
uv run python pipeline/06_car_times.py --config config/garches.yaml     # Pkw-Zeiten (OSRM in Docker)
uv run python pipeline/07_indicators.py --config config/garches.yaml    # Kennzahlen, CSV, Synthese
uv run python pipeline/05_transfers.py --config config/garches.yaml --itineraries  # Beispielrouten (Tops, tote Zonen)
uv run python pipeline/08_isochrones.py --config config/garches.yaml    # Isochronen, Entfernungsringe
uv run python pipeline/09_cartogram.py --config config/garches.yaml     # Zeitkartogramm
uv run python pipeline/10_export_web.py --config config/garches.yaml    # web/data/<slug>.json
uv run python pipeline/11_sensitivity.py --config config/sensitivity/garches_townhall.yaml  # nach 01, 04 bis 07 der Variante
uv run python pipeline/12_all_pairs.py --config config/garches.yaml     # Phase 2: alle Einheiten untereinander
uv run python pipeline/13_export_phase2_web.py --config config/garches.yaml  # Phase 2 für die Web-Karte (web/data/phase2_<region>.json)
uv run python pipeline/14_time_windows.py                               # Zeitfenster-Sensitivität (nach den Varianten in config/sensitivity/)
```

Für eine andere Gemeinde: `config/garches.yaml` kopieren und `origin`, `core_city`, `analysis.date` anpassen. Die GTFS-Feeds decken nur ~1 Monat ab – das Stichdatum muss im Feed liegen (02 prüft das).

CPU-Last: `routing.jvm_active_processors` begrenzt die Kerne von R5 (Standard: 4).

## Stand (07.10.2026)

- [x] 00 bis 12 für Garches, Kronberg und Bad Soden; Ergebnisse in `outputs/`, Kurzfassung in `docs/NEXT_STEPS.md`
- [x] Web-Karte `web/` (MapLibre + D3, DE/FR, offline): `cd web && python3 -m http.server`
- [x] Sensitivität Zielpunkt (Rathaus OSM, `config/sensitivity/`), Toleranz der Umstiege
- [x] Phase 2: alle Gemeinden ≤ 30 km von Paris bzw. Frankfurt untereinander (`outputs/phase2/`)
- [x] Methodenpapier DE/FR: `docs/methodologie.md`

06 und 12 (OSRM) brauchen einen laufenden Docker-Daemon.

## Lizenzen der Daten

IDFM GTFS und OpenStreetMap: ODbL · IGN Admin Express, INSEE: Licence Ouverte 2.0 · DELFI/gtfs.de: CC BY 4.0 · BKG VG250-EW: dl-de/by-2-0 · Stadt Frankfurt: dl-de/by-2-0. Details in `config/sources.yaml`.
