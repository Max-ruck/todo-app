# Todo-App

Eine schlanke, selbst gehostete Aufgabenverwaltung für Teams. Todos können innerhalb von Gruppen geteilt, delegiert und über einen gemeinsamen Pool verteilt werden.

## Voraussetzungen

- Ubuntu 24.04 LTS (Root-Zugriff oder sudo)
- Eine Domain mit A-Record auf deinen Server

## Installation

```bash
# 1. Repo klonen
git clone https://github.com/DEIN-USERNAME/todo-app.git
cd todo-app

# 2. Installer ausführen
bash install.sh mydomain.com "Hoster, Rechenzentrum"

# 3. Im Browser öffnen – erster Nutzer wird Admin
https://mydomain.com/todo/
```

Der Installer übernimmt automatisch: Abhängigkeiten, Python-venv, systemd-Service, nginx, Let's-Encrypt-SSL und Certbot-Auto-Renewal.

## Features

- **Eigene Todos** – Erstellen, priorisieren, archivieren
- **Gruppen** – Gemeinsame Arbeitsbereiche mit Mitgliederverwaltung
- **Pool** – Offene Aufgaben, die von Gruppenmitgliedern übernommen werden können
- **Senden** – Todos an andere Nutzer delegieren, mit Rückgabe-Möglichkeit
- **Inbox** – Eingehende Aufgaben im Blick behalten
- **Archiv** – Erledigte Todos mit Aktivitäts-Log
- **Shared View** – Lesenden oder schreibenden Zugriff auf eigene Todos freigeben
- **Admin-Panel** – Nutzer- und Gruppenverwaltung, Einladungslinks generieren
- **Datenschutz** – Selbst gehostet, kein Tracking, DSGVO-Datenschutzerklärung enthalten

## Screenshots

_Folgen nach dem ersten Release._

## Lizenz

MIT License – siehe [LICENSE](LICENSE).
