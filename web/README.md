# So nah, so fern: Web-Viewer / Visualiseur web

## Deutsch

Statische Seite ohne Build-Schritt und ohne Netzzugriff zur Laufzeit (keine CDN, keine Kachelserver, keine Webfonts).

- Lokal starten: im Ordner `web/` den Befehl `python3 -m http.server 8765` ausführen und `http://localhost:8765/` öffnen (ein Dateizugriff per `file://` funktioniert wegen `fetch` nicht).
- Dateien: `index.html`, `app.js` (Karten, Diagramm, Listen), `i18n.js` (Texte DE/FR), `style.css`.
- Daten (nur lesen): `data/index.json` und `data/<ort>.json` (Meta, Polygone, Zeitkartogramm, Ergebnisse, Isochronen, Ringe, Beispielverbindungen). Alle angezeigten Zahlen stammen aus diesen Dateien.
- URL-Parameter (optional): `?lang=fr&mode=compare&o=garches&p=centre&ind=transfers&view=time` ; Vue alle Gemeinden / toutes les communes : `?mode=all&reg=idf&u=75100&dir=nach`.
- Bibliotheken in `vendor/`: MapLibre GL JS 4.7.1 (BSD-3-Clause, `LICENSE-maplibre-gl.txt`) und D3 7.9.0 (ISC, `LICENSE-d3.txt`).
- Datenquellen und Lizenzen: siehe Fußzeile der Seite (aus `meta.sources`).
- Ansicht „Alle Gemeinden“ (dritter Knopf unter „Ansicht“): Daten `data/phase2_rhein_main.json` und `data/phase2_idf.json` (Schlüssel `index.json` → `phase2`). Ohne Auswahl färbt die Karte jede Gemeinde nach ihrer Erreichbarkeit (bev.-gew. effektives Tempo zu allen anderen, höher = besser verbunden; alternativ Median-Reisezeit oder Verhältnis ÖV/Pkw). Ein Klick (oder die Suche) wählt eine Gemeinde als Ausgangsort: alle anderen werden nach der Median-Reisezeit gefärbt (Skala 0 bis 180 min), der Schalter „von / nach“ wählt Zeile oder Spalte der Matrix (die Matrix ist nicht symmetrisch). Dazu Streudiagramm, Kennzahlenkarte, Listen der bemerkenswerten Paare und ein Vergleich beider Regionen (`meta.summary`, `by_band`). Die Matrix wird nur gelesen (Index = Ausgangsindex × N + Zielindex, -1 = kein Wert); die Region Île-de-France (2,9 MB) wird beim ersten Öffnen der Ansicht geladen.

## Français

Page statique sans étape de build et sans accès réseau à l'exécution (pas de CDN, pas de serveur de tuiles, pas de polices web).

- Lancer en local : dans le dossier `web/`, exécuter `python3 -m http.server 8765` puis ouvrir `http://localhost:8765/` (l'ouverture directe en `file://` ne fonctionne pas à cause de `fetch`).
- Fichiers : `index.html`, `app.js` (cartes, graphique, listes), `i18n.js` (textes DE/FR), `style.css`.
- Données (lecture seule) : `data/index.json` et `data/<lieu>.json` (méta, polygones, cartogramme temporel, résultats, isochrones, cercles, itinéraires d'exemple). Tous les chiffres affichés proviennent de ces fichiers.
- Paramètres d'URL (optionnels) : `?lang=fr&mode=compare&o=garches&p=centre&ind=transfers&view=time` ; Vue alle Gemeinden / toutes les communes : `?mode=all&reg=idf&u=75100&dir=nach`.
- Bibliothèques dans `vendor/` : MapLibre GL JS 4.7.1 (BSD-3-Clause, `LICENSE-maplibre-gl.txt`) et D3 7.9.0 (ISC, `LICENSE-d3.txt`).
- Sources et licences des données : voir le pied de page (issues de `meta.sources`).
- Vue « Toutes les communes » (troisième bouton sous « Vue ») : données `data/phase2_rhein_main.json` et `data/phase2_idf.json` (clé `phase2` de `index.json`). Sans sélection, la carte colore chaque commune selon son accessibilité (vitesse effective pondérée par la population vers toutes les autres, plus haut = mieux reliée ; ou temps médian, ou rapport TC/voiture). Un clic (ou la recherche) choisit une commune comme origine : les autres sont colorées par le temps médian en TC (échelle 0 à 180 min) ; l'interrupteur « depuis / vers » choisit la ligne ou la colonne de la matrice (la matrice n'est pas symétrique). S'y ajoutent un nuage de points, une fiche, les listes de paires remarquables et une comparaison des deux régions (`meta.summary`, `by_band`). La matrice n'est que lue (indice = indice d'origine × N + indice de destination, -1 = pas de valeur) ; la région Île-de-France (2,9 Mo) n'est chargée qu'à l'ouverture de la vue.
