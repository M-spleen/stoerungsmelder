"""U-Bahn-Störungsmonitor: holt BVG-Störungsmeldungen und schickt neue per ntfy.

Nutzung:
    python stoerungsmonitor.py --test    # schickt nur eine Test-Nachricht
    python stoerungsmonitor.py --once    # ein Durchlauf, dann Ende (für GitHub Actions)
    python stoerungsmonitor.py           # läuft dauerhaft (Beenden mit Strg+C)

Das ntfy-Topic kommt aus der Umgebungsvariable NTFY_TOPIC (bei GitHub: Secret).
"""
import os
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

# ---- Einstellungen ---------------------------------------------------------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = "https://ntfy.sh"
NTFY_ICON = "https://thumb.wikimedia.org/wikipedia/commons/thumb/d/d4/Bvg-logo.svg/250px-Bvg-logo.svg.png"  # "" = kein Icon
POLL_SECONDS = 300          # Pause zwischen zwei Abrufen
BLOCK_PAUSE_SECONDS = 1800  # Pause nach einer Sperre (HTTP 403)
STATE_FILE = Path(__file__).with_name("gesehen.json")
API_URL = "https://www.bvg.de/disruption-reports-service/disruptions/v1/de?type=all&timeFrame=TODAY&page="
LINE_FILTER = re.compile(r"^U\d+$")  # nur U-Bahn-Linien
# ----------------------------------------------------------------------------


def clean_text(raw):
    """Wandelt den HTML-Text einer Meldung in normalen Text um."""
    raw = re.sub(r"<bds-signet-line.*?line-id='([^']*)'.*?</bds-signet-line>", r" \1 ", raw, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)    # kein Leerzeichen vor Satzzeichen
    text = re.sub(r"(?<=\S)(zu erreichen)", r" \1", text)  # "Seestraßezu erreichen" -> "Seestraße zu erreichen"
    return text


def fetch_disruptions():
    """Liefert eine Liste von U-Bahn-Meldungen:
    {"id": str, "lines": ["U2", ...], "title": str, "text": str}
    """
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    result = []
    page, pages = 1, 1
    while page <= pages:
        req = urllib.request.Request(API_URL + str(page), headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pages = data.get("numPages", 1)
        for el in data.get("elements", []):
            if el.get("messageType") != "TRAFFIC":  # keine Aufzugsstörungen o. Ä.
                continue
            lines = [ln["name"] for group in el.get("lines", []) for ln in group.get("subway", [])]
            if not any(LINE_FILTER.match(line) for line in lines):
                continue
            block = (el.get("content") or [{}])[0]
            title = block.get("headline", "Störung")
            if any(r["lines"] == lines and r["title"] == title for r in result):
                continue  # gleiche Meldung doppelt
            result.append({
                "id": el["id"],
                "lines": lines,
                "title": title,
                "text": clean_text(block.get("content", "")),
            })
        page += 1
        if page <= pages:
            time.sleep(1)  # zwischen den Seiten kurz warten
    return result


def send_ntfy(title, message, priority=4):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC fehlt (Umgebungsvariable bzw. GitHub-Secret)")
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": ["warning"],
    }
    if NTFY_ICON:
        payload["icon"] = NTFY_ICON
    req = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def load_seen():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:  # Datei leer oder kaputt: von vorn anfangen
            return {}
    return {}


def save_seen(seen):
    tmp = STATE_FILE.with_suffix(".tmp")  # erst in Hilfsdatei, dann ersetzen: nie halb geschrieben
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def check_once(first_run):
    """Ein Durchlauf. Gibt (Anzahl Meldungen, Anzahl gesendet) zurück."""
    seen = load_seen()
    items = fetch_disruptions()
    sent = 0
    for item in items:
        # Fingerabdruck aus Titel und Text: neue ODER geänderte Meldungen lösen aus
        fingerprint = item["title"] + "|" + item["text"]
        if seen.get(item["id"]) == fingerprint:
            continue
        lines = ", ".join(item["lines"])
        if not first_run:  # beim allerersten Lauf nur merken, nichts senden
            send_ntfy(f"{lines}: {item['title']}", item["text"])
            sent += 1
            print(f"[{time.strftime('%H:%M')}] Gesendet: {lines} - {item['title']}")
        seen[item["id"]] = fingerprint
        save_seen(seen)  # sofort merken, damit nichts doppelt kommt
    save_seen(seen)
    return len(items), sent


def main():
    if "--test" in sys.argv:
        send_ntfy("Test", "Dies ist eine Testbenachrichtigung.", priority=5)
        print("Testbenachrichtigung gesendet.")
        return

    first_run = not STATE_FILE.exists()

    if "--once" in sys.argv:  # ein einziger Durchlauf (GitHub Actions ruft das alle paar Minuten auf)
        try:
            count, sent = check_once(first_run)
            if first_run:
                print(f"Start: {count} Meldungen gemerkt, nichts gesendet.")
            else:
                print(f"Geprüft: {count} Meldungen, {sent} gesendet.")
        except Exception as exc:
            print(f"Fehler: {exc}")  # bewusst kein Absturz, sonst kommen Fehler-Mails alle 5 Minuten
        return

    print(f"Störungsmonitor läuft. Abruf alle {POLL_SECONDS} Sekunden. Beenden mit Strg+C.")
    try:
        while True:
            try:
                count, sent = check_once(first_run)
                if first_run:
                    print(f"[{time.strftime('%H:%M')}] Start: {count} Meldungen gemerkt, nichts gesendet.")
                    first_run = False
                elif sent == 0:
                    print(f"[{time.strftime('%H:%M')}] Geprüft: {count} Meldungen, nichts Neues.")
            except Exception as exc:  # Fehler sollen das Skript nicht beenden
                print(f"[{time.strftime('%H:%M')}] Fehler: {exc}")
                if "403" in str(exc):
                    print(f"Blockiert. Warte {BLOCK_PAUSE_SECONDS // 60} Minuten.")
                    time.sleep(BLOCK_PAUSE_SECONDS)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()

{
  "e9a36533-1045-40ed-9a5d-e04ab97caf89": "Kein Halt: U Senefelderplatz|Aufgrund von Bauarbeiten hält U2 Richtung S+U Pankow nicht an U Senefelderplatz. Um U Senefelderplatz zu erreichen, fahren Sie bitte bis zum nächsten U-Bahnhof U Eberswalder Straße und von dort wieder zurück.",
  "ee28879a-f062-4c3f-94fa-0b9da0898fde": "Nur Nachts: Ersatzverkehr|nur Sonntag bis Donnerstag, jeweils 22:00 Uhr bis 01:30 Uhr Ersatzverkehr U2 zwischen U Bismarckstraße U Zoologischer Garten Freitag und Samstag fährt die U2 wie gewohnt. Achtung Bitte beachten Sie die Unterbrechung Wittenbergplatz.",
  "HIM_FREETEXT_365466": "Kein Halt: Podbielskiallee|U3: Die Linie hält aufgrund von Bauarbeiten Richtung Krumme Lanke nicht im Bahnhof Podbielskiallee. Um den Bahnhof zu erreichen, fahren Sie bitte bis zum nächsten Bahnhof Dahlem-Dorf und von dort wieder zurück.",
  "a6954db9-f2fc-4acb-8724-394edcca34f7": "Sperrung wegen Bauarbeiten|nur Sonntag bis Donnerstag, jeweils 22:00 Uhr bis 01:30 Uhr Aufgrund von Bauarbeiten wird die U5 zwischen Tierpark und Elsterwerdaer Platz unterbrochen. Die Linie fährt U5 zwischen Hauptbahnhof Tierpark sowie zwischen Elsterwerdaer Platz Hönow. Zwischen Elsterwerdaer Platz Hönow ist ein Umstieg in Kaulsdorf-Nord erforderlich. Ersatzverkehr Ersatzverkehr U5 zwischen Tierpark Elsterwerdaer Platz. Shuttle U5 zwischen Biesdorf Süd Elsterwerdaer Platz. Freitag und Samstag fährt die U5 wie gewohnt. BVG-Herzensprojekte",
  "ef3b5e69-9734-42c0-aaa5-e3b48f2c242e": "Ersatzverkehr|Die Linie fährt U6 U Alt-Mariendorf U Kurt-Schumacher-Platz Ersatzverkehr U6 U Kurt-Schumacher-Platz U Alt-Tegel Hintergründe zum Bauvorhaben: U6-Nord Streckensanierung | BVG",
  "9397cf54-0531-42de-b551-1df0650c5740": "Kein Halt: U Seestraße|Aufgrund von Bauarbeiten hält U6 Richtung U Alt-Mariendorf nicht an U Seestraße. Um U Seestraße zu erreichen, fahren Sie bitte bis zum nächsten U-Bahnhof U Leopoldplatz und von dort wieder zurück.",
  "74a7b636-6daf-4687-9454-675f5a7c74af": "Kein Halt: U Güntzelstraße|Aufgrund von Bauarbeiten hält U9 Richtung Osloer Straße nicht an Güntzelstraße. Um U Güntzelstraße zu erreichen, fahren Sie bitte bis zum nächsten U-Bahnhof Spichernstraße und von dort wieder zurück. Gilt nicht 22:30 Uhr bis 1:00 Uhr, Sonntag bis Donnerstag"
}
