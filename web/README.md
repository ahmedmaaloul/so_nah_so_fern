# So nah, so fern: Web-Viewer / Visualiseur web

## Deutsch

Statische Seite ohne Build-Schritt und ohne Netzzugriff zur Laufzeit (keine CDN, keine Kachelserver, keine Webfonts).

- Lokal starten: im Ordner `web/` den Befehl `python3 -m http.server 8765` ausführen und `http://localhost:8765/` öffnen (ein Dateizugriff per `file://` funktioniert wegen `fetch` nicht).
- Dateien: `index.html`, `app.js` (Karten, Diagramm, Listen), `i18n.js` (Texte DE/FR), `style.css`.
- Daten (nur lesen): `data/index.json` und `data/<ort>.json` (Meta, Polygone, Zeitkartogramm, Ergebnisse, Isochronen, Ringe, Beispielverbindungen). Alle angezeigten Zahlen stammen aus diesen Dateien.
- URL-Parameter (optional): `?lang=fr&mode=compare&o=garches&p=centre&ind=transfers&view=time`.
- Bibliotheken in `vendor/`: MapLibre GL JS 4.7.1 (BSD-3-Clause, `LICENSE-maplibre-gl.txt`) und D3 7.9.0 (ISC, `LICENSE-d3.txt`).
- Datenquellen und Lizenzen: siehe Fußzeile der Seite (aus `meta.sources`).

## Français

Page statique sans étape de build et sans accès réseau à l'exécution (pas de CDN, pas de serveur de tuiles, pas de polices web).

- Lancer en local : dans le dossier `web/`, exécuter `python3 -m http.server 8765` puis ouvrir `http://localhost:8765/` (l'ouverture directe en `file://` ne fonctionne pas à cause de `fetch`).
- Fichiers : `index.html`, `app.js` (cartes, graphique, listes), `i18n.js` (textes DE/FR), `style.css`.
- Données (lecture seule) : `data/index.json` et `data/<lieu>.json` (méta, polygones, cartogramme temporel, résultats, isochrones, cercles, itinéraires d'exemple). Tous les chiffres affichés proviennent de ces fichiers.
- Paramètres d'URL (optionnels) : `?lang=fr&mode=compare&o=garches&p=centre&ind=transfers&view=time`.
- Bibliothèques dans `vendor/` : MapLibre GL JS 4.7.1 (BSD-3-Clause, `LICENSE-maplibre-gl.txt`) et D3 7.9.0 (ISC, `LICENSE-d3.txt`).
- Sources et licences des données : voir le pied de page (issues de `meta.sources`).
