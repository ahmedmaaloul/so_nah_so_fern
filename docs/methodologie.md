# So nah, so fern · Methodenpapier / Note méthodologique

Stand / état : 07.10.2026. Alle Zahlen stammen aus `outputs/` (Dateien in Klammern) ;
jede Annahme ist mit ihrem Code in `outputs/<ursprung>/assumptions.jsonl` protokolliert.
Tous les chiffres viennent de `outputs/` ; chaque hypothèse figure avec son code dans
`outputs/<origine>/assumptions.jsonl`.

---

## Deutsch

**Frage.** Wie weit liegen Luftlinie und tatsächliche ÖV-Reisezeit auseinander, von
Garches (westlich von Paris) und von den Vergleichsgemeinden Kronberg im Taunus und
Bad Soden am Taunus (westlich von Frankfurt) aus?

**Einheiten und Punkte (U1 bis U5, H1, H2).** Ziele sind alle Gemeinden, deren amtlicher
Punkt höchstens 30 km vom Ursprung entfernt ist (Garches 424, Kronberg 119, Bad Soden 121).
Paris ist in 17 Einheiten (Arrondissements, 1 bis 4 zusammengefasst), Frankfurt in seine
45 Stadtteile geteilt. Punkt einer Gemeinde: IGN-Chef-lieu bzw. BKG „Kern der Gemeinde“.
Einwohner: INSEE 2023, Destatis 31.12.2024, Frankfurter Melderegister für die Stadtteile
(2,8 % über Destatis, U1b). Entfernungen geodätisch (WGS84).

**Netz und Stichtag (G0 bis G3, D1).** OSM (Geofabrik bzw. Spiegel GWDG und OSM France,
G0) und GTFS (IDFM, gtfs.de/DELFI), zugeschnitten auf die konvexe Hülle aller Ziele plus
10 km. Fernverkehr (ICE, IC, EC, FLX …) ist ausgeschlossen: 1 286 Fahrten im Zuschnitt von Kronberg,
keine in Île-de-France (G1). Stichtag ist ein Dienstag in der Schulzeit: 13.10.2026 (FR)
und 20.10.2026 (DE), beide mit mindestens 97 % des üblichen Werktagsangebots (Verhältnis 1,001 bzw. 1,000).
Zusätzlich je Linie (D2): übliche Linien ohne jede Fahrt am Stichtag tragen 0,01 % (FR)
bzw. 0,16 % (DE) der üblichen Fahrten, Schwelle 1 %.

**Reisezeit (R1 bis R4).** r5py 1.1.7 / R5 7.5.1: Abfahrt jede Minute von 07:30 bis 09:00
(90 Abfahrten), Median (P50) als Hauptwert, P25 und P75 als Streuung. Tür zu Tür:
Zugangsweg, Wartezeit, Fahrt, Umstiege, Abgangsweg; Fußwege 4,5 km/h, höchstens 30 Min.
Zu- oder Abgang, höchstens 180 Min. gesamt. Punkte werden ins Fußwegenetz eingerastet; ein
Punkt auf einer abgetrennten „Fußweg-Insel“ wird zum nächsten verbundenen Punkt verschoben
(nur Andrésy, 15 m, R4).

**Umstiege (T1, T2).** Kleinste Zahl k an Fahrzeugen, deren Median höchstens 1 Min. über dem
unbeschränkten Median liegt, minus 1. Mit 3 oder 5 Min. Toleranz sinkt der Anteil der Ziele
mit mindestens 2 Umstiegen von Garches von 87 % auf 81 % bzw. 77 %, von Kronberg von 45 % auf
35 % bzw. 32 % (`qa_transfers_tolerance.json`): der Abstand bleibt. Beispielrouten auf der
Karte: eine Abfahrt um 08:15; ihre Dauer kann vom Median abweichen (Bad Soden: S3 alle 30 Min.
ab xx:11 und xx:41, um 08:15 also 26 statt im Median 14,5 Min. Warten).

**Pkw (C1, C2).** OSRM 5.27.1, Profil „car“, **ohne Verkehr** und ohne Parkplatzsuche: die
Pkw-Zeit ist eine Untergrenze, das Verhältnis ÖV/Pkw also eine Obergrenze. Variante Morgenspitze
(C3): Freiflusszeit je Straßenklasse mal (1 + Stauniveau der Metropolregion um 8 Uhr, TomTom
Traffic Index 2025): Paris Autobahnen +84,2 %, übrige +57,9 %; Frankfurt +36,9 % / +64,4 %.
ÖV/Pkw ab Bahnhof dann 1,54 / 1,84 / 2,03 statt 2,50 / 2,90 / 3,11.

**Kennzahlen (I1, I2).** Effektive Geschwindigkeit = Luftlinie / ÖV-Zeit; Paradoxie-Index =
Rang nach Zeit minus Rang nach Entfernung; Abweichung von der Regression log(Zeit) ~ log(Entfernung);
tote Zone = näher als 8 km und länger als 60 Min. oder unerreichbar.

**Einordnung.** Paris ist Hauptstadt und Mittelpunkt eines strahlenförmigen Netzes, Frankfurt eine
Regionalmetropole: Im Umkreis von 30 km leben 10,13 Mio. (Garches) gegenüber 2,85 Mio. Menschen
(Kronberg). Median-Zeit und effektive Geschwindigkeit zählen jede Gemeinde gleich und messen Zeit je
Kilometer; sie sind daher ähnlich (75 / 72,5 Min., 16,1 / 16,0 km/h). Die erreichbare Bevölkerung
unterscheidet sich stark: in 60 Min. 5,26 Mio. (52 %) gegenüber 0,81 Mio. (28 %). Ins Zentrum
der Kernstadt: 47 Min. für 13,2 km (Paris Centre) gegenüber 46 Min. für 14,2 km (Altstadt); über
alle Bezirke 47 gegenüber 55 Min.; Ziele außerhalb der Kernstadt 77 gegenüber 85 Min.

**Ergebnisse ab Bahnhof** (`synthese_*.json`): Median 75 / 72,5 / 72 Min. (Garches / Kronberg /
Bad Soden); bevölkerungsgewichtete effektive Geschwindigkeit 16,1 / 16,0 / 14,4 km/h;
Rangkorrelation Entfernung/Zeit 0,75 / 0,81 / 0,90; ÖV/Pkw 2,5 / 2,9 / 3,1; in 60 Min.
erreichte Fläche 431 / 170 / 200 km². Von Garches aus sind 34 Ziele in 180 Min. nicht erreichbar.

**Visualisierung (V1, V2).** Isochronen aus einem 200-m-Raster; Zeitkartogramm mit gleichem
Maßstab für alle Ursprünge (Radius = ÖV-Zeit × 15 km/h, Winkel bleibt). Falten im Kartogramm
sind gewollt: sie zeigen ferne, aber schnell erreichbare Orte.

**Sensitivität (S1).** Rathaus aus OSM statt amtlichem Punkt: Rangkorrelation der Zeiten
0,9998 (Garches), 0,990 (Kronberg), 0,994 (Bad Soden); höchstens 20 Min. Abweichung; 8 bis 10
der 10 Tops bleiben gleich (`comparaison_reference.json`).

**Zeitfenster (S2, `outputs/time_windows/`).** Gleicher Tag, Fenster 10:00 bis 11:30,
17:30 bis 19:00 und 20:30 bis 22:00 statt 07:30 bis 09:00. Kronberg und Bad Soden ändern
sich bis 19:00 kaum (Median gleich, Rangkorrelation der Zeiten ≥ 0,990). Garches reagiert
stärker: tagsüber Median +4 Min., abends 85 statt 34 Ziele in 180 Min. nicht erreichbar
(Rangkorrelation 0,890). Die Abendmediane gelten nur für erreichte Ziele und sind daher zu
günstig.

**Andere Tage (S3, `outputs/days/`).** Gleiches Fenster 07:30 bis 09:00. Donnerstag derselben
Woche: identische Zeiten (Rangkorrelation 1,000). Samstag: Median +6,5 Min. (Garches), +2,5
(Kronberg), −1 (Bad Soden); Sonntag +12 / +4,5 / +7 Min. mit bis zu 68 (Garches) unerreichten
Zielen. Ein Dienstag in den Ferien oder drei Wochen später ändert in Rhein-Main wenig. In
Île-de-France fehlen im „latest“-Feed ab dem 20.10. Linien, die am Stichtag fahren (149 Linien
am Schultag 03.11., darunter Tram T4): diese Varianten messen die Lücke des Feeds, nicht das
Angebot.

**Phase 2 (P1).** Alle Einheiten ≤ 30 km von Paris (417) bzw. Frankfurt (133) untereinander:
Median 95 / 82 Min., ÖV/Pkw 2,54 / 2,98 (Morgenspitze 1,50 / 1,96), Rangkorrelation 0,78 / 0,84
(`outputs/phase2/`).

**Grenzen.** Nur Fußweg als Zubringer; keine Tarif- oder
Komfortgrößen; Pkw ohne Stau; GTFS-Qualität der Betreiber als gegeben.

---

## Français

**Question.** Quel écart entre distance à vol d'oiseau et temps réel en transports en commun,
depuis Garches (ouest de Paris) et depuis ses villes jumelles Kronberg im Taunus et Bad Soden
am Taunus (ouest de Francfort) ?

**Unités et points (U1 à U5, H1, H2).** Destinations : toutes les communes dont le point officiel
est à 30 km au plus de l'origine (Garches 424, Kronberg 119, Bad Soden 121). Paris est découpé
en 17 unités (arrondissements, 1 à 4 réunis), Francfort en 45 Stadtteile. Point : chef-lieu IGN
ou « Kern der Gemeinde » BKG. Population : INSEE 2023, Destatis au 31/12/2024, registre de
Francfort pour les Stadtteile (2,8 % au dessus de Destatis, U1b). Distances géodésiques (WGS84).

**Réseau et jour (G0 à G3, D1).** OSM (Geofabrik ou miroirs GWDG et OSM France, G0) et GTFS
(IDFM, gtfs.de/DELFI), découpés sur l'enveloppe convexe des destinations plus 10 km. Grandes
lignes exclues (ICE, IC, EC, FLX …) : 1 286 trajets dans la découpe de Kronberg, aucun en Île-de-France (G1).
Jour : un mardi scolaire, 13/10/2026 (FR) et 20/10/2026 (DE), avec au moins 97 % de l'offre
habituelle (ratios 1,001 et 1,000). En plus, ligne par ligne (D2) : les lignes habituelles sans
aucun trajet le jour d'analyse portent 0,01 % (FR) et 0,16 % (DE) des trajets habituels, seuil 1 %.

**Temps de trajet (R1 à R4).** r5py 1.1.7 / R5 7.5.1 : un départ par minute de 07:30 à 09:00
(90 départs), médiane (p50) comme valeur principale, p25 et p75 pour la dispersion. Porte à
porte : marche d'accès, attente, trajet, correspondances, marche finale ; marche à 4,5 km/h,
30 min au plus en accès ou sortie, 180 min au plus au total. Points accrochés au réseau
piéton ; un point accroché à un îlot piéton isolé est déplacé vers le point relié le plus
proche (seulement Andrésy, 15 m, R4).

**Correspondances (T1, T2).** Plus petit nombre k de véhicules dont la médiane dépasse d'au plus
1 min la médiane sans limite, moins 1. Avec 3 ou 5 min de tolérance, la part des destinations à
2 correspondances ou plus passe de 87 % à 81 % puis 77 % pour Garches, de 45 % à 35 % puis 32 %
pour Kronberg (`qa_transfers_tolerance.json`) : l'écart demeure. Itinéraires de la carte : un
départ à 08:15 ; leur durée peut s'écarter de la médiane (Bad Soden : S3 toutes les 30 min à
xx:11 et xx:41, donc 26 min d'attente à 08:15 contre 14,5 min en médiane).

**Voiture (C1, C2).** OSRM 5.27.1, profil « car », **sans trafic** ni recherche de stationnement :
le temps voiture est un minorant, donc le rapport TC/voiture un majorant. Variante pointe du matin
(C3) : temps fluide par classe de route × (1 + congestion de la zone métropolitaine à 8 h,
TomTom Traffic Index 2025) : Paris autoroutes +84,2 %, autres +57,9 % ; Francfort +36,9 % /
+64,4 %. TC/voiture depuis la gare : 1,54 / 1,84 / 2,03 au lieu de 2,50 / 2,90 / 3,11.

**Indicateurs (I1, I2).** Vitesse effective = distance / temps TC ; indice de paradoxe = rang en
temps moins rang en distance ; écart à la régression log(temps) ~ log(distance) ; zone morte =
à moins de 8 km et à plus de 60 min ou inatteignable.

**Mise en contexte.** Paris est la capitale, au centre d'un réseau radial ; Francfort est une
métropole régionale : dans un rayon de 30 km vivent 10,13 M (Garches) contre 2,85 M d'habitants
(Kronberg). Le temps médian et la vitesse effective comptent chaque commune à égalité et mesurent un
temps par kilomètre ; ils sont donc proches (75 / 72,5 min, 16,1 / 16,0 km/h). La population atteinte
diffère fortement : en 60 min, 5,26 M (52 %) contre 0,81 M (28 %). Vers le centre de la ville
centre : 47 min pour 13,2 km (Paris Centre) contre 46 min pour 14,2 km (Altstadt) ; sur tous les
quartiers 47 contre 55 min ; destinations hors ville centre 77 contre 85 min.

**Résultats depuis la gare** (`synthese_*.json`) : médiane 75 / 72,5 / 72 min (Garches /
Kronberg / Bad Soden) ; vitesse effective médiane pondérée par la population 16,1 / 16,0 /
14,4 km/h ; corrélation de rang distance/temps 0,75 / 0,81 / 0,90 ; rapport TC/voiture 2,5 /
2,9 / 3,1 ; surface atteinte en 60 min 431 / 170 / 200 km². Depuis Garches, 34 destinations ne
sont pas atteintes en 180 min.

**Visualisation (V1, V2).** Isochrones sur une grille de 200 m ; cartogramme temporel à la même
échelle pour toutes les origines (rayon = temps TC × 15 km/h, angle conservé). Les replis du
cartogramme sont voulus : ils montrent les lieux lointains mais vite atteints.

**Sensibilité (S1).** Mairie OSM au lieu du point officiel : corrélation de rang des temps
0,9998 (Garches), 0,990 (Kronberg), 0,994 (Bad Soden) ; écart maximal 20 min ; 8 à 10 des
10 tops inchangés (`comparaison_reference.json`).

**Plages horaires (S2, `outputs/time_windows/`).** Même jour, fenêtres 10:00 à 11:30,
17:30 à 19:00 et 20:30 à 22:00 au lieu de 07:30 à 09:00. Kronberg et Bad Soden changent peu
jusqu'à 19:00 (médiane identique, corrélation de rang des temps ≥ 0,990). Garches réagit
davantage : médiane +4 min en heure creuse, et le soir 85 destinations non atteintes en
180 min au lieu de 34 (corrélation 0,890). Les médianes du soir ne portent que sur les
destinations atteintes et sont donc flatteuses.

**Autres jours (S3, `outputs/days/`).** Même fenêtre 07:30 à 09:00. Jeudi de la même semaine :
temps identiques (corrélation de rang 1,000). Samedi : médiane +6,5 min (Garches), +2,5
(Kronberg), −1 (Bad Soden) ; dimanche +12 / +4,5 / +7 min, avec jusqu'à 68 destinations non
atteintes (Garches). Un mardi de vacances ou trois semaines plus tard change peu en Rhin-Main.
En Île-de-France, le flux « latest » ne contient plus, à partir du 20/10, des lignes qui
circulent le jour d'analyse (149 lignes le 03/11, jour d'école, dont le tram T4) : ces
variantes mesurent la lacune du flux, pas l'offre.

**Phase 2 (P1).** Toutes les unités à 30 km au plus de Paris (417) ou de Francfort (133), entre
elles : médiane 95 / 82 min, TC/voiture 2,54 / 2,98 (pointe du matin 1,50 / 1,96), corrélation de rang
0,78 / 0,84 (`outputs/phase2/`).

**Limites.** Rabattement à pied seulement ; ni tarif ni
confort ; voiture sans congestion ; qualité des GTFS des opérateurs prise telle quelle.
