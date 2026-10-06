# So nah, so fern

**Ce que les kilomètres ne disent pas des transports en commun** · [Deutsche Version](README.md)

Carte interactive de l'écart entre distance à vol d'oiseau et temps réel porte à porte en transports en commun, au départ de Garches (Hauts-de-Seine, 12 km à l'ouest de Paris), comparé à une commune équivalente de la région Rhin-Main (Kronberg im Taunus ; contrôle : Bad Soden am Taunus).

Chaque chiffre sort du calcul ; chaque hypothèse est consignée dans `outputs/<origine>/assumptions.jsonl`.

## La méthode en bref

| Étape | Contenu |
|---|---|
| Origines | gare et centre-ville de la commune d'origine |
| Destinations | point officiel (chef-lieu IGN / « Kern der Gemeinde » BKG) de chaque commune à 30 km ; ville-centre découpée (Paris : 17 unités, « Paris Centre » = 1er–4e ; Francfort : 45 Stadtteile) |
| Temps | r5py (R5), mardi en période scolaire, un départ par minute de 7h30 à 9h00, médiane (+ p25/p75), marche à 4,5 km/h, attente incluse |
| Indicateurs | vitesse effective, indice de paradoxe (rang en temps − rang en distance), écart au temps attendu (régression log-log), ratio TC/voiture, « zones mortes » (< 8 km, > 60 min) |

Tous les paramètres : [`config/default.yaml`](config/default.yaml). Origines : [`config/garches.yaml`](config/garches.yaml), [`config/kronberg.yaml`](config/kronberg.yaml). Sources et licences : [`config/sources.yaml`](config/sources.yaml).

## Installation

Il faut Python ≥ 3.12 avec [uv](https://docs.astral.sh/uv/), Java ≥ 21 (pour R5), [osmium-tool](https://osmcode.org/osmium-tool/), ~2 Go d'espace disque, **≥ 16 Go de RAM** (R5 demande ~11 Go de tas pour l'Île-de-France).

```bash
# Linux (Debian/Ubuntu)
sudo apt-get install -y openjdk-21-jdk-headless osmium-tool
uv sync
```

macOS : Java via Temurin par exemple ; osmium-tool via conda-forge dans `.tools/osmium` (Homebrew échoue sur macOS 27) :
`conda create -p ./.tools/osmium -c conda-forge osmium-tool`

## Pipeline

```bash
uv run python pipeline/00_download.py                                   # toutes les sources (~1,7 Go) + MANIFEST.json
uv run python pipeline/01_prepare_units.py --config config/garches.yaml # communes, points, populations, distances
uv run python pipeline/02_clip_osm_gtfs.py --config config/garches.yaml # découpe OSM/GTFS, contrôle du jour
uv run python pipeline/03_select_twin_city.py --config config/twin_selection.yaml
uv run python pipeline/04_transit_times.py --config config/garches.yaml # matrice de temps (r5py)
uv run python pipeline/05_transfers.py --config config/garches.yaml     # correspondances (en cours, voir plus bas)
uv run python pipeline/07_indicators.py --config config/garches.yaml    # indicateurs, CSV, synthèse
```

Pour une autre commune : copier `config/garches.yaml` et adapter `origin`, `core_city`, `analysis.date`. Les GTFS ne couvrent qu'environ un mois : la date doit être dans le fichier (02 le vérifie).

Charge CPU : `routing.jvm_active_processors` limite les cœurs utilisés par R5 (2 par défaut ; à augmenter sur une machine dédiée ou dans le cloud).

## État d'avancement (06/10/2026)

- [x] 00–03 : téléchargement, unités spatiales, découpe, choix de la commune jumelle (`outputs/twin_selection/`)
- [x] 04 : temps de trajet depuis Garches (`data/processed/garches/tt_transit.parquet`, non versionné)
- [ ] 05 : correspondances. Nouvelle méthode : TravelTimeMatrix avec `max_public_transport_rides` = 1…4 ; correspondances = plus petit k dont la médiane ≤ médiane sans limite + `transfer_tolerance_min`, moins 1. Itinéraires détaillés (DetailedItineraries) seulement pour les destinations mises en avant (tops, zones mortes) à `itinerary_departure`. L'ancienne variante (DetailedItineraries pour toutes les OD) était trop lente.
- [ ] 04–07 pour Kronberg et Bad Soden
- [ ] 06 : temps en voiture (OSRM, Docker)
- [ ] 08 isochrones · 09 cartogramme temporel · 10 export web · `web/` (MapLibre + D3, DE/FR)
- [ ] Phase 2 : matrice de toutes les communes entre elles
- [ ] Note méthodologique (1 page, DE/FR)

## Licences des données

GTFS IDFM et OpenStreetMap : ODbL · IGN Admin Express, INSEE : Licence Ouverte 2.0 · DELFI/gtfs.de : CC BY 4.0 · BKG VG250-EW : dl-de/by-2-0 · Ville de Francfort : dl-de/by-2-0. Détails dans `config/sources.yaml`.
