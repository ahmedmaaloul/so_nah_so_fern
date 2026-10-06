# Choix de la commune jumelle — indicateurs comparés

*Généré par `pipeline/03_select_twin_city.py` le 06/10/2026 21:38. Aucun choix n'est fait ici : ce sont des chiffres comparables, identiques pour toutes les communes.*

## Paramètres

- Rayon de chalandise : **2,0 km** autour du point de la commune (chef-lieu IGN pour Garches ; « Kern der Gemeinde » VG250 pour les candidates), distance géodésique WGS84.
- Fenêtre de départs : **07:30–09:00** (bornes incluses) ; une heure GTFS ≥ 24:00 (ex. 31:00) est hors fenêtre.
- Jour d'analyse FR : **13/10/2026** (mardi) ; GTFS valide 20261003 → 20261104 ; 145 234 trajets ce jour (médiane des 5 mêmes jours de semaine du feed : 140 876, rapport 1,031).
- Jour d'analyse DE : **20/10/2026** (mardi) ; GTFS valide 20261003 → 20261102 ; 791 963 trajets ce jour (médiane des 4 mêmes jours de semaine du feed : 811 478, rapport 0,976).
- Ferré lourd ou guidé : route_type tram = 0, 900–999; metro = 1, 400–499; rail = 2, 100–199; monorail = 12 ; grand-ligne exclu : noms commençant par ICE, IC, EC, ECE, EN, NJ, RJ, RJX, FLX, TGV ou route_type 101, 102.
- Ligne = (agence, mode, `route_short_name`) : plusieurs `route_id` de même nom = une seule ligne.
- Départ = ligne de `stop_times` d'un trajet actif ce jour-là, hors dernier arrêt du trajet, dans la fenêtre. Gares et lignes (colonnes « jour ») : desservies à n'importe quelle heure du jour ; entre parenthèses, la même chose restreinte à la fenêtre.
- Score de similarité = moyenne des |log(candidate / Garches)| sur : `population`, `dist_core_km`, `rail_lines_catchment`, `rail_departures_catchment` (plus petit = plus proche).

## Tableau

| Rang | Commune | Pays | Habitants | Surface km² | Densité /km² | Dist. centre km | Gares ferrées jour (fenêtre) | Lignes ferrées jour (fenêtre) | Départs ferrés 07:30–09:00 | Départs bus 07:30–09:00 | Lignes | Score |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| réf. | Garches | FR | 17 743 | 2,72 | 6 517 | 12,2 | 1 (1) | 1 (1) | 17 | 919 | L | — |
| 1 | Kronberg im Taunus | DE | 18 671 | 18,58 | 1 005 | 14,5 | 2 (2) | 1 (1) | 9 | 307 | S4 | 0,215 |
| 6 | Oberursel (Taunus) | DE | 46 736 | 45,34 | 1 031 | 12,3 | 7 (7) | 3 (3) | 89 | 438 | RB15, S5, U3 | 0,933 |
| 2 | Bad Soden am Taunus | DE | 23 103 | 12,50 | 1 848 | 13,4 | 2 (2) | 1 (1) | 9 | 338 | S3 | 0,249 |
| 4 | Bad Homburg v.d.Höhe | DE | 56 688 | 51,14 | 1 108 | 14,0 | 1 (1) | 2 (2) | 14 | 1030 | RB15, S5 | 0,547 |
| 5 | Bad Vilbel | DE | 35 961 | 25,68 | 1 400 | 8,8 | 2 (2) | 7 (4) | 32 | 360 | RB34, RB37, RB40, RB41, RE30, RE99, S6 | 0,901 |
| 3 | Eschborn | DE | 22 403 | 12,13 | 1 847 | 8,8 | 2 (2) | 2 (2) | 24 | 436 | S3, S4 | 0,398 |

Détail des écarts par métrique (|log|) : voir `twin_candidates.csv` (colonnes `sim_*`).

## Ce que disent les chiffres

- Garches (référence) : 17 743 hab., densité 6 517 hab./km², à 12,2 km de Paris ; dans 2,0 km : 1 gare ferrée et 1 ligne ferrée (L), 17 départs ferrés et 919 départs de bus entre 07:30 et 09:00.
- Kronberg im Taunus : 18 671 hab. (×1,05 Garches), densité 1 005 hab./km² (×0,15), à 14,5 km de Frankfurt am Main (Garches : 12,2 km de Paris) ; dans 2,0 km : 2 gares ferrées (Garches : 1) et 1 ligne ferrée (S4) (Garches : 1), 9 départs ferrés (Garches : 17) et 307 départs de bus (Garches : 919) entre 07:30 et 09:00 ; score de similarité 0,21, rang 1.
- Oberursel (Taunus) : 46 736 hab. (×2,63 Garches), densité 1 031 hab./km² (×0,16), à 12,3 km de Frankfurt am Main (Garches : 12,2 km de Paris) ; dans 2,0 km : 7 gares ferrées (Garches : 1) et 3 lignes ferrées (RB15, S5, U3) (Garches : 1), 89 départs ferrés (Garches : 17) et 438 départs de bus (Garches : 919) entre 07:30 et 09:00 ; score de similarité 0,93, rang 6.
- Bad Soden am Taunus : 23 103 hab. (×1,30 Garches), densité 1 848 hab./km² (×0,28), à 13,4 km de Frankfurt am Main (Garches : 12,2 km de Paris) ; dans 2,0 km : 2 gares ferrées (Garches : 1) et 1 ligne ferrée (S3) (Garches : 1), 9 départs ferrés (Garches : 17) et 338 départs de bus (Garches : 919) entre 07:30 et 09:00 ; score de similarité 0,25, rang 2.
- Bad Homburg v.d.Höhe : 56 688 hab. (×3,19 Garches), densité 1 108 hab./km² (×0,17), à 14,0 km de Frankfurt am Main (Garches : 12,2 km de Paris) ; dans 2,0 km : 1 gare ferrée (Garches : 1) et 2 lignes ferrées (RB15, S5) (Garches : 1), 14 départs ferrés (Garches : 17) et 1030 départs de bus (Garches : 919) entre 07:30 et 09:00 ; score de similarité 0,55, rang 4.
- Bad Vilbel : 35 961 hab. (×2,03 Garches), densité 1 400 hab./km² (×0,21), à 8,8 km de Frankfurt am Main (Garches : 12,2 km de Paris) ; dans 2,0 km : 2 gares ferrées (Garches : 1) et 7 lignes ferrées (RB34, RB37, RB40, RB41, RE30, RE99, S6) (Garches : 1), 32 départs ferrés (Garches : 17) et 360 départs de bus (Garches : 919) entre 07:30 et 09:00 ; score de similarité 0,90, rang 5.
- Eschborn : 22 403 hab. (×1,26 Garches), densité 1 847 hab./km² (×0,28), à 8,8 km de Frankfurt am Main (Garches : 12,2 km de Paris) ; dans 2,0 km : 2 gares ferrées (Garches : 1) et 2 lignes ferrées (S3, S4) (Garches : 1), 24 départs ferrés (Garches : 17) et 436 départs de bus (Garches : 919) entre 07:30 et 09:00 ; score de similarité 0,40, rang 3.

## Gares ferrées trouvées (dans le rayon et en bordure)

Distance = distance minimale d'un quai desservi au point de la commune ; au-delà de 2,0 km la gare n'est pas comptée.

| Commune | Gare (zone d'arrêt) | Dist. km | Lignes | Dép. fenêtre | Dép. jour | Dans le rayon |
|---|---|---:|---|---:|---:|---|
| Garches | Garches - Marnes-la-Coquette | 0,59 | L | 17 | 152 | oui |
| Garches | Sèvres - Ville-d'Avray | 2,05 | L, U | 28 | 243 | non |
| Garches | Saint-Cloud | 2,21 | L, U | 44 | 395 | non |
| Garches | Parc de Saint-Cloud | 2,50 | T2 | 26 | 213 | non |
| Kronberg im Taunus | Kronberg (Taunus) Bahnhof | 0,44 | S4 | 3 | 42 | oui |
| Kronberg im Taunus | Kronberg (Taunus) Süd | 1,50 | S4 | 6 | 84 | oui |
| Oberursel (Taunus) | Oberursel Stadtmitte \| VOberursel | 0,32 | U3 | 6 | 78 | oui |
| Oberursel (Taunus) | Oberursel Stadtmitte \| NOberursel | 0,32 | U3 | 6 | 78 | oui |
| Oberursel (Taunus) | Oberursel (Taunus) Bahnhof | 0,49 | RB15, S5, U3 | 27 | 307 | oui |
| Oberursel (Taunus) | Oberursel (Taunus) Altstadt | 0,65 | U3 | 12 | 156 | oui |
| Oberursel (Taunus) | Oberursel (Taunus)-Bommersheim | 1,19 | U3 | 12 | 149 | oui |
| Oberursel (Taunus) | Oberursel (Taunus) Lahnstraße | 1,31 | U3 | 12 | 156 | oui |
| Oberursel (Taunus) | Oberursel (Taunus)-Stierstadt Bahnhof | 1,67 | RB15, S5 | 14 | 152 | oui |
| Oberursel (Taunus) | Oberursel (Taunus) Glöcknerwiese | 2,07 | U3 | 12 | 156 | non |
| Oberursel (Taunus) | Oberursel (Taunus)-Weißkirchen Ost | 2,13 | U3 | 12 | 149 | non |
| Oberursel (Taunus) | Oberursel (Taunus) Kupferhammer | 2,40 | U3 | 12 | 156 | non |
| Bad Soden am Taunus | Bad Soden (Taunus) Bahnhof | 0,21 | S3 | 3 | 43 | oui |
| Bad Soden am Taunus | Sulzbach (Taunus) Nordbahnhof | 1,26 | S3 | 6 | 86 | oui |
| Bad Soden am Taunus | Schwalbach (Taunus) Limes Bahnhof | 2,21 | S3 | 7 | 86 | non |
| Bad Homburg v.d.Höhe | Bad Homburg v.d.H. Bahnhof | 1,09 | RB15, S5 | 14 | 157 | oui |
| Bad Vilbel | Bad Vilbel Südbahnhof | 0,62 | S6 | 12 | 164 | oui |
| Bad Vilbel | Bad Vilbel Bahnhof | 0,86 | RB34, RB37, RB40, RB41, RE30, RE99, S6 | 20 | 243 | oui |
| Eschborn | Eschborn Bahnhof | 0,63 | S3, S4 | 12 | 170 | oui |
| Eschborn | Eschborn Südbahnhof | 1,27 | S3, S4 | 12 | 170 | oui |
| Eschborn | Eschborn-Niederhöchstadt Bahnhof | 2,01 | S3, S4 | 12 | 170 | non |

## Grand-ligne exclu (passages dans le rayon, jour entier)

- **Garches** : aucun passage grand-ligne dans le rayon.
- **Kronberg im Taunus** : aucun passage grand-ligne dans le rayon.
- **Oberursel (Taunus)** : aucun passage grand-ligne dans le rayon.
- **Bad Soden am Taunus** : aucun passage grand-ligne dans le rayon.
- **Bad Homburg v.d.Höhe** : aucun passage grand-ligne dans le rayon.
- **Bad Vilbel** : aucun passage grand-ligne dans le rayon.
- **Eschborn** : aucun passage grand-ligne dans le rayon.

## Sensibilité au rayon

Mêmes indicateurs avec un rayon de ± 25 % ; score et rang sont recalculés à chaque rayon (Garches au même rayon ; population et distance au centre inchangées).

| Commune | Rayon km | Gares | Lignes | Dép. ferrés | Dép. bus | Score | Rang |
|---|---:|---:|---:|---:|---:|---:|---:|
| Garches | 1,50 | 1 | 1 | 17 | 451 | — | réf. |
| Kronberg im Taunus | 1,50 | 1 | 1 | 3 | 239 | 0,490 | 3 |
| Oberursel (Taunus) | 1,50 | 6 | 3 | 75 | 321 | 0,890 | 5 |
| Bad Soden am Taunus | 1,50 | 2 | 1 | 9 | 247 | 0,249 | 1 |
| Bad Homburg v.d.Höhe | 1,50 | 1 | 2 | 14 | 840 | 0,547 | 4 |
| Bad Vilbel | 1,50 | 2 | 7 | 32 | 289 | 0,901 | 6 |
| Eschborn | 1,50 | 2 | 2 | 24 | 375 | 0,398 | 2 |
| Garches | 2,00 | 1 | 1 | 17 | 919 | — | réf. |
| Kronberg im Taunus | 2,00 | 2 | 1 | 9 | 307 | 0,215 | 1 |
| Oberursel (Taunus) | 2,00 | 7 | 3 | 89 | 438 | 0,933 | 6 |
| Bad Soden am Taunus | 2,00 | 2 | 1 | 9 | 338 | 0,249 | 2 |
| Bad Homburg v.d.Höhe | 2,00 | 1 | 2 | 14 | 1030 | 0,547 | 4 |
| Bad Vilbel | 2,00 | 2 | 7 | 32 | 360 | 0,901 | 5 |
| Eschborn | 2,00 | 2 | 2 | 24 | 436 | 0,398 | 3 |
| Garches | 2,50 | 4 | 3 | 115 | 1557 | — | réf. |
| Kronberg im Taunus | 2,50 | 2 | 1 | 9 | 405 | 0,967 | 6 |
| Oberursel (Taunus) | 2,50 | 10 | 3 | 125 | 564 | 0,265 | 1 |
| Bad Soden am Taunus | 2,50 | 3 | 1 | 16 | 514 | 0,858 | 4 |
| Bad Homburg v.d.Höhe | 2,50 | 1 | 2 | 14 | 1171 | 0,953 | 5 |
| Bad Vilbel | 2,50 | 2 | 7 | 32 | 417 | 0,788 | 3 |
| Eschborn | 2,50 | 3 | 2 | 36 | 735 | 0,530 | 2 |

## Anomalies et points d'attention

- Garches : gare(s) ferrée(s) en bordure du rayon (2,0 km ± 25 %) : Sèvres - Ville-d'Avray 2,05 km (dehors); Saint-Cloud 2,21 km (dehors); Parc de Saint-Cloud 2,50 km (dehors).
- Kronberg im Taunus : gare(s) ferrée(s) en bordure du rayon (2,0 km ± 25 %) : Kronberg (Taunus) Süd 1,50 km (dedans).
- Oberursel (Taunus) : « Oberursel Stadtmitte » correspond à 2 zones d'arrêt distinctes (92969, 48460 ; nom de zone identique ; distances au point : 0,321 et 0,323 km) — possible double comptage : 6 gare(s) si fusionnées au lieu de 7 (non fusionnées dans le CSV, définition = zones d'arrêt distinctes).
- Oberursel (Taunus) : gare(s) ferrée(s) en bordure du rayon (2,0 km ± 25 %) : Oberursel (Taunus)-Stierstadt Bahnhof 1,67 km (dedans); Oberursel (Taunus) Glöcknerwiese 2,07 km (dehors); Oberursel (Taunus)-Weißkirchen Ost 2,13 km (dehors); Oberursel (Taunus) Kupferhammer 2,40 km (dehors).
- Bad Soden am Taunus : gare(s) ferrée(s) en bordure du rayon (2,0 km ± 25 %) : Schwalbach (Taunus) Limes Bahnhof 2,21 km (dehors).
- Bad Soden am Taunus : 6 départs de la fenêtre ont pickup_type = 1 (montée interdite) ; ils sont comptés comme départs (définition demandée).
- Bad Vilbel : 7 lignes dans la journée mais 4 dans la fenêtre 07:30–09:00 (sans départ dans la fenêtre : RB40, RB41, RE30). La métrique `rail_lines_catchment` suit la définition « ce jour-là » (jour entier).
- Eschborn : gare(s) ferrée(s) en bordure du rayon (2,0 km ± 25 %) : Eschborn-Niederhöchstadt Bahnhof 2,01 km (dehors).
- Le classement par score de similarité dépend du rayon de chalandise : 1,50 km → Bad Soden am Taunus (0,25) > Eschborn (0,40) > Kronberg im Taunus (0,49) > Bad Homburg v.d.Höhe (0,55) > Oberursel (Taunus) (0,89) > Bad Vilbel (0,90) ; 2,00 km → Kronberg im Taunus (0,21) > Bad Soden am Taunus (0,25) > Eschborn (0,40) > Bad Homburg v.d.Höhe (0,55) > Bad Vilbel (0,90) > Oberursel (Taunus) (0,93) ; 2,50 km → Oberursel (Taunus) (0,27) > Eschborn (0,53) > Bad Vilbel (0,79) > Bad Soden am Taunus (0,86) > Bad Homburg v.d.Höhe (0,95) > Kronberg im Taunus (0,97).
