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

## Fait le 07/10/2026 (cloud)

- 06 voiture (OSRM), 08 isochrones (grille de 200 m ; `r5py.Isochrones` ne rend que des
  contours simplifiés), 09 cartogramme temporel (temps des sommets interpolés entre les points
  des communes ; une première version sur la grille de 08 était trop bruitée), 10 export web,
  `web/` (MapLibre 4.7.1 + D3 7.9.0 vendorisés), 11 sensibilité (mairie OSM), 12 phase 2,
  `docs/methodologie.md`.
- Ville-d'Avray : pas d'anomalie, trajet à pied (26 min ≈ 1,95 km de cheminement pour 1,13 km
  à vol d'oiseau) ; d'où p25 = p50 = p75.
- Saclay : « L › L » et « 4609 › 4609 » sont de vrais changements de véhicule sur la même ligne.
- Tolérance des correspondances (`05 --tolerance-sensitivity 1 3 5`) : part ≥ 2 correspondances
  Garches 87 / 81 / 77 %, Kronberg 45 / 35 / 32 %, Bad Soden 46 / 41 / 30 %.
- Variantes : fichier court avec `extends:` et `run_tag:` (ex. `config/sensitivity/*.yaml`),
  sorties dans `<slug>__<run_tag>`, extraits OSM/GTFS partagés.

## Pistes

1. ~~Intégrer la phase 2 au site~~ : fait (13, vue « Alle Gemeinden / Toutes les communes »).
2. ~~Autres plages horaires~~ : fait (variantes `creuse`, `soir_pointe`, `soiree`, synthèse 14,
   `outputs/time_windows/`). Depuis la gare, médiane TC : Garches 75 / 80 / 74 / 84 min
   (07:30, 10:00, 17:30, 20:30), non atteintes 34 / 32 / 22 / 85 ; Kronberg 72,5 / 73,5 / 72,5 / 72
   (0 / 0 / 0 / 5) ; Bad Soden 72 / 72 / 71,5 / 74 (0 / 0 / 0 / 4).
3. ~~Autres jours~~ : fait (variantes `jeudi`, `samedi`, `dimanche`, `vacances`, `autre_mardi`,
   `14 --kind days`, `outputs/days/`). Jeudi = mardi (corrélation 1,000). Attention : le flux IDFM
   « latest » n'a plus, à partir du 20/10, 149 lignes du jour d'analyse (T4, bus 177, 137 …),
   y compris le 03/11 (jour d'école) ; les variantes FR `vacances` et `autre_mardi` mesurent
   cette lacune. 14 compte désormais les lignes actives le jour de référence et absentes le
   jour testé. Contrôle ligne par ligne ajouté à 02 (D2, `analysis.max_missing_route_share` = 1 %,
   `02 --routes-check-only`) : 13/10 FR 0,01 %, 20/10 DE 0,16 % ; il rejette 03/11 FR (3,08 %).
4. ~~Temps voiture en heure de pointe~~ : fait (06, `car.peak`, hypothèse C3). Pas de donnée ouverte de
   vitesses sur tout le réseau pour les deux régions ; facteur par classe de route tiré du TomTom
   Traffic Index 2025 (zone metro, 8 h, consulté le 07/10/2026) : Paris autoroutes +84,2 %, autres
   +57,9 % ; Francfort +36,9 % / +64,4 %. Part autoroute de chaque trajet par OSRM /route (classe
   « motorway »). Voiture en pointe (gare, médiane) : Garches 50,1 min, Kronberg 37,3, Bad Soden 38,2 ;
   TC/voiture 1,54 / 1,84 / 2,03 (fluide : 2,50 / 2,90 / 3,11). Limite : facteur moyen de la zone
   métropolitaine, sans variation locale. Phase 2 aussi (`12 --car-only`, 191 028 itinéraires /route,
   18 min en IDF) : TC/voiture médian IDF 2,54 → 1,50, Rhin-Main 2,98 → 1,96.
5. ~~Bad Soden, écart de +9 min~~ : expliqué. La S3 part du terminus de Bad Soden à xx:11 et xx:41 ;
   prêt à 08:15, on attend 26 min (08:41) contre 14,5 min en médiane sur 07:30 à 09:00, soit
   +11,5 min. Observé : +9,0 min (24 itinéraires commençant par la S3), dont 7,3 min d'attente
   avant le départ. 08:15 n'est pas changé (pas de choix d'heure par origine) ; le site affiche
   désormais l'attente avant le départ (`offset_min`, `meta.itinerary_departure`).

## Refonte de l'interface (prévue)

Préparé le 07/10/2026 :

- **shadcn/ui** : serveur MCP à déclarer localement (`npx shadcn@latest mcp init`), non versionné.
- **impeccable** (Paul Bakaus, Apache 2.0, https://github.com/pbakaus/impeccable) : à installer
  localement (`npx impeccable install`), non versionné ; puis `/impeccable init` et `/impeccable critique`.
- **Plan** :
  1. `/impeccable critique` et `audit` du site actuel (captures desktop + mobile) : liste des défauts.
  2. Nouveau front dans `web/` : Vite + React + TypeScript + Tailwind + composants shadcn (via le MCP),
     MapLibre et D3 conservés pour la carte et les graphiques ; mêmes fichiers `web/data/*.json`,
     mêmes vues (carte, carte-temps, comparaison FR/DE, toutes les communes), DE par défaut + FR.
  3. Build statique (`vite build`, `base: './'`) ; le workflow Pages ajoute alors `npm ci && npm run build`
     et publie `web/dist` au lieu de `web/`.
  4. Reprendre les tests Playwright (vues, valeurs contre les JSON, pas d'erreur console, pas de requête
     externe), puis `/impeccable polish`.
- **GitHub Pages** : workflow `.github/workflows/pages.yml` (publie `web/` à chaque push sur `main`).
  Le dépôt est privé : Pages exige un compte GitHub payant ou un dépôt public, puis
  Settings > Pages > Source = « GitHub Actions ». URL attendue :
  https://ahmedmaaloul.github.io/so_nah_so_fern/
