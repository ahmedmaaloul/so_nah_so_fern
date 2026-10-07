# So nah, so fern

**Ce que les kilomètres ne disent pas des transports en commun** · [Deutsche Version](README.md)

Carte interactive de l'écart entre distance à vol d'oiseau et temps réel porte à porte en transports en commun, au départ de Garches (Hauts-de-Seine, 12 km à l'ouest de Paris), comparé à une commune équivalente de la région Rhin-Main (Kronberg im Taunus ; contrôle : Bad Soden am Taunus).

Paris est la capitale, au centre d'un réseau radial ; Francfort est une métropole régionale : dans un rayon de 30 km vivent 10,13 M contre 2,85 M d'habitants. La carte montre donc, en plus du temps par kilomètre, le temps vers le centre de la ville centre et la population atteinte en 30, 45 et 60 minutes.

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

Autre possibilité : osmium-tool via conda-forge dans `.tools/osmium` (utilisé en priorité par le pipeline) :
`conda create -p ./.tools/osmium -c conda-forge osmium-tool`

## Pipeline

```bash
uv run python pipeline/00_download.py                                   # toutes les sources (~5 Go) + MANIFEST.json (miroirs si Geofabrik injoignable)
uv run python pipeline/01_prepare_units.py --config config/garches.yaml # communes, points, populations, distances
uv run python pipeline/02_clip_osm_gtfs.py --config config/garches.yaml # découpe OSM/GTFS, contrôle du jour
uv run python pipeline/03_select_twin_city.py --config config/twin_selection.yaml
uv run python pipeline/04_transit_times.py --config config/garches.yaml # matrice de temps (r5py), îlots piétons
uv run python pipeline/05_transfers.py --config config/garches.yaml     # correspondances (matrices à k véhicules)
uv run python pipeline/06_car_times.py --config config/garches.yaml     # temps voiture (OSRM sous Docker)
uv run python pipeline/07_indicators.py --config config/garches.yaml    # indicateurs, CSV, synthèse
uv run python pipeline/05_transfers.py --config config/garches.yaml --itineraries  # itinéraires d'exemple (tops, zones mortes)
uv run python pipeline/08_isochrones.py --config config/garches.yaml    # isochrones, cercles de distance
uv run python pipeline/09_cartogram.py --config config/garches.yaml     # cartogramme temporel
uv run python pipeline/10_export_web.py --config config/garches.yaml    # web/data/<slug>.json
uv run python pipeline/11_sensitivity.py --config config/sensitivity/garches_townhall.yaml  # après 01, 04 à 07 de la variante
uv run python pipeline/12_all_pairs.py --config config/garches.yaml     # phase 2 : toutes les unités entre elles
uv run python pipeline/13_export_phase2_web.py --config config/garches.yaml  # phase 2 pour le site (web/data/phase2_<région>.json)
uv run python pipeline/14_time_windows.py                               # sensibilité à la plage horaire (après les variantes de config/sensitivity/)
uv run python pipeline/15_export_funding.py                             # argent public pour les TC (config/funding.yaml) pour le site
```

Pour une autre commune : copier `config/garches.yaml` et adapter `origin`, `core_city`, `analysis.date`. Les GTFS ne couvrent qu'environ un mois : la date doit être dans le fichier (02 le vérifie).

Charge CPU : `routing.jvm_active_processors` limite les cœurs utilisés par R5 (4 par défaut).

## État (07/10/2026)

- [x] 00 à 12 pour Garches, Kronberg et Bad Soden ; résultats dans `outputs/`, résumé dans `docs/NEXT_STEPS.md`
- [x] Carte web `web/` (MapLibre + D3, DE/FR, hors ligne) : `cd web && python3 -m http.server`
- [x] Sensibilité au point de destination (mairie OSM, `config/sensitivity/`), tolérance des correspondances
- [x] Phase 2 : toutes les communes à 30 km au plus de Paris ou de Francfort, entre elles (`outputs/phase2/`)
- [x] Note méthodologique DE/FR : `docs/methodologie.md`

06 et 12 (OSRM) ont besoin d'un démon Docker en marche.

## Licences des données

GTFS IDFM et OpenStreetMap : ODbL · IGN Admin Express, INSEE : Licence Ouverte 2.0 · DELFI/gtfs.de : CC BY 4.0 · BKG VG250-EW : dl-de/by-2-0 · Ville de Francfort : dl-de/by-2-0. Détails dans `config/sources.yaml`.
