# So nah, so fern

**Was Kilometer über den Nahverkehr verschweigen** · [Version française](README.fr.md)

Interaktive Karte zur Lücke zwischen Luftlinie und tatsächlicher Tür-zu-Tür-Reisezeit im öffentlichen Verkehr – ausgehend von Garches (Hauts-de-Seine, 12 km westlich von Paris) und im Vergleich mit einer ähnlichen Gemeinde im Rhein-Main-Gebiet (Kronberg im Taunus, Kontrolle: Bad Soden am Taunus).

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

macOS: Java z. B. über Temurin; osmium-tool über conda-forge in `.tools/osmium` (Homebrew schlägt auf macOS 27 fehl):
`conda create -p ./.tools/osmium -c conda-forge osmium-tool`

## Pipeline

```bash
uv run python pipeline/00_download.py                                   # alle Quellen (~1,7 GB) + MANIFEST.json
uv run python pipeline/01_prepare_units.py --config config/garches.yaml # Gemeinden, Punkte, Einwohner, Distanzen
uv run python pipeline/02_clip_osm_gtfs.py --config config/garches.yaml # OSM/GTFS-Zuschnitt, Prüfung des Stichtags
uv run python pipeline/03_select_twin_city.py --config config/twin_selection.yaml
uv run python pipeline/04_transit_times.py --config config/garches.yaml # Reisezeitmatrix (r5py)
uv run python pipeline/05_transfers.py --config config/garches.yaml     # Umstiege (in Arbeit, s. u.)
uv run python pipeline/07_indicators.py --config config/garches.yaml    # Kennzahlen, CSV, Synthese
```

Für eine andere Gemeinde: `config/garches.yaml` kopieren und `origin`, `core_city`, `analysis.date` anpassen. Die GTFS-Feeds decken nur ~1 Monat ab – das Stichdatum muss im Feed liegen (02 prüft das).

CPU-Last: `routing.jvm_active_processors` begrenzt die Kerne von R5 (Standard 2; auf einem dedizierten Rechner oder in der Cloud höher setzen).

## Stand (06.10.2026)

- [x] 00–03: Download, Gebietseinheiten, Zuschnitt, Wahl der Vergleichsgemeinde (`outputs/twin_selection/`)
- [x] 04: Reisezeiten Garches (`data/processed/garches/tt_transit.parquet`, nicht versioniert)
- [ ] 05: Umstiege – neue Methode: TravelTimeMatrix mit `max_public_transport_rides` = 1…4; Umstiege = kleinstes k mit Median ≤ unbeschränkter Median + `transfer_tolerance_min`, minus 1. Detaillierte Routen (DetailedItineraries) nur für hervorgehobene Ziele (Tops, tote Zonen) zu `itinerary_departure`. Die bisherige Variante (DetailedItineraries für alle OD) war zu langsam.
- [ ] 04–07 für Kronberg und Bad Soden
- [ ] 06: Autofahrzeiten (OSRM, Docker)
- [ ] 08 Isochronen · 09 Zeitkartogramm · 10 Web-Export · `web/` (MapLibre + D3, DE/FR)
- [ ] Phase 2: Matrix aller Gemeinden untereinander
- [ ] Methodenpapier (1 Seite, DE/FR)

## Lizenzen der Daten

IDFM GTFS und OpenStreetMap: ODbL · IGN Admin Express, INSEE: Licence Ouverte 2.0 · DELFI/gtfs.de: CC BY 4.0 · BKG VG250-EW: dl-de/by-2-0 · Stadt Frankfurt: dl-de/by-2-0. Details in `config/sources.yaml`.
