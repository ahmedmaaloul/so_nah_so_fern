# Prochaines étapes / Nächste Schritte (06/10/2026)

Note de passage de relais vers l'environnement cloud. Les données (`data/`) ne sont
pas versionnées : relancer `00_download.py`, puis 01 → 02 → 04 pour chaque origine
(`garches`, `kronberg`, `bad_soden`). `data/raw/MANIFEST.json` garde la trace (sha256,
dates) des fichiers utilisés lors du premier calcul local.

## Points d'attention

1. **Feeds GTFS « latest »** : IDFM et gtfs.de ne couvrent qu'environ un mois
   (03/10 → 04/11 et 03/10 → 02/11/2026 lors du premier téléchargement). Les dates
   d'analyse (13/10 FR, 20/10 DE) doivent rester dans le feed retéléchargé ;
   `02_clip_osm_gtfs.py` le vérifie et s'arrête sinon.
2. **Mémoire R5** : ≥ 11 Go de tas pour l'IDF (8 Go → OutOfMemoryError). Dans le
   cloud, relever `routing.jvm_active_processors` (2 par défaut).
3. **Cache r5py** : r5py crée `~/.cache/r5py/<nom de fichier>` et réutilise un lien
   existant de même nom. Tous les extraits s'appellent `osm_clip.osm.pbf` → 04 passe
   par `stage_inputs()` (liens `data/interim/<slug>/r5_inputs/<slug>__<fichier>`).
   Réutiliser cette fonction dans tout nouveau script r5py.

## Résultats 04 Garches (premier calcul local)

- 848 OD (2 origines × 424 destinations) ; 2,9 s de matrice une fois le réseau en cache.
- 70 OD sans temps = 35 destinations, toutes à plus de 17 km. Andrésy (78015) : point
  accroché à un îlot piéton déconnecté → problème d'accrochage, pas de service.
  Les 34 autres : petits villages non atteints en < 180 min dans la fenêtre.
- p50 depuis la gare : min 9, médiane 75, max 148 min (Chavenay, 14,8 km).
- R5 inclut bien la marche directe (aucun p50 > temps de marche seule).
- Ville-d'Avray : 26 min pour 1,1 km (p25 = p50 = p75) → à vérifier (marche ?).

## À faire

1. **05 correspondances, nouvelle méthode** (l'actuel `05_transfers.py` avec
   `DetailedItineraries` pour toutes les OD est trop lent : > 17 min par départ ;
   OutOfMemoryError avec une fenêtre d'une minute) :
   - `TravelTimeMatrix` avec `max_public_transport_rides` = 1…`max_rides_tested` ;
   - correspondances = (plus petit k dont la médiane ≤ médiane sans limite +
     `transfer_tolerance_min`) − 1 ; marche seule → 0 ;
   - itinéraires détaillés (`DetailedItineraries`, fenêtre 10 min, départ
     `itinerary_departure`) seulement pour les destinations mises en avant (tops,
     zones mortes) → carte web.
2. Andrésy : accrocher le point à la composante connexe principale du réseau piéton.
3. 04–05–07 pour `kronberg` et `bad_soden` ; tester `07_indicators.py` (écrit, jamais lancé).
4. 06 voiture (OSRM via Docker), 08 isochrones (`r5py.Isochrones`), 09 cartogramme
   temporel (angle conservé, rayon = temps, polygones déformés), 10 export web,
   `web/` (MapLibre + D3, DE par défaut, FR).
5. Sensibilité : relancer 04 avec `analysis.destination_point: osm_townhall`
   (surtout DE, où le point BKG est à 290 m en médiane de la mairie OSM).
6. Phase 2 : matrice de toutes les communes entre elles (rayon 30 km autour de
   Paris et de Francfort).
7. Note méthodologique d'une page (DE/FR), à partir des `assumptions.jsonl`.
