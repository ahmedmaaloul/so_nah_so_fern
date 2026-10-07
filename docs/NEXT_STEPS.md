# Prochaines étapes / Nächste Schritte (07/10/2026)

Les données (`data/`) ne sont pas versionnées : relancer `00_download.py`, puis
01 → 02 → 04 → 05 → 07 → 05 `--itineraries` pour chaque origine (`garches`,
`kronberg`, `bad_soden`). `data/raw/MANIFEST.json` garde la trace (sha256, dates,
URL réellement utilisée) des fichiers du dernier calcul.

## Points d'attention

1. **Feeds GTFS « latest »** : IDFM et gtfs.de ne couvrent qu'environ un mois. Les
   dates d'analyse (13/10 FR, 20/10 DE) doivent rester dans le feed retéléchargé ;
   `02_clip_osm_gtfs.py` le vérifie et s'arrête sinon.
2. **Mémoire R5** : ≥ 11 Go de tas pour l'IDF (8 Go → OutOfMemoryError). Une seule
   JVM à la fois sur une machine de 16 Go (le processus 05 `--itineraries` IDF monte
   à ~12 Go). `routing.jvm_active_processors` = nombre de cœurs disponibles.
3. **Cache r5py** : r5py crée `~/.cache/r5py/<nom de fichier>` et réutilise un lien
   existant de même nom. Tous les extraits s'appellent `osm_clip.osm.pbf` → 04 passe
   par `stage_inputs()` (liens `data/interim/<slug>/r5_inputs/<slug>__<fichier>`).
   Réutiliser cette fonction dans tout nouveau script r5py.
4. **Miroirs** : si download.geofabrik.de est injoignable, 00 bascule sur les
   `mirrors` de `config/sources.yaml` (GWDG pour l'Allemagne entière, OSM France
   pour l'IDF) ; 02 consigne `G0_mirror`. L'Allemagne entière (~4,9 Go) remplace les
   trois extraits régionaux : extrait Kronberg identique à l'ancien (même nombre de
   nœuds, ways, relations).
5. **Îlots piétons** : 04 et 05 déplacent les points accrochés à un morceau de réseau
   déconnecté (`fix_islands`, hypothèse `R4_islands`, paramètres `routing.islands`).
   Seul cas actuel : Andrésy (78015), déplacé de 15 m. À réutiliser dans 06/08.
6. **06 voiture** : OSRM v5.27.1 via Docker (`ghcr.io/project-osrm/osrm-backend`). Dans le
   conteneur cloud, le démon n'est pas lancé : `dockerd > /tmp/dockerd.log 2>&1 &` avant 06.
   Graphe en cache dans `data/interim/<slug>/osrm/` (construction 45 à 90 s).
7. **Durée de 05 `--itineraries`** : ~33 min par origine en IDF (≈ 20 destinations),
   5 à 7 min en Rhin-Main. Ne relancer que si les destinations mises en avant
   changent.

## Résultats (calcul cloud du 06–07/10/2026)

| | Garches | Kronberg | Bad Soden |
|---|---|---|---|
| Destinations | 424 | 119 | 121 |
| Sans temps (gare) | 34 | 0 | 0 |
| p50 gare : min / médiane / max | 9 / 75 / 148 | 7 / 72 / 152 | 3 / 72 / 150 |
| v_eff médiane pondérée pop. (gare) | 16,1 km/h | 16,0 km/h | 14,4 km/h |
| Part ≥ 2 correspondances (gare) | 87 % | 46 % | 47 % |
| Voiture médiane (gare, sans trafic) | 31,5 min | 23,8 min | 24,7 min |
| Ratio TC/voiture médian (gare) | 2,50 | 2,90 | 3,11 |

- Garches identique au premier calcul local hormis Andrésy (désormais 67 min depuis la gare).
- 05 : 0 étape non monotone, 0 cas « k max plus rapide que sans limite » ; 86 OD
  censurées (≥ 4 correspondances) à Garches, 0 à Kronberg, 6 à Bad Soden.
- Itinéraires 08:15 vs médiane : écart médian +2,8 min (Garches), −1,8 (Kronberg),
  +9,0 (Bad Soden, à expliquer : cadencement S-Bahn ?).

## À faire

1. Ville-d'Avray : 26 min pour 1,1 km (p25 = p50 = p75) → à vérifier (marche ?).
2. Sensibilité de `transfer_tolerance_min` (1 → 3, 5 min) sur les OD censurées de Garches.
3. Saclay : chaîne « L › L › 6132 › 4609 › 4609 » (même ligne deux fois de suite).
4. 08 isochrones (`r5py.Isochrones`), 09 cartogramme temporel (angle conservé,
   rayon = temps, polygones déformés), 10 export web, `web/` (MapLibre + D3, DE par
   défaut, FR).
5. Sensibilité : relancer 04 avec `analysis.destination_point: osm_townhall`
   (surtout DE, où le point BKG est à 290 m en médiane de la mairie OSM).
6. Phase 2 : matrice de toutes les communes entre elles (rayon 30 km autour de
   Paris et de Francfort).
7. Note méthodologique d'une page (DE/FR), à partir des `assumptions.jsonl`.
