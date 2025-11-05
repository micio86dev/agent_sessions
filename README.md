# Agent Sessions

Mini launcher per registrare attività desktop, sincronizzare su MongoDB e generare mock JSON.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
```

## Build

```bash
pip install pyinstaller
pyinstaller --onefile --windowed launcher.py --add-data ".env:."
```

## Dopo la build:

```bash
dist/launcher       ← qui c’è il binario pronto
build/              ← cartella temporanea della build
launcher.spec       ← file di configurazione PyInstaller
```

## Puoi lanciare direttamente:

```bash
./dist/launcher   # macOS/Linux
dist\launcher.exe # Windows
```
