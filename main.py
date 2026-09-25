"""Branchenradar – wöchentlicher Branchenüberblick "Möbel & Onlinehandel".

Ein Werkbank-Modul, das jeden Montag früh (und auf Knopfdruck) über die
Anthropic-API mit dem serverseitigen Web-Such-Werkzeug einen konsistenten
Newsfeed erzeugt. Der komplette Spec-Prompt (Persona, Recherchefelder,
Sorgfaltsregeln, Ausgabeformat, Ton) ist fest verdrahtet und editierbar,
damit die Ergebnisse Woche für Woche gleich aufgebaut sind – nur der Inhalt
ist neu, weil die Nachrichten neu sind.

Nur für die Rolle "admin" (kostet pro Erstellung Anthropic-Guthaben).
"""

import io
import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import markdown as _md
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

import auth

APP_NAME = 'Branchenradar'
ACCENT = '#0f766e'
TYP = 'BR-12'

DATA_DIR = Path(os.environ.get('BR_DATA_DIR', '/data'))
BRIEF_PATH = DATA_DIR / 'briefings.json'
SPEC_PATH = DATA_DIR / 'spec.json'
STATUS_PATH = DATA_DIR / 'status.json'
AUTOMATIK_PATH = DATA_DIR / 'automatik.json'
LI_PATH = DATA_DIR / 'linkedin_posts.json'
CONTENT_PATH = DATA_DIR / 'content_posts.json'
CONTENT_SPEC_PATH = DATA_DIR / 'content_prompts.json'
PROFIL_PATH = DATA_DIR / 'profil.json'          # zentrales „Über mich" für alle Content-Agenten
TOPICS_PATH = DATA_DIR / 'themen.json'          # Themenspeicher (Ideen-Inbox)
REFS_DIR = DATA_DIR / 'refs'        # Referenzfotos (Gesicht) – bleiben auf dem Volume
STIL_DIR = DATA_DIR / 'stil'        # Stil-Referenzbilder (Look/Komposition, keine Identität)
BILDER_DIR = DATA_DIR / 'bilder'    # erzeugte Instagram-Bilder
AUDIT_DB = DATA_DIR / 'audit.db'

MODELL = os.environ.get('BRANCHENRADAR_MODELL', 'claude-opus-5')
# Nano Banana = Googles Gemini Bildmodell (eigener GEMINI_API_KEY, eigene Abrechnung)
GEMINI_BILD_MODELL = os.environ.get('GEMINI_BILD_MODELL', 'gemini-2.5-flash-image')
GEMINI_BILD_FORMAT = os.environ.get('GEMINI_BILD_FORMAT', '4:5')  # Instagram-Hochformat
# Standort fürs Live-Wetter (Outfit passend zum Tag). Default: Oberpfalz (Ebermannsdorf/Kümmersbruck).
WETTER_LAT = os.environ.get('WETTER_LAT', '49.38')
WETTER_LON = os.environ.get('WETTER_LON', '11.93')
WETTER_ORT = os.environ.get('WETTER_ORT', 'Oberpfalz')
MAX_PAUSE = 14  # Fortsetzungen für pause_turn (Web-Such-Schleife)

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)


# ── Persistenz ───────────────────────────────────────────────────────────────
def _lade(pfad: Path, standard):
    try:
        return json.loads(pfad.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return standard


def _sichere(pfad: Path, inhalt):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        pfad.write_text(json.dumps(inhalt, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


# ── Der feste Spec-Prompt (editierbar) ───────────────────────────────────────
DEFAULT_SPEC = """Du erstellst den wöchentlichen Branchenüberblick „Möbel & Onlinehandel".

## Wer das liest
Jörn Henseleit, Vertriebsleiter bei der Skyport GmbH. B2B-Großhandel für Möbel, reiner Onliner: Dropshipping, 24-Stunden-Versand, über 1.300 Artikel, die ein Händler ohne Kapitalbindung listen kann. Lieferung per Paketdienst, bei Speditionsversand frei Bordsteinkante. Kunden auch in der Schweiz, Norwegen und UK. Skyport verkauft daneben auch selbst an Endkunden – das ist kein Widerspruch, aber behaupte nie, Skyport verkaufe ausschließlich über Händler. Er schreibt auf LinkedIn aus Lieferantensicht über Verlässlichkeit, Verfügbarkeit, Produktdatenqualität und Lieferzeit. Keine Motivationsinhalte, keine Beratersprache.

## Recherche: die letzten 7 Tage
Nutze das Web-Such-Werkzeug ausgiebig und rufe seriöse Quellen direkt ab. Themenfelder:
1. Möbelbranche Deutschland – Branchenzahlen (VDM, HDH), Insolvenzen, Übernahmen, Personalien, Messen, Fachpresse (möbel kultur, MÖBELMARKT).
2. Möbel-Onlinehandel und E-Commerce – bevh-Zahlen, Marktplätze, Konsumklima, Studien von IFH und BBE.
3. Logistik und Zustellung – Paket- und Speditionsmarkt, Zustellkosten, Sperrgut, Retouren im Möbelversand.
4. Regulatorik mit Lieferantenrelevanz – Digitaler Produktpass, Verpackungs- und Produktrecht, Verbraucherrecht im Onlinehandel.
AUSSCHLUSS: Küchen sind nicht sein Feld. Küchenspezifische Meldungen weglassen, außer sie stecken in einer Gesamtbranchenzahl.

## Sorgfaltsregeln
- Jede Zahl braucht eine Quelle mit Link. Keine Zahl aus dem Gedächtnis.
- Prüfe bei Branchenzahlen immer die Bezugsebene. Cluster- und Untergruppenzahlen werden in der Presse regelmäßig verwechselt (z. B. „Holz- und Möbelindustrie" vs. reine „Möbelindustrie" vs. Untergruppe „Polstermöbel"). Wenn eine kursierende Zahl falsch verwendet wird, schreib das hin und nenne die richtige.
- Wenn zu einem Themenfeld nichts Belastbares zu finden war, schreib das offen statt die Lücke zu füllen.
- Kennzeichne Meldungen, die du nur aus einer einzigen Sekundärquelle hast.

## Ausgabe (genau dieses Format, als Markdown)
Beginne mit einer Überschrift und dem Zeitraum. Dann:
1. **Das Wichtigste in drei Sätzen** – was diese Woche wirklich zählt.
2. **Die Meldungen** – 5 bis 8 Stück. Je Meldung: was ist passiert, Quelle mit Link, und ein Absatz „Was das für dich bedeutet" aus Lieferantensicht.
3. **Zahlen der Woche** – Markdown-Tabelle mit Kennzahl, Wert, Quelle (als Link).
4. **Kommentar-Ansätze** – 3 bis 5 Vorschläge, worüber er diese Woche auf LinkedIn schreiben oder kommentieren könnte. Je Vorschlag: Aufhänger und Kerngedanke. NICHT ausformulieren – er will jeden Kommentar selbst freigeben.
5. **Quellen** – alle verwendeten Links gesammelt.

## Auswahlkriterien: was rein darf
Zahlen und Studien zu Möbel/Einrichtung/Onlinehandel; Marktbewegungen (Insolvenzen, Übernahmen, Neugründungen, Marktaustritte); Regulatorik, die Lieferanten oder Händler verpflichtet; Veränderungen bei Zustellung, Retouren und Logistikkosten; Studien zum Kaufverhalten mit belegter Zahl.

## Was NICHT rein gehört
Beiträge von Agenturen, Coaches, Vertriebstrainern, Beratern, Personalvermittlern; reine Werbe-/Produktankündigungen ohne Marktrelevanz; Küchenthemen; Lob auf den stationären Handel als Konzept (er ist Onliner) – aber ebenso wenig gegen den stationären Handel schießen (Verbundgruppen wie der Einrichtungspartnerring VME sind mögliche Online-Partner); Meinungsbeiträge ohne Faktengrundlage.

## Ton
Deutsch. Wie ein Kollege, der die Branche kennt und wenig Zeit hat. Ungleich lange Sätze, gesprochenes Register. Keine Pressemitteilungssprache, keine Floskeln, keine Emojis, keine Hashtags, keine Dreierfiguren als Stilmittel. Nicht werblich. Unbequeme Befunde nicht abmildern.

Wenn eine Woche wirklich nichts hergibt, sag das in einem Satz und liefere einen kurzen Überblick statt eines aufgeblähten. Gib AUSSCHLIESSLICH den fertigen Newsfeed als Markdown zurück – keine Vorrede, keine Meta-Kommentare über deinen Prozess."""


def _spec() -> str:
    s = _lade(SPEC_PATH, None)
    if isinstance(s, dict):
        return s.get('text') or DEFAULT_SPEC
    return DEFAULT_SPEC


LINKEDIN_SYS = """Du schreibst einen fertigen LinkedIn-Beitrag für Jörn Henseleit, Vertriebsleiter bei der Skyport GmbH (B2B-Möbelgroßhandel, reiner Onliner: Dropshipping, 24-Stunden-Versand, über 1.300 Artikel; Kunden auch in der Schweiz, Norwegen und UK; verkauft daneben auch selbst an Endkunden). Er schreibt aus Lieferantensicht über Verlässlichkeit, Verfügbarkeit, Produktdatenqualität und Lieferzeit.

Regeln:
- Kein Berater- oder Motivationssprech, keine Floskeln, keine Emojis, keine Dreierfiguren als Stilmittel. Nicht werblich. Unbequeme Befunde nicht abmildern.
- Ungleich lange Sätze, gesprochenes Register, wie ein Kollege, der die Branche kennt und wenig Zeit hat.
- Behaupte nie, Skyport verkaufe ausschließlich über Händler. Kein Lob auf den stationären Handel als Konzept, aber auch nicht dagegen schießen.
- Wenn du Zahlen oder Fakten nutzt, nur belegte aus der beigefügten Faktengrundlage, und nenne die Quelle knapp (z. B. „laut bevh"). Erfinde keine Zahlen.
- Länge etwa 120–220 Wörter, ein klarer Aufhänger, ein Kerngedanke, ein konkreter Abschluss ohne aufgesetzte Handlungsaufforderung.

Setze ans Ende des Beitrags maximal fünf passende, spezifische Hashtags in einer eigenen Zeile (deutsch, branchenbezogen – z. B. #Möbelhandel #Onlinehandel #Dropshipping #Logistik #Verpackungsverordnung; wähle nur die, die wirklich zum Beitrag passen, keine generischen Motivations-Hashtags). Gib AUSSCHLIESSLICH den fertigen Beitragstext samt dieser Hashtag-Zeile zurück – keine Vorrede, keine Überschrift, keine Varianten, keine Meta-Kommentare, keine Emojis."""


def _erzeuge_linkedin(thema: str, kontext_md: str = '') -> str:
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        raise RuntimeError('ANTHROPIC_API_KEY ist nicht gesetzt.')
    from anthropic import Anthropic
    client = Anthropic(api_key=key)
    user = f'Thema / Aufhänger für den Beitrag:\n{thema}\n'
    if kontext_md:
        user += ('\nFaktengrundlage (aktueller Branchenüberblick – nutze nur belegte Zahlen '
                 'mit Quelle, wenn sie zum Thema passen):\n\n' + kontext_md)
    with client.messages.stream(model=MODELL, max_tokens=8000, system=_mit_profil(LINKEDIN_SYS),
                                messages=[{'role': 'user', 'content': user}]) as stream:
        resp = stream.get_final_message()
    return ''.join(getattr(b, 'text', '') for b in resp.content
                   if getattr(b, 'type', None) == 'text').strip()


# ── Content-Pipeline: Agenten + zentrales Profil „Über mich" ─────────────────
# Ein editierbares Profil ist die gemeinsame Wissensbasis ALLER Content-Agenten;
# es wird ihrem System-Prompt vorangestellt. So pflegt der Nutzer sich an EINER
# Stelle statt in jedem einzelnen Prompt.
PROFIL_DEFAULT = (
    'Jörn Henseleit, Vertriebsleiter bei der Skyport GmbH.\n\n'
    'Skyport ist B2B-Großhandel für Möbel und reiner Onliner: Dropshipping, 24-Stunden-Versand, über '
    '1.300 Artikel, die ein Händler ohne Kapitalbindung listen kann. Lieferung per Paketdienst, bei '
    'Speditionsversand frei Bordsteinkante. Kunden auch in der Schweiz, Norwegen und UK. Skyport verkauft '
    'daneben auch selbst an Endkunden – kein Widerspruch; behaupte nie, Skyport verkaufe ausschließlich '
    'über Händler.\n\n'
    'Auf LinkedIn schreibe ich aus Lieferantensicht über Verlässlichkeit, Verfügbarkeit, '
    'Produktdatenqualität und Lieferzeit. Keine Motivationsinhalte, keine Beratersprache.\n\n'
    '— Dieses Profil ist die gemeinsame Grundlage aller Content-Agenten. Ergänze es gern: konkrete '
    'Zahlen/Fakten zu Skyport, deine Kernthemen und Haltung, was du bewusst NICHT sagst, 1–2 Stilproben '
    'aus echten Beiträgen.')

_TONREGELN = ('Kein Berater- oder Motivationssprech, keine Floskeln, keine Emojis, keine Dreierfiguren als '
              'Stilmittel, nicht werblich, unbequeme Befunde nicht abmildern. Ungleich lange Sätze, '
              'gesprochenes Register, wie ein Kollege, der die Branche kennt und wenig Zeit hat. Behaupte '
              'nie, Skyport verkaufe ausschließlich über Händler. Kein Lob auf den stationären Handel als '
              'Konzept, aber auch nicht dagegen schießen. Nutze nur belegte Zahlen aus der Faktengrundlage '
              'und nenne die Quelle knapp; erfinde keine Zahlen.')


def _profil() -> str:
    d = _lade(PROFIL_PATH, None)
    if isinstance(d, dict) and (d.get('text') or '').strip():
        return d['text']
    return PROFIL_DEFAULT


def _mit_profil(agent_prompt: str) -> str:
    """Stellt das zentrale Profil jedem Agenten-System-Prompt voran."""
    return f'## Über die Person, für die du arbeitest\n{_profil()}\n\n---\n\n{agent_prompt}'


KURATOR_DEFAULT = (
    'Du bist der Themen-Kurator für die Person, die oben im Profil beschrieben ist.\n\n'
    'Aufgabe: Wähle aus dem Thema des Nutzers und der Faktengrundlage den EINEN stärksten, '
    'posting-würdigen Aufhänger für diese Woche (bei leerem Thema wählst du selbst). Gib ein knappes '
    'Briefing zurück – KEINE ausformulierten Beiträge:\n'
    '- **Hook:** ein Satz.\n'
    '- **Kerngedanke:** 2–3 Sätze aus Lieferantensicht.\n'
    '- **Fakten & Quellen:** nur belegte Zahlen/Aussagen aus der Faktengrundlage, je mit Quelle.\n'
    '- **Warum jetzt:** ein Satz zur Relevanz.\n'
    'Wenn keine belegte Zahl zum Thema passt, sag das offen und schlage einen tragfähigen Blickwinkel '
    'ohne erfundene Zahlen vor. Gib nur das Briefing als Markdown zurück.')

TEXTER_DEFAULT = (
    'Du schreibst fertige Social-Media-Beiträge für die Person aus dem Profil oben.\n\n'
    f'Tonregeln für BEIDE Kanäle: {_TONREGELN}\n\n'
    'Erzeuge aus dem Briefing DREI Fassungen desselben Themas und trenne sie EXAKT mit diesen '
    'Markierungen, jeweils in einer eigenen Zeile:\n'
    '[LINKEDIN]\n'
    'Der LinkedIn-Beitrag (Post): 120–220 Wörter, ein klarer Aufhänger, ein Kerngedanke, ein konkreter '
    'Abschluss ohne aufgesetzte Handlungsaufforderung. Am Ende maximal 5 passende, spezifische, '
    'branchenbezogene Hashtags in einer eigenen Zeile.\n'
    '[NEWSLETTER]\n'
    'Der LinkedIn-Newsletter zum selben Thema, aus Lieferantensicht – länger und ausführlicher als der '
    'Post: erste Zeile „Titel:" mit einem prägnanten Titel, dann 300–600 Wörter Fließtext mit klarem '
    'Bogen (Einstieg, zwei bis drei Aspekte – bei Bedarf mit kurzen Zwischenüberschriften – und ein '
    'Abschluss, der einordnet statt zu werben). Gleicher Ton, keine Emojis, keine Hashtags.\n'
    '[INSTAGRAM]\n'
    'Die Instagram-Fassung, gleicher fachlicher Ton, ebenfalls keine Emojis: Hook in der ERSTEN Zeile '
    '(vor dem „mehr anzeigen"), danach kurze, durch Leerzeilen getrennte Absätze, insgesamt kürzer als '
    'LinkedIn. Darunter ein Hashtag-Block aus branchenbezogenen Hashtags (keine generischen '
    'Motivations-Hashtags). GANZ am Ende eine eigene Zeile, die mit „Bild-Briefing:" beginnt und in 1–2 '
    'Sätzen einen fertigen Bild-Prompt beschreibt – formuliert so, dass die Referenzperson (Jörn) im Bild '
    'vorkommt (z. B. „Referenzperson im Lager vor Palettenware, …"), sachlich und markenpassend, keine '
    'Effekthascherei.\n\n'
    'Gib ausschließlich die drei markierten Fassungen zurück – keine Vorrede, keine Meta-Kommentare.')

PRUEFER_DEFAULT = (
    'Du bist der kritische Lektor und Faktenprüfer für die Beiträge der Person aus dem Profil oben.\n\n'
    f'Prüfe die beiden Entwürfe streng gegen die Faktengrundlage und die Tonregeln: {_TONREGELN}\n\n'
    'Gib einen knappen Prüfbericht als Markdown zurück:\n'
    '- **LinkedIn-Post – Ampel:** Grün/Gelb/Rot, mit den konkreten Fundstellen.\n'
    '- **LinkedIn-Newsletter – Ampel:** Grün/Gelb/Rot, mit den konkreten Fundstellen.\n'
    '- **Instagram – Ampel:** Grün/Gelb/Rot, mit den konkreten Fundstellen.\n\n'
    'Prüfkriterien: (1) Jede Zahl/Behauptung im Entwurf muss in der Faktengrundlage mit Quelle stehen – '
    'markiere alles Unbelegte. (2) Tonverstöße: Emojis, Floskeln, Beratersprech, Dreierfiguren, die '
    'Aussage „ausschließlich über Händler", unpassendes Framing zum stationären Handel. (3) Hook-Stärke, '
    'Länge, Plattform-Passung. (4) Hashtags passend und branchenbezogen.\n'
    'Schlage KEINE fertige Neufassung vor – nenne nur die konkreten Korrekturen. Schließe mit einer Zeile '
    '„Empfehlung: …" (freigeben / überarbeiten).')

BILDPROMPT_DEFAULT = (
    'Du bist Bild-Prompt-Designer für die LinkedIn-/Instagram-Bilder der Person aus dem Profil oben.\n\n'
    'Aus dem Beitrag baust du EINEN fertigen Bild-Prompt für Googles Bildmodell (Nano Banana / Gemini).\n'
    'Immer gilt: ein hochwertiges, professionelles Foto der Referenzperson (Jörn) – souverän, sympathisch '
    'und gepflegt, hell und vorteilhaft beleuchtet, gestochen scharf, Premium-Qualität. Die Person wirkt '
    'LOCKER und NATÜRLICH – entspannte Körperhaltung, offener, sympathischer Ausdruck (gern ein leichtes '
    'Lächeln), NICHT steif oder verkrampft-gestellt; sie darf auch mal seitlich, in Bewegung oder im Tun '
    'sein statt nur starr in die Kamera zu blicken. KEIN düsterer, körniger oder dokumentarischer Look. '
    'Wahre Gesicht und Identität genau (gepflegter kurzer Bart, klare runde Brille, gepflegtes '
    'Erscheinungsbild).\n\n'
    'WICHTIG – Stimmung: Das Bild soll WARM, EINLADEND und PERSÖNLICH wirken, mit „Gemütlichkeit" und '
    'echter Ausstrahlung – NICHT kühl, glatt, steril oder wie ein Corporate-Stockfoto. Nutze warmes, '
    'weiches Licht (goldene, warme Töne; kein hartes, klinisch-kaltes Studiolicht), eine lebendige, '
    'gelebte Umgebung mit persönlichen, warmen Details (Holz, Pflanzen, weiche Materialien, eine '
    'Kaffeetasse, kleine persönliche Gegenstände) statt leerer, glatter Flächen. Ein echter, warmer, '
    'nahbarer Moment mit Persönlichkeit.\n\n'
    'WICHTIG – Themenbezug: Die Szene MUSS den Kern des Themas/Beitrags sichtbar aufgreifen (durch '
    'Handlung, Umgebung oder passende Requisiten, die zum konkreten Beitrag passen) und darf NIE ein '
    'beliebiges, themenfremdes Porträt oder ein halb leeres Bild sein. Das Bild soll sowohl im LinkedIn- '
    'als auch im Instagram-Feed funktionieren.\n\n'
    'Bestimme ZUERST aus Thema/Beitrag, ob es BERUFLICH oder PRIVAT/GESELLSCHAFTLICH ist, und wähle '
    'Umgebung UND Outfit entsprechend:\n'
    '• BERUFLICH (Skyport, Vertrieb, Handel, Branche): Wähle eine Umgebung aus Jörns Repertoire: '
    '(a) helles, modernes Büro am Schreibtisch mit Laptop; (b) am Fenster mit einer Tasse Kaffee oder '
    'einem Getränk in der Hand; (c) Lager-/Versandbereich – Variante (c) nur, wenn das Thema zu Logistik/'
    'Versand/Lieferzeit passt. Die Szene soll den Kern des Themas widerspiegeln. Outfit (Signature-Look): '
    'dunkelblaues Sakko ODER dunkelblaue Bomberjacke über einem sauberen weißen Rundhals-T-Shirt, gut '
    'sitzend – KEIN klassisches Hemd.\n'
    '• PRIVAT/GESELLSCHAFTLICH (Familie, Ehrenamt/Sportverein, Gemeinderat/Kommunales, mentale '
    'Gesundheit, Gemeinschaft, Work-Life-Balance): Wähle eine Umgebung aus Jörns Repertoire: '
    '(a) lockeres Alltagssetting (z. B. Café, Zuhause, in der Stadt); (b) draußen in der Natur der '
    'Oberpfalz (Feldweg, Wald, Landschaft); (c) am Sportplatz oder Vereinsheim (SpVgg Ebermannsdorf). '
    'Outfit gepflegt-lässig (schlichter Pullover oder Strick, Freizeitjacke, Jeans) – ruhig, nahbar, '
    'echt; hier KEIN Sakko und kein Büro erzwingen.\n\n'
    'Für BEIDE Fälle: Outfit/Jacke an Wetter und Jahreszeit aus dem oben genannten Kontext anpassen '
    '(wärmer leichter, kühler mit Jacke, kalt mit Mantel), aber immer gepflegt und hochwertig. '
    'Komposition: Hochformat 4:5, die Person zu EINER Seite gesetzt, sodass auf der anderen genug ruhige, '
    'freie Fläche für eine spätere Textüberschrift bleibt. KEIN Text im Bild, keine Schrift, keine Logos, '
    'keine Collage; erfinde keine Marken.\n'
    'Gib AUSSCHLIESSLICH den fertigen Bild-Prompt als Fließtext zurück (3–5 Sätze) – keine Überschrift, '
    'keine Erklärung, keine Varianten.')

IDEEN_DEFAULT = (
    'Du bist Themen-Ideengeber für die Person aus dem Profil oben.\n\n'
    'Schlage konkrete, posting-würdige Themen/Aufhänger vor, die zu ihrem Profil, ihren Kernthemen und '
    'ihrer Haltung passen – aus Lieferantensicht ebenso wie aus dem persönlichen/ehrenamtlichen Bereich, '
    'wenn es passt. Keine ausgelutschten Motivationsthemen, keine Beratersprech-Titel. Jede Idee ist ein '
    'konkreter Aufhänger, aus dem sich ein Beitrag bauen lässt – nicht nur ein Schlagwort.\n'
    'Format: eine Idee pro Zeile, jeweils beginnend mit „- ", der Aufhänger in einem Satz (optional ein '
    'knapper Halbsatz zum Blickwinkel). Keine Nummerierung, keine Überschrift, keine Einleitung, keine '
    'Erklärung – nur die Liste.')


def _content_prompts() -> dict:
    d = _lade(CONTENT_SPEC_PATH, None)
    if not isinstance(d, dict):
        d = {}
    return {'kurator': d.get('kurator') or KURATOR_DEFAULT,
            'texter': d.get('texter') or TEXTER_DEFAULT,
            'pruefer': d.get('pruefer') or PRUEFER_DEFAULT,
            'bildprompt': d.get('bildprompt') or BILDPROMPT_DEFAULT,
            'ideen': d.get('ideen') or IDEEN_DEFAULT}


def _ki_text(system: str, user: str, max_tokens: int = 4000) -> str:
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        raise RuntimeError('ANTHROPIC_API_KEY ist nicht gesetzt.')
    from anthropic import Anthropic
    client = Anthropic(api_key=key)
    resp = client.messages.create(model=MODELL, max_tokens=max_tokens, system=system,
                                  messages=[{'role': 'user', 'content': user}])
    return ''.join(getattr(b, 'text', '') for b in resp.content
                   if getattr(b, 'type', None) == 'text').strip()


def _split_kanaele(text: str):
    """Zerlegt die Texter-Ausgabe an [LINKEDIN]/[NEWSLETTER]/[INSTAGRAM] in drei Fassungen."""
    rest = text or ''
    ig = nl = ''
    if '[INSTAGRAM]' in rest:
        rest, ig = rest.split('[INSTAGRAM]', 1)
    if '[NEWSLETTER]' in rest:
        rest, nl = rest.split('[NEWSLETTER]', 1)
    li = rest.replace('[LINKEDIN]', '').strip()
    return li, nl.strip(), ig.strip()


def _jahreszeit(monat: int) -> str:
    return {12: 'Winter', 1: 'Winter', 2: 'Winter', 3: 'Frühling', 4: 'Frühling', 5: 'Frühling',
            6: 'Sommer', 7: 'Sommer', 8: 'Sommer', 9: 'Herbst', 10: 'Herbst', 11: 'Herbst'}.get(monat, '')


def _wettercode(code) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return 'wechselhaft'
    if code == 0:
        return 'klar/sonnig'
    if code in (1, 2):
        return 'leicht bewölkt'
    if code == 3:
        return 'bedeckt'
    if code in (45, 48):
        return 'neblig'
    if code in (51, 53, 55, 56, 57):
        return 'Nieselregen'
    if code in (61, 63, 65, 66, 67):
        return 'Regen'
    if code in (71, 73, 75, 77, 85, 86):
        return 'Schnee'
    if code in (80, 81, 82):
        return 'Regenschauer'
    if code in (95, 96, 99):
        return 'Gewitter'
    return 'wechselhaft'


def _wetter_kontext() -> str:
    """Kurzer Wetter-/Jahreszeit-Kontext für heute (Live über Open-Meteo, Fallback = Jahreszeit)."""
    now = datetime.now()
    ctx = f'Heutiges Datum: {now.strftime("%d.%m.%Y")}. Jahreszeit: {_jahreszeit(now.month)}. Region: {WETTER_ORT}'
    try:
        import urllib.request
        url = (f'https://api.open-meteo.com/v1/forecast?latitude={WETTER_LAT}&longitude={WETTER_LON}'
               '&current=temperature_2m,weather_code')
        with urllib.request.urlopen(url, timeout=6) as r:  # noqa: S310 (öffentliche Wetter-API)
            cur = json.loads(r.read().decode('utf-8')).get('current', {})
        temp = cur.get('temperature_2m')
        if temp is not None:
            ctx += f', aktuell rund {round(float(temp))} °C, {_wettercode(cur.get("weather_code"))}'
    except Exception:  # noqa: BLE001 - ohne Live-Wetter bleibt die Jahreszeit
        pass
    return ctx + '. Wähle Kleidung passend dazu.'


def _content_pipeline(thema: str, kontext_md: str = '') -> dict:
    """Drei verkettete Agenten: Kurator -> Texter -> Prüfer. Reine Text-Ausgaben."""
    p = _content_prompts()
    fakt = kontext_md.strip() or ('(Keine Faktengrundlage übergeben – arbeite nur mit dem Thema und '
                                  'erfinde keine Zahlen.)')
    thema_txt = thema.strip() or '(Kein Thema vorgegeben – wähle den stärksten Aufhänger aus der Faktengrundlage.)'

    brief = _ki_text(_mit_profil(p['kurator']),
                     f'Thema/Aufhänger vom Nutzer:\n{thema_txt}\n\n'
                     f'Faktengrundlage (aktueller Branchenüberblick):\n\n{fakt}', 2000)
    doppel = _ki_text(_mit_profil(p['texter']),
                      f'Briefing:\n\n{brief}\n\nFaktengrundlage:\n\n{fakt}', 4500)
    linkedin, newsletter, instagram = _split_kanaele(doppel)
    pruef = _ki_text(_mit_profil(p['pruefer']),
                     f'Faktengrundlage:\n\n{fakt}\n\nLinkedIn-Post:\n{linkedin}\n\n'
                     f'LinkedIn-Newsletter:\n{newsletter}\n\nInstagram-Entwurf:\n{instagram}', 2200)
    bildprompt = _ki_text(_mit_profil(p['bildprompt']),
                          f'Wetter-Kontext (für ein aktuelles, wetterpassendes Outfit):\n{_wetter_kontext()}\n\n'
                          f'Thema/Briefing:\n\n{brief}\n\nInstagram-Fassung:\n{instagram}', 800)
    return {'brief': brief, 'linkedin': linkedin, 'newsletter': newsletter, 'instagram': instagram,
            'pruef': pruef, 'bildprompt': bildprompt}


def _content_laden():
    liste = _lade(CONTENT_PATH, [])
    return liste if isinstance(liste, list) else []


def _content_speichern(eintrag: dict) -> str:
    liste = _content_laden()
    liste.insert(0, eintrag)
    _sichere(CONTENT_PATH, liste[:40])
    return eintrag['id']


def _content_aktualisieren(cid: str, linkedin: str, newsletter: str, instagram: str):
    liste = _content_laden()
    for e in liste:
        if e.get('id') == cid:
            e['linkedin'] = (linkedin or '').strip()
            e['newsletter'] = (newsletter or '').strip()
            e['instagram'] = (instagram or '').strip()
            _sichere(CONTENT_PATH, liste)
            return


def _content_loeschen(cid: str):
    _sichere(CONTENT_PATH, [e for e in _content_laden() if e.get('id') != cid])


# ── Themen-System: Ideengenerator + Themenspeicher ───────────────────────────
def _themen_laden():
    liste = _lade(TOPICS_PATH, [])
    return liste if isinstance(liste, list) else []


def _thema_speichern(titel: str, notiz: str = '') -> str:
    titel = (titel or '').strip()
    if not titel:
        return ''
    liste = _themen_laden()
    tid = secrets.token_hex(6)
    liste.insert(0, {'id': tid, 'ts': datetime.now().isoformat(timespec='seconds'),
                     'datum': datetime.now().strftime('%d.%m.%Y'), 'titel': titel,
                     'notiz': (notiz or '').strip()})
    _sichere(TOPICS_PATH, liste[:100])
    return tid


def _thema_loeschen(tid: str):
    _sichere(TOPICS_PATH, [t for t in _themen_laden() if t.get('id') != tid])


def _ideen_generieren(fokus: str = '', anzahl: int = 12):
    """KI schlägt Themen aus dem Profil (+ optionalem Fokus) vor. -> Liste von Strings."""
    p = _content_prompts()
    user = f'Schlage {anzahl} konkrete Themen/Aufhänger vor.'
    if (fokus or '').strip():
        user += f'\n\nAktueller Fokus / Wunschrichtung des Nutzers:\n{fokus.strip()}'
    text = _ki_text(_mit_profil(p['ideen']), user, 1500)
    ideen = []
    for zeile in (text or '').splitlines():
        z = zeile.strip().lstrip('-*•').strip()
        z = re.sub(r'^\d+[\.\)]\s*', '', z).strip()
        if z:
            ideen.append(z)
    return ideen


# ── Bildgenerierung (Nano Banana / Gemini) mit Referenzgesicht ───────────────
_BILD_EXT = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}
_BILD_SUFFIXE = ('.jpg', '.jpeg', '.png', '.webp')


def _sichere_bytes(pfad: Path, daten: bytes):
    try:
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_bytes(daten)
    except OSError:
        pass


def _sicherer_name(name: str) -> str:
    """Nur Basisname, keine Pfadanteile (gegen Directory Traversal)."""
    return os.path.basename(str(name or '')).replace('\x00', '')


def _ref_liste():
    try:
        return sorted(p.name for p in REFS_DIR.iterdir()
                      if p.is_file() and p.suffix.lower() in _BILD_SUFFIXE)
    except (OSError, FileNotFoundError):
        return []


def _stil_liste():
    try:
        return sorted(p.name for p in STIL_DIR.iterdir()
                      if p.is_file() and p.suffix.lower() in _BILD_SUFFIXE)
    except (OSError, FileNotFoundError):
        return []


def _bild_briefing(instagram_text: str) -> str:
    """Extrahiert den Bild-Prompt aus der „Bild-Briefing:"-Zeile der IG-Fassung."""
    t = instagram_text or ''
    m = re.search(r'Bild-?Briefing\s*:\s*(.+)', t, re.IGNORECASE | re.DOTALL)
    return (m.group(1).strip() if m else t.strip())


def _erzeuge_bild(prompt: str, ref_paths, stil_paths=None):
    """Ruft Gemini (Nano Banana) mit Prompt + Gesichts- und optionalen Stil-Referenzen auf.
    -> (bytes, mime)."""
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise RuntimeError('GEMINI_API_KEY ist nicht gesetzt.')
    if not ref_paths:
        raise RuntimeError('Kein Referenzfoto hinterlegt.')
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)

    def _teil(pfad):
        try:
            daten = Path(pfad).read_bytes()
        except OSError:
            return None
        mime = 'image/png' if str(pfad).lower().endswith('.png') else 'image/jpeg'
        return types.Part.from_bytes(data=daten, mime_type=mime)

    voll = ('Erstelle ein hochwertiges, professionelles Foto im Hochformat für einen LinkedIn-/Instagram-'
            'Post – gepflegt und scharf, aber vor allem WARM, EINLADEND und PERSÖNLICH: warmes, weiches '
            'Licht (goldene Töne) und eine gemütliche, lebendige Atmosphäre mit echter Ausstrahlung, kein '
            'kühler, klinisch-steriler oder glatter Corporate-Stockfoto-Look. Die abgebildete Person ist '
            'die Referenzperson aus den ersten beigefügten Fotos – wahre ihr Gesicht und ihre Identität '
            'möglichst genau, gepflegtes Erscheinungsbild. Kein Text im Bild, keine Logos. Motiv: '
            + (prompt or '').strip())
    contents = [voll]
    for rp in ref_paths:
        teil = _teil(rp)
        if teil is not None:
            contents.append(teil)
    stil_teile = [t for t in (_teil(sp) for sp in (stil_paths or [])) if t is not None]
    if stil_teile:
        contents.append('Die folgenden Bilder dienen NUR als Vorlage für Look, Bildstil, Licht und Farben '
                        '– übernimm diesen Stil, aber NICHT eine fremde Identität und NICHT leere '
                        'Textflächen oder das Layout 1:1. Person und thementypische Szene stehen im '
                        'Mittelpunkt; das Bild darf nicht halb leer sein:')
        contents.extend(stil_teile)

    def _call(mit_format: bool):
        cfg = {'response_modalities': ['IMAGE']}
        if mit_format:
            cfg['image_config'] = types.ImageConfig(aspect_ratio=GEMINI_BILD_FORMAT)
        return client.models.generate_content(
            model=GEMINI_BILD_MODELL, contents=contents,
            config=types.GenerateContentConfig(**cfg))
    try:
        resp = _call(True)
    except Exception:  # noqa: BLE001 - ältere SDKs kennen aspect_ratio evtl. nicht
        resp = _call(False)

    for part in (getattr(resp, 'parts', None) or []):
        inline = getattr(part, 'inline_data', None)
        if inline is not None and getattr(inline, 'data', None):
            daten = inline.data
            if isinstance(daten, str):
                import base64 as _b64
                daten = _b64.b64decode(daten)
            return daten, (getattr(inline, 'mime_type', None) or 'image/png')
    raise RuntimeError('Gemini hat kein Bild zurückgegeben (evtl. blockiert oder Nur-Text-Antwort).')


def _gemini_selftest() -> str:
    """Minimaler Gemini-Bildaufruf ohne Referenzfoto. -> 'OK' oder Fehlertext."""
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        return 'GEMINI_API_KEY ist nicht gesetzt.'
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=GEMINI_BILD_MODELL,
            contents=['Ein einfaches, neutrales Testbild: eine schlichte hellgraue Fläche.'],
            config=types.GenerateContentConfig(response_modalities=['IMAGE']))
        for part in (getattr(resp, 'parts', None) or []):
            inline = getattr(part, 'inline_data', None)
            if inline is not None and getattr(inline, 'data', None):
                return 'OK'
        return 'Verbindung stand, aber kein Bild in der Antwort (evtl. Safety-Filter oder Nur-Text).'
    except Exception as e:  # noqa: BLE001
        return 'FEHLER: ' + str(e)[:400]


def _font(size: int):
    from PIL import ImageFont
    for pfad in ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(pfad, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _wasserzeichen(daten: bytes, text: str = 'AI-generated') -> bytes:
    """Legt jedem Bild einen sichtbaren „AI-generated"-Hinweis unten rechts auf.
    Wirft bei Fehler eine Exception – es wird nie ein Bild OHNE Hinweis gespeichert."""
    from PIL import Image, ImageDraw
    img = Image.open(io.BytesIO(daten)).convert('RGB')
    draw = ImageDraw.Draw(img, 'RGBA')
    w, h = img.size
    size = max(10, w // 58)          # klein und dezent (kleiner als zuvor)
    font = _font(size)
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th, offx, offy = bbox[2] - bbox[0], bbox[3] - bbox[1], bbox[0], bbox[1]
    except AttributeError:  # sehr alte Pillow
        tw, th = draw.textsize(text, font=font)
        offx = offy = 0
    pad = max(3, size // 4)
    rand = max(7, w // 90)
    box_w, box_h = tw + pad * 2, th + pad * 2
    bx, by = w - box_w - rand, h - box_h - rand
    draw.rectangle([bx, by, bx + box_w, by + box_h], fill=(0, 0, 0, 105))
    draw.text((bx + pad - offx, by + pad - offy), text, font=font, fill=(255, 255, 255, 225))
    out = io.BytesIO()
    img.save(out, format='PNG')
    return out.getvalue()


def _bild_fuer_content(cid: str):
    """Erzeugt ein Bild für den gespeicherten Content-Eintrag, Ablage unter /data/bilder."""
    liste = _content_laden()
    eintrag = next((e for e in liste if e.get('id') == cid), None)
    if not eintrag:
        raise RuntimeError('Eintrag nicht gefunden.')
    prompt = (eintrag.get('bildprompt') or '').strip() or _bild_briefing(eintrag.get('instagram') or '')
    refs = [str(REFS_DIR / n) for n in _ref_liste()]
    stil = [str(STIL_DIR / n) for n in _stil_liste()]
    daten, _mime = _erzeuge_bild(prompt, refs, stil)
    daten = _wasserzeichen(daten)          # Pflicht: sichtbarer „AI-generated"-Hinweis auf JEDEM Bild
    ext = '.png'
    name = f'{cid}{ext}'
    _sichere_bytes(BILDER_DIR / name, daten)
    for e2 in set(_BILD_EXT.values()):     # alte Bilddatei anderer Endung aufräumen
        alt = BILDER_DIR / f'{cid}{e2}'
        if e2 != ext and alt.exists():
            try:
                alt.unlink()
            except OSError:
                pass
    for e in liste:
        if e.get('id') == cid:
            e['bild'] = name
            e['bild_ts'] = datetime.now().strftime('%d.%m.%Y %H:%M')
            break
    _sichere(CONTENT_PATH, liste)


def _varianten_prompts(brief: str, instagram: str, anzahl: int = 3):
    """Lässt den Bild-Agenten GENAU `anzahl` Prompts zum selben Thema in unterschiedlichen Umgebungen bauen."""
    p = _content_prompts()
    user = (f'Wetter-Kontext (für ein aktuelles, wetterpassendes Outfit):\n{_wetter_kontext()}\n\n'
            'Erzeuge AUSNAHMSWEISE GENAU 3 Bild-Prompts zum SELBEN Thema – einen je Umgebung. Erkenne '
            'zuerst, ob das Thema BERUFLICH oder PRIVAT ist, und nutze dann GENAU diese drei Umgebungen '
            '(je eine pro Prompt, klar verschieden):\n'
            '– beruflich: 1) helles modernes Büro am Schreibtisch, 2) am Fenster mit Kaffee/Getränk in der '
            'Hand, 3) Lager-/Versandbereich.\n'
            '– privat: 1) lockeres Alltagssetting, 2) draußen in der Natur der Oberpfalz, 3) Sportplatz/'
            'Vereinsheim.\n'
            'Trenne die drei Prompts durch eine eigene Zeile, die nur "---" enthält – sonst kein weiterer '
            f'Text.\n\nThema/Briefing:\n\n{brief}\n\nInstagram-Fassung:\n{instagram}')
    text = _ki_text(_mit_profil(p['bildprompt']), user, 1800)
    teile = [t.strip() for t in re.split(r'(?m)^\s*-{3,}\s*$', text) if t.strip()]
    return teile[:anzahl]


def _bilder_set_erzeugen(cid: str, anzahl: int = 3):
    """Erzeugt mehrere Bild-Varianten (verschiedene Umgebungen) für einen Eintrag zur Auswahl."""
    liste = _content_laden()
    eintrag = next((e for e in liste if e.get('id') == cid), None)
    if not eintrag:
        raise RuntimeError('Eintrag nicht gefunden.')
    prompts = _varianten_prompts(eintrag.get('brief') or '', eintrag.get('instagram') or '', anzahl)
    if not prompts:
        prompts = [(eintrag.get('bildprompt') or '').strip() or _bild_briefing(eintrag.get('instagram') or '')]
    refs = [str(REFS_DIR / n) for n in _ref_liste()]
    stil = [str(STIL_DIR / n) for n in _stil_liste()]
    for alt in (eintrag.get('varianten') or []):        # alte Varianten-Dateien aufräumen
        try:
            (BILDER_DIR / _sicherer_name(alt.get('name', ''))).unlink()
        except OSError:
            pass
    ts = datetime.now().strftime('%d.%m.%Y %H:%M')
    varianten = []
    for i, pr in enumerate(prompts):
        daten, _m = _erzeuge_bild(pr, refs, stil)
        daten = _wasserzeichen(daten)
        name = f'{cid}_v{i + 1}.png'
        _sichere_bytes(BILDER_DIR / name, daten)
        varianten.append({'name': name, 'prompt': pr, 'ts': ts})
    for e in liste:
        if e.get('id') == cid:
            e['varianten'] = varianten
            _sichere(CONTENT_PATH, liste)
            break


def _bild_block(eintrag: dict, cid: str, gemini_aktiv: bool, hat_refs: bool) -> str:
    bild = eintrag.get('bild')
    prompt = (eintrag.get('bildprompt') or '').strip() or _bild_briefing(eintrag.get('instagram') or '')
    vorschau = ''
    if bild:
        vorschau = (
            f'<div style="margin:6px 0"><img src="/content/bild/{_esc(bild)}" alt="" '
            'style="max-width:240px;border:1px solid var(--line);border-radius:8px;display:block">'
            f'<div class="row" style="margin-top:4px"><a class="btn ghost" href="/content/bild/{_esc(bild)}" '
            f'download>Bild herunterladen</a> <span class="hint" style="align-self:center">erstellt '
            f'{_esc(eintrag.get("bild_ts") or "")}</span></div></div>')
    if gemini_aktiv and hat_refs:
        label = 'Bild neu erzeugen' if bild else 'Bild erzeugen (Nano Banana)'
        erzeugen = (f'<button class="btn ghost" type="submit" formaction="/content/{_esc(cid)}/bild">'
                    f'{label}</button>'
                    f'<button class="btn ghost" type="submit" formaction="/content/{_esc(cid)}/bilder3">'
                    '3 Varianten (verschiedene Umgebungen)</button>')
    else:
        grund = 'GEMINI_API_KEY fehlt' if not gemini_aktiv else 'kein Referenzfoto hinterlegt'
        erzeugen = f'<span class="hint" style="align-self:center">Bildgenerierung nicht möglich ({grund}).</span>'

    # Varianten-Auswahl (3 verschiedene Umgebungen) anzeigen
    varianten = eintrag.get('varianten') or []
    var_html = ''
    if varianten:
        kacheln = ''
        for v in varianten:
            n = v.get('name') or ''
            kacheln += (
                '<div style="display:inline-block;vertical-align:top;margin:0 10px 10px 0;max-width:220px">'
                f'<img src="/content/bild/{_esc(n)}" alt="" style="width:220px;border:1px solid var(--line);'
                'border-radius:8px;display:block">'
                f'<div class="row" style="margin-top:3px"><a class="btn ghost" href="/content/bild/{_esc(n)}" '
                'download>herunterladen</a></div></div>')
        var_html = ('<div class="hint" style="margin:12px 0 4px">Zur Auswahl &middot; 3 Varianten in '
                    'verschiedenen Umgebungen (lade dir die beste herunter)</div>'
                    f'<div>{kacheln}</div>')

    # EIN Formular: „Bild erzeugen" nimmt genau den Text aus dem Feld (kein Prompt-Verlust mehr).
    return ('<div class="hint" style="margin:12px 0 2px">Bild (LinkedIn &amp; Instagram)</div>' + vorschau
            + '<div class="hint" style="margin:6px 0 2px">Bild-Prompt (KI-Vorschlag &ndash; editierbar; '
            '„Bild erzeugen" verwendet genau diesen Text)</div>'
            f'<form method="post" action="/content/{_esc(cid)}/bildprompt">'
            f'<textarea name="bildprompt" rows="4" style="width:100%">{_esc(prompt)}</textarea>'
            '<div class="row" style="margin-top:6px">'
            '<button class="btn ghost" type="submit">Prompt speichern</button>'
            + erzeugen + '</div></form>' + var_html)


# ── Zugang (nur admin) ───────────────────────────────────────────────────────
def _is_admin(user):
    return bool(user) and user.get('role') == 'admin'


async def br_middleware(request, call_next):
    user = auth.get_current_user(request)
    request.state.user = user
    if request.url.path in ('/login', '/health'):
        return await call_next(request)
    if _is_admin(user):
        return await call_next(request)
    if user:
        return HTMLResponse(_seite(
            '<div class="plate"><div><div class="eyebrow">Kein Zugriff</div>'
            '<h1>Nur f&uuml;r die Skyport-Vertriebsleitung</h1></div></div>'
            '<div class="card" style="max-width:520px"><p>Dieses Werkzeug ist nicht Teil Ihres Zugangs.</p>'
            '<div class="row"><a class="btn secondary" href="/logout" style="text-decoration:none">Abmelden</a>'
            '</div></div>', user), status_code=403)
    if request.method == 'GET':
        return RedirectResponse('/login', status_code=303)
    return JSONResponse(status_code=401, content={'detail': 'Nicht angemeldet.'})


app.add_middleware(BaseHTTPMiddleware, dispatch=br_middleware)


@app.get('/login', response_class=HTMLResponse)
def login_page():
    return HTMLResponse(auth.login_page_html(app_name=APP_NAME))


@app.post('/login')
async def login_submit(request: Request):
    form = await request.form()
    user = auth.authenticate(form.get('email', ''), form.get('password', ''))
    if not user:
        return HTMLResponse(auth.login_page_html(error='E-Mail oder Passwort falsch', app_name=APP_NAME))
    if not _is_admin(user):
        return HTMLResponse(auth.login_page_html(
            error='Dieses Werkzeug ist nur f&uuml;r die Vertriebsleitung freigegeben.', app_name=APP_NAME))
    token = auth.create_token(user['email'], user['role'])
    resp = RedirectResponse('/', status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_HOURS * 3600,
                    httponly=True, samesite='lax')
    return resp


@app.get('/logout')
def logout(request: Request):
    resp = RedirectResponse('/login', status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get('/health')
def health():
    return {'status': 'ok'}


# ── Seiten-Shell (Werkbank-Layout) ───────────────────────────────────────────
def _seite(inhalt: str, user=None) -> str:
    kopf = auth.user_header_html(user) if user else ''
    return (
        '<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{APP_NAME} &middot; Skyport Werkbank</title>'
        '<link rel="stylesheet" href="https://skyport-werkbank.sliplane.app/werkbank.css">'
        f'<style>:root{{--accent:{ACCENT}}}'
        '.feed{max-width:820px}'
        '.feed h1{font-size:26px;margin:8px 0 2px}.feed h2{font-size:19px;margin:26px 0 8px;'
        'border-bottom:1px solid var(--line);padding-bottom:5px}'
        '.feed h3{font-size:16px;margin:20px 0 6px}'
        '.feed p{line-height:1.65;margin:9px 0}.feed ul{line-height:1.6}'
        '.feed table{width:100%;border-collapse:collapse;margin:12px 0;font-size:14px}'
        '.feed th,.feed td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}'
        '.feed th{background:#f2f6f6}'
        '.feed a{color:var(--accent)}.feed blockquote{border-left:3px solid var(--line);'
        'margin:10px 0;padding:2px 14px;color:var(--muted)}'
        '.statusbox{border:1px solid var(--line);border-radius:10px;padding:14px 16px;'
        'background:var(--surface);margin-top:14px}'
        '.laeuft{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}'
        '.altliste a{display:block;padding:8px 0;border-bottom:1px solid var(--line);text-decoration:none;color:var(--ink)}'
        '.altliste a:hover{color:var(--accent)}'
        '.subtabs{display:flex;gap:2px;border-bottom:2px solid var(--line);margin:20px 0 4px;flex-wrap:wrap}'
        '.subtabs a{padding:11px 20px;font-family:var(--cond);font-weight:600;text-transform:uppercase;'
        'letter-spacing:.08em;font-size:12.5px;color:var(--muted);text-decoration:none;'
        'border-bottom:2px solid transparent;margin-bottom:-2px}'
        '.subtabs a.on{color:var(--accent);border-bottom-color:var(--accent)}'
        '.subtabs a:hover{color:var(--ink)}'
        '</style></head><body>'
        '<header class="bar"><div class="brand">'
        '<a href="https://skyport-werkbank.sliplane.app">Skyport <b>&middot;</b> Werkbank</a></div>'
        '<nav>'
        '<a href="https://skyport-werkbank.sliplane.app">&Uuml;bersicht</a>'
        '<a href="https://sortimentsabgleich.sliplane.app">Sortiment</a>'
        '<a href="https://schweiz-export.sliplane.app">Schweiz Export</a>'
        '<a class="on" href="/">Branchenradar</a>'
        '</nav>'
        f'<div class="live">{kopf}</div></header>'
        '<div class="band"></div><main>' + inhalt + '</main></body></html>')


def _platte(sub: str = '') -> str:
    return ('<div class="plate"><div><div class="eyebrow">Werkzeug</div>'
            f'<h1>{APP_NAME}</h1>'
            + (f'<p class="sub">{sub}</p>' if sub else '') +
            f'</div><div class="typ"><span>Typ</span><strong>{TYP}</strong></div></div>')


def _md_html(text: str) -> str:
    return _md.markdown(text or '', extensions=['tables', 'fenced_code', 'sane_lists'])


def _subtabs(active: str) -> str:
    def a(key, label, href):
        cls = ' class="on"' if active == key else ''
        return f'<a href="{href}"{cls}>{label}</a>'
    return ('<div class="subtabs">'
            + a('ueberblick', 'Wochenüberblick', '/')
            + a('themen', 'Themen', '/themen')
            + a('content', 'Content-Pipeline', '/content')
            + a('linkedin', 'LinkedIn-Beiträge', '/linkedin')
            + '</div>')


# ── Erzeugung (Anthropic + Web-Suche, pause_turn-Schleife) ───────────────────
_lock = threading.Lock()


def _erzeuge_briefing() -> str:
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        raise RuntimeError('ANTHROPIC_API_KEY ist nicht gesetzt.')
    from anthropic import Anthropic
    client = Anthropic(api_key=key)
    heute = datetime.now().strftime('%d.%m.%Y')
    system = _spec()
    user = (f'Heutiges Datum: {heute}. Erstelle jetzt den wöchentlichen Branchenüberblick '
            '„Möbel & Onlinehandel" für die letzten 7 Tage. Nutze das Web-Such-Werkzeug '
            'ausgiebig, prüfe jede Zahl an der Quelle und liefere exakt das im System '
            'vorgegebene Format als Markdown.')
    tools = [{'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': 25}]
    messages = [{'role': 'user', 'content': user}]
    resp = None
    for _ in range(MAX_PAUSE):
        with client.messages.stream(model=MODELL, max_tokens=32000, system=system,
                                    tools=tools, messages=messages) as stream:
            resp = stream.get_final_message()
        if resp.stop_reason == 'pause_turn':
            messages.append({'role': 'assistant', 'content': resp.content})
            continue
        break
    if resp is None:
        raise RuntimeError('Keine Antwort erhalten.')
    text = ''.join(getattr(b, 'text', '') for b in resp.content
                   if getattr(b, 'type', None) == 'text').strip()
    if not text:
        raise RuntimeError('Leere Antwort (evtl. Refusal oder Modellfehler).')
    return text


def _speichere_briefing(text: str):
    liste = _lade(BRIEF_PATH, []) or []
    jetzt = datetime.now()
    liste.insert(0, {
        'id': secrets.token_hex(6),
        'ts': jetzt.isoformat(timespec='seconds'),
        'datum': jetzt.strftime('%d.%m.%Y'),
        'md': text,
    })
    _sichere(BRIEF_PATH, liste[:16])


def _run_generation(quelle='manuell'):
    if not _lock.acquire(blocking=False):
        return  # läuft schon
    try:
        _sichere(STATUS_PATH, {'status': 'laeuft', 'quelle': quelle,
                               'seit': datetime.now().isoformat(timespec='seconds')})
        text = _erzeuge_briefing()
        _speichere_briefing(text)
        _sichere(STATUS_PATH, {'status': 'fertig',
                               'zeit': datetime.now().isoformat(timespec='seconds')})
    except Exception as e:  # noqa: BLE001
        _sichere(STATUS_PATH, {'status': 'fehler', 'meldung': str(e)[:400],
                               'zeit': datetime.now().isoformat(timespec='seconds')})
    finally:
        _lock.release()


# ── Automatik: jeden Montag 07:00 Europe/Berlin (per Schalter an/aus) ─────────
def _automatik_an() -> bool:
    """Ist der Montags-Automatiklauf aktiv? Standard = an (bisheriges Verhalten)."""
    d = _lade(AUTOMATIK_PATH, None)
    if isinstance(d, dict) and 'an' in d:
        return bool(d['an'])
    return True


def _automatik_setzen(an: bool):
    _sichere(AUTOMATIK_PATH, {'an': bool(an),
                              'geaendert': datetime.now().isoformat(timespec='seconds')})


def _automatik_lauf():
    """Cron-Ziel: erzeugt nur, wenn die Automatik eingeschaltet ist."""
    if _automatik_an():
        _run_generation('automatik')


try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _sched = BackgroundScheduler(timezone='Europe/Berlin')
    _sched.add_job(_automatik_lauf, 'cron',
                   day_of_week='mon', hour=7, minute=0, id='montag',
                   misfire_grace_time=3600, coalesce=True)
    _sched.start()
except Exception:  # noqa: BLE001 - Automatik ist optional
    _sched = None


# ── Routen ───────────────────────────────────────────────────────────────────
def _status():
    return _lade(STATUS_PATH, {}) or {}


def _li_laden():
    liste = _lade(LI_PATH, [])
    return liste if isinstance(liste, list) else []


def _li_speichern(text: str, thema: str = '') -> str:
    posts = _li_laden()
    jetzt = datetime.now()
    pid = secrets.token_hex(6)
    posts.insert(0, {'id': pid, 'ts': jetzt.isoformat(timespec='seconds'),
                     'datum': jetzt.strftime('%d.%m.%Y'), 'thema': (thema or '').strip(),
                     'text': (text or '').strip()})
    _sichere(LI_PATH, posts[:40])
    return pid


def _li_aktualisieren(pid: str, text: str) -> str:
    posts = _li_laden()
    for p in posts:
        if p.get('id') == pid:
            p['text'] = (text or '').strip()
            _sichere(LI_PATH, posts)
            return pid
    return _li_speichern(text)


def _li_loeschen(pid: str):
    _sichere(LI_PATH, [p for p in _li_laden() if p.get('id') != pid])


def _kopier_btn(elem_id: str, label: str = 'Kopieren') -> str:
    return ('<button class="btn ghost" type="button" onclick="'
            f"var t=document.getElementById('{elem_id}');t.select();document.execCommand('copy');"
            f'this.textContent=\'Kopiert &#10003;\'">{label}</button>')


def _linkedin_html(draft: str = '', thema: str = '', saved_id: str = '') -> str:
    ki_aktiv = bool(os.environ.get('ANTHROPIC_API_KEY'))
    aktiv = '' if ki_aktiv else ' disabled'
    kistat = ('' if ki_aktiv else
              '<div class="row" style="margin-top:2px"><span class="badge warn">Kein '
              '<code>ANTHROPIC_API_KEY</code> &ndash; Erstellung nicht möglich</span></div>')

    form = (
        '<div class="statusbox" style="margin-top:14px">'
        '<div class="step"><span class="ttl">Neuen Beitrag entwerfen</span></div>'
        '<p class="hint" style="margin:4px 0 0">Aufhänger/Kerngedanke (oder eigenes Thema). Claude '
        'formuliert einen fertigen Beitrag in deinem Ton mit bis zu 5 passenden Hashtags. Der Entwurf '
        'wird automatisch gespeichert &ndash; du bearbeitest, kopierst und postest selbst.</p>'
        '<form method="post" action="/linkedin">'
        '<textarea name="thema" rows="3" style="width:100%;margin-top:8px" '
        f'placeholder="z.B. PPWR ab 12.8. aus Lieferantensicht: wer trägt bei Dropshipping die Erzeugerpflicht?">{_esc(thema)}</textarea>'
        '<label style="display:flex;gap:8px;align-items:center;margin:8px 0;font-size:13px">'
        '<input type="checkbox" name="kontext" value="1" checked style="width:16px;height:16px;min-width:0"> '
        'Aktuellen Wochenüberblick als Faktengrundlage nutzen</label>'
        f'<div class="row"><button class="btn" type="submit"{aktiv}>Entwurf erstellen</button></div>'
        '</form></div>')

    ergebnis = ''
    if draft:
        ergebnis = (
            '<div class="statusbox" style="margin-top:14px">'
            '<div class="step"><span class="ttl">Entwurf (automatisch gespeichert)</span></div>'
            '<form method="post" action="/linkedin/speichern">'
            f'<input type="hidden" name="id" value="{_esc(saved_id)}">'
            f'<textarea id="lidraft" name="text" rows="14" style="width:100%;margin-top:8px">{_esc(draft)}</textarea>'
            f'<div class="row">{_kopier_btn("lidraft")} '
            '<button class="btn" type="submit">Bearbeitete Fassung speichern</button></div>'
            '</form></div>')

    posts = _li_laden()
    archiv = ''
    if posts:
        zeilen = ''
        for p in posts:
            pid = p.get('id') or ''
            thema_z = (f' &middot; <span style="color:var(--muted)">{_esc(p.get("thema"))}</span>'
                       if p.get('thema') else '')
            zeilen += (
                '<div class="statusbox" style="margin-top:12px">'
                f'<div class="hint" style="margin-bottom:6px">{_esc(p.get("datum") or "")}{thema_z}</div>'
                f'<textarea id="lp{_esc(pid)}" rows="8" style="width:100%">{_esc(p.get("text") or "")}</textarea>'
                f'<div class="row">{_kopier_btn("lp" + pid)} '
                f'<form method="post" action="/linkedin/{_esc(pid)}/loeschen" style="display:inline" '
                'onsubmit="return confirm(\'Beitrag löschen?\')">'
                '<button class="btn ghost" type="submit">Löschen</button></form></div></div>')
        archiv = ('<div class="step" style="margin-top:26px"><span class="ttl">Gespeicherte Beiträge '
                  f'({len(posts)})</span></div>' + zeilen)

    return (_platte('LinkedIn-Beiträge &ndash; entwerfen mit Hashtags, gespeichert')
            + _subtabs('linkedin') + kistat + form + ergebnis + archiv)


def _content_html(res=None, thema='', saved_id='', hinweis=''):
    ki_aktiv = bool(os.environ.get('ANTHROPIC_API_KEY'))
    aktiv = '' if ki_aktiv else ' disabled'
    kistat = ('' if ki_aktiv else
              '<div class="row" style="margin-top:2px"><span class="badge warn">Kein '
              '<code>ANTHROPIC_API_KEY</code> &ndash; Pipeline nicht möglich</span></div>')

    form = (
        '<div class="statusbox" style="margin-top:14px">'
        '<div class="step"><span class="ttl">Neuen Content erzeugen</span></div>'
        '<p class="hint" style="margin:4px 0 0">Thema/Aufhänger (oder leer lassen &ndash; dann wählt der '
        'Kurator selbst aus dem Wochenüberblick). Drei Agenten nacheinander: <b>Kurator</b> (Briefing) '
        '&rarr; <b>Texter</b> (LinkedIn + Instagram) &rarr; <b>Prüfer</b> (Ampel &amp; Hinweise). Freigabe '
        'und Posten bleiben bei dir.</p>'
        '<form method="post" action="/content">'
        '<textarea name="thema" rows="3" style="width:100%;margin-top:8px" '
        f'placeholder="z. B. PPWR aus Lieferantensicht bei Dropshipping – wer trägt die Erzeugerpflicht?">{_esc(thema)}</textarea>'
        '<label style="display:flex;gap:8px;align-items:center;margin:8px 0;font-size:13px">'
        '<input type="checkbox" name="kontext" value="1" checked style="width:16px;height:16px;min-width:0"> '
        'Aktuellen Wochenüberblick als Faktengrundlage nutzen</label>'
        f'<div class="row"><button class="btn" type="submit"{aktiv}>Pipeline starten</button> '
        '<a class="btn ghost" href="/content/profil">Profil „Über mich"</a> '
        '<a class="btn ghost" href="/content/vorlagen">Agenten-Vorlagen</a></div>'
        '<p class="hint" style="margin:8px 0 0">Dauert ~1 Minute (drei Modelldurchläufe). Für Bilder '
        'liefert die Instagram-Fassung ein fertiges Bild-Briefing (Nano Banana / Gemini).</p>'
        '</form></div>')

    ergebnis = ''
    if res:
        li = res.get('linkedin') or ''
        nl = res.get('newsletter') or ''
        ig = res.get('instagram') or ''
        ergebnis = (
            '<div class="statusbox" style="margin-top:14px">'
            '<div class="step"><span class="ttl">1 &middot; Briefing (Kurator)</span></div>'
            f'<div class="feed">{_md_html(res.get("brief") or "")}</div></div>'
            '<div class="statusbox laeuft" style="margin-top:14px">'
            '<div class="step"><span class="ttl">Prüfbericht (Prüfer)</span></div>'
            f'<div class="feed">{_md_html(res.get("pruef") or "")}</div>'
            '<p class="hint" style="margin:6px 0 0">Vorprüfung &ndash; die finale Freigabe machst du.</p></div>'
            '<form method="post" action="/content/speichern">'
            f'<input type="hidden" name="id" value="{_esc(saved_id)}">'
            '<div class="statusbox" style="margin-top:14px">'
            '<div class="step"><span class="ttl">LinkedIn-Post</span></div>'
            f'<textarea id="cli" name="linkedin" rows="12" style="width:100%;margin-top:8px">{_esc(li)}</textarea>'
            f'<div class="row">{_kopier_btn("cli")}</div></div>'
            '<div class="statusbox" style="margin-top:14px">'
            '<div class="step"><span class="ttl">LinkedIn-Newsletter</span></div>'
            f'<textarea id="cnl" name="newsletter" rows="16" style="width:100%;margin-top:8px">{_esc(nl)}</textarea>'
            f'<div class="row">{_kopier_btn("cnl")}</div></div>'
            '<div class="statusbox" style="margin-top:14px">'
            '<div class="step"><span class="ttl">Instagram-Fassung (inkl. Bild-Briefing)</span></div>'
            f'<textarea id="cig" name="instagram" rows="12" style="width:100%;margin-top:8px">{_esc(ig)}</textarea>'
            f'<div class="row">{_kopier_btn("cig")} '
            '<button class="btn" type="submit">Bearbeitete Fassungen speichern</button></div></div>'
            '</form>')

    # Referenzfotos-Karte (Basis für die Bilderzeugung)
    gemini_aktiv = bool(os.environ.get('GEMINI_API_KEY'))
    refs = _ref_liste()
    hat_refs = bool(refs)
    gem_badge = ('<span class="badge ok">&#10003; Gemini verbunden</span>' if gemini_aktiv else
                 '<span class="badge warn">Kein <code>GEMINI_API_KEY</code> &ndash; Bildgenerierung aus</span>')
    thumbs = ''.join(
        '<div style="display:inline-block;text-align:center;margin:0 8px 8px 0">'
        f'<img src="/content/referenz/{_esc(n)}" alt="" style="height:74px;width:74px;object-fit:cover;'
        'border:1px solid var(--line);border-radius:8px;display:block">'
        f'<form method="post" action="/content/referenz/{_esc(n)}/loeschen" style="margin-top:3px" '
        'onsubmit="return confirm(\'Referenzfoto löschen?\')">'
        '<button class="btn ghost" style="padding:2px 8px;font-size:11px" type="submit">entfernen</button>'
        '</form></div>' for n in refs)
    referenz_karte = (
        '<div class="statusbox" style="margin-top:14px">'
        '<div class="step"><span class="ttl">Referenzfotos (dein Gesicht)</span></div>'
        f'<div class="row" style="margin:4px 0 6px">{gem_badge}</div>'
        '<p class="hint" style="margin:0 0 8px">Basis für die Bilderzeugung mit Nano Banana. 1–3 klare '
        'Fotos deines Gesichts genügen. Sie bleiben auf dem Server (nicht im Git) und verlassen ihn nur '
        'beim Bild-Aufruf an Google.</p>'
        + (f'<div style="margin-bottom:6px">{thumbs}</div>' if thumbs else
           '<p class="hint" style="margin:0 0 8px">Noch keine Referenzfotos.</p>')
        + '<form method="post" action="/content/referenz" enctype="multipart/form-data" class="row">'
        '<input type="file" name="fotos" accept="image/*" multiple>'
        '<button class="btn ghost" type="submit">Hochladen</button></form>'
        '<form method="post" action="/content/gemini-test" style="margin-top:8px">'
        '<button class="btn ghost" type="submit">Gemini-Verbindung testen</button></form></div>')

    # Stil-Referenzbilder (optional): geben Look/Komposition vor, nicht die Identität
    stil = _stil_liste()
    stil_thumbs = ''.join(
        '<div style="display:inline-block;text-align:center;margin:0 8px 8px 0">'
        f'<img src="/content/stil/{_esc(n)}" alt="" style="height:74px;width:74px;object-fit:cover;'
        'border:1px solid var(--line);border-radius:8px;display:block">'
        f'<form method="post" action="/content/stil/{_esc(n)}/loeschen" style="margin-top:3px" '
        'onsubmit="return confirm(\'Stil-Referenzbild löschen?\')">'
        '<button class="btn ghost" style="padding:2px 8px;font-size:11px" type="submit">entfernen</button>'
        '</form></div>' for n in stil)
    stil_karte = (
        '<div class="statusbox" style="margin-top:14px">'
        '<div class="step"><span class="ttl">Stil-Referenzbilder (optional)</span></div>'
        '<p class="hint" style="margin:0 0 8px">Beispielbilder in deinem gewünschten Look (z. B. deine '
        'bisherigen Beiträge). Sie geben Bildstil, Licht und Komposition vor &ndash; die Identität kommt '
        'weiter von deinen Gesichtsfotos. Bleiben auf dem Server, nicht im Git.</p>'
        + (f'<div style="margin-bottom:6px">{stil_thumbs}</div>' if stil_thumbs else
           '<p class="hint" style="margin:0 0 8px">Noch keine Stil-Referenzbilder.</p>')
        + '<form method="post" action="/content/stil" enctype="multipart/form-data" class="row">'
        '<input type="file" name="fotos" accept="image/*" multiple>'
        '<button class="btn ghost" type="submit">Hochladen</button></form></div>')

    posts = _content_laden()
    archiv = ''
    if posts:
        zeilen = ''
        for p in posts:
            cid = p.get('id') or ''
            thema_z = (f' &middot; <span style="color:var(--muted)">{_esc(p.get("thema"))}</span>'
                       if p.get('thema') else '')
            zeilen += (
                '<div class="statusbox" style="margin-top:12px">'
                f'<div class="hint" style="margin-bottom:6px">{_esc(p.get("datum") or "")}{thema_z}</div>'
                '<div class="hint" style="margin:4px 0 2px">LinkedIn-Post</div>'
                f'<textarea id="al{_esc(cid)}" rows="6" style="width:100%">{_esc(p.get("linkedin") or "")}</textarea>'
                f'<div class="row" style="margin:4px 0 8px">{_kopier_btn("al" + cid)}</div>'
                '<div class="hint" style="margin:4px 0 2px">LinkedIn-Newsletter</div>'
                f'<textarea id="an{_esc(cid)}" rows="8" style="width:100%">{_esc(p.get("newsletter") or "")}</textarea>'
                f'<div class="row" style="margin:4px 0 8px">{_kopier_btn("an" + cid)}</div>'
                '<div class="hint" style="margin:4px 0 2px">Instagram (inkl. Bild-Briefing)</div>'
                f'<textarea id="ai{_esc(cid)}" rows="6" style="width:100%">{_esc(p.get("instagram") or "")}</textarea>'
                f'<div class="row" style="margin-top:4px">{_kopier_btn("ai" + cid)}</div>'
                + _bild_block(p, cid, gemini_aktiv, hat_refs)
                + '<div class="row" style="margin-top:10px">'
                f'<form method="post" action="/content/{_esc(cid)}/loeschen" style="display:inline" '
                'onsubmit="return confirm(\'Eintrag löschen?\')">'
                '<button class="btn ghost" type="submit">Löschen</button></form></div></div>')
        archiv = ('<div class="step" style="margin-top:26px"><span class="ttl">Gespeicherte Inhalte '
                  f'({len(posts)})</span></div>' + zeilen)

    sicherung_karte = (
        '<div class="statusbox" style="margin-top:26px">'
        '<div class="step"><span class="ttl">Sicherung</span></div>'
        '<p class="hint" style="margin:4px 0 8px">Beiträge, Themen, Profil und Agenten-Vorlagen als Datei '
        'sichern oder wieder einspielen. (Zusätzlich sichert Sliplane das Volume täglich automatisch.)</p>'
        '<div class="row"><a class="btn ghost" href="/content/export.json">Backup herunterladen</a>'
        '<form method="post" action="/content/import" enctype="multipart/form-data" class="row" '
        "onsubmit=\"return confirm('Backup einspielen? Überschreibt die aktuellen Beiträge, Themen, "
        "Profil und Vorlagen.')\">"
        '<input type="file" name="datei" accept="application/json,.json">'
        '<button class="btn ghost" type="submit">Backup einspielen</button></form></div></div>')

    warn = (f'<div class="statusbox"><span class="badge warn">{hinweis}</span></div>' if hinweis else '')
    return (_platte('Content-Pipeline &ndash; Kurator &middot; Texter &middot; Prüfer, Freigabe durch dich')
            + _subtabs('content') + kistat + warn + form + referenz_karte + stil_karte + ergebnis + archiv
            + sicherung_karte)


def _themen_html(vorschlaege=None, fokus='', hinweis=''):
    ki_aktiv = bool(os.environ.get('ANTHROPIC_API_KEY'))
    aktiv = '' if ki_aktiv else ' disabled'
    kistat = ('' if ki_aktiv else
              '<div class="row" style="margin-top:2px"><span class="badge warn">Kein '
              '<code>ANTHROPIC_API_KEY</code> &ndash; Ideenvorschläge nicht möglich</span></div>')
    warn = (f'<div class="statusbox"><span class="badge warn">{hinweis}</span></div>' if hinweis else '')

    gen = (
        '<div class="statusbox" style="margin-top:14px">'
        '<div class="step"><span class="ttl">Ideen vorschlagen lassen</span></div>'
        '<p class="hint" style="margin:4px 0 0">Die KI schlägt aus deinem <a href="/content/profil">Profil</a> '
        'konkrete Themen vor. Optional eine Richtung vorgeben (z. B. „Lieferzeit", „Ehrenamt", „20 Jahre '
        'Skyport"). Gute Ideen übernimmst du in den Speicher.</p>'
        '<form method="post" action="/themen/ideen">'
        f'<textarea name="fokus" rows="2" style="width:100%;margin-top:8px" '
        f'placeholder="optional: Fokus / Wunschrichtung">{_esc(fokus)}</textarea>'
        f'<div class="row" style="margin-top:6px"><button class="btn" type="submit"{aktiv}>Ideen vorschlagen</button></div>'
        '</form></div>')

    vorschau = ''
    if vorschlaege:
        zeilen = ''
        for idee in vorschlaege:
            zeilen += (
                '<div class="row" style="align-items:flex-start;gap:8px;border-bottom:1px solid var(--line);padding:8px 0">'
                '<label style="display:flex;gap:8px;flex:1;align-items:flex-start;cursor:pointer">'
                f'<input type="checkbox" name="titel" value="{_esc(idee)}" class="ideachk" '
                'style="width:16px;height:16px;min-width:0;margin-top:2px">'
                f'<span>{_esc(idee)}</span></label>'
                f'<a class="btn ghost" href="/content?thema={quote(idee)}">&rarr; Beitrag</a></div>')
        selall = ('<label style="display:inline-flex;gap:8px;align-items:center;margin-bottom:8px;cursor:pointer">'
                  '<input type="checkbox" style="width:16px;height:16px;min-width:0" '
                  "onclick=\"for(const c of document.getElementsByClassName('ideachk'))c.checked=this.checked\">"
                  ' <span class="hint">Alle auswählen</span></label>')
        vorschau = ('<div class="statusbox" style="margin-top:14px">'
                    f'<div class="step"><span class="ttl">Vorschläge ({len(vorschlaege)})</span></div>'
                    '<p class="hint" style="margin:4px 0 8px">Kreuze alle an, die du super findest, und '
                    'übernimm sie zusammen in den Speicher.</p>'
                    '<form method="post" action="/themen/add">'
                    + selall + zeilen
                    + '<div class="row" style="margin-top:10px">'
                    '<button class="btn" type="submit">Ausgewählte in Speicher</button></div>'
                    '</form></div>')

    manuell = (
        '<div class="statusbox" style="margin-top:14px">'
        '<div class="step"><span class="ttl">Eigene Idee ablegen</span></div>'
        '<form method="post" action="/themen/add">'
        '<input type="text" name="titel" placeholder="Thema / Aufhänger" style="width:100%;margin-top:8px">'
        '<textarea name="notiz" rows="2" style="width:100%;margin-top:6px" placeholder="Notiz / Link (optional)"></textarea>'
        '<div class="row" style="margin-top:6px"><button class="btn ghost" type="submit">Ablegen</button></div>'
        '</form></div>')

    themen = _themen_laden()
    if themen:
        zeilen = ''
        for t in themen:
            tid = t.get('id') or ''
            notiz = (f'<div class="hint" style="margin-top:2px">{_esc(t.get("notiz"))}</div>'
                     if t.get('notiz') else '')
            zeilen += (
                '<div class="statusbox" style="margin-top:10px">'
                f'<div style="font-weight:600">{_esc(t.get("titel") or "")}</div>{notiz}'
                f'<div class="hint" style="margin-top:2px">{_esc(t.get("datum") or "")}</div>'
                '<div class="row" style="margin-top:6px">'
                f'<a class="btn ghost" href="/content?thema={quote(t.get("titel") or "")}">&rarr; Beitrag erstellen</a>'
                f'<form method="post" action="/themen/{_esc(tid)}/loeschen" style="display:inline" '
                'onsubmit="return confirm(\'Thema löschen?\')">'
                '<button class="btn ghost" type="submit">Löschen</button></form></div></div>')
        speicher = ('<div class="step" style="margin-top:26px"><span class="ttl">Themenspeicher '
                    f'({len(themen)})</span></div>' + zeilen)
    else:
        speicher = ('<div class="step" style="margin-top:26px"><span class="ttl">Themenspeicher</span></div>'
                    '<p class="hint">Noch keine Themen abgelegt.</p>')

    return (_platte('Themen &ndash; Ideengenerator &amp; Themenspeicher, speist den Kurator')
            + _subtabs('themen') + kistat + warn + gen + vorschau + manuell + speicher)


def _startseite_html():
    st = _status()
    briefings = _lade(BRIEF_PATH, []) or []
    ki_aktiv = bool(os.environ.get('ANTHROPIC_API_KEY'))

    if ki_aktiv:
        kistat = '<span class="badge ok">&#10003; API verbunden</span>'
    else:
        kistat = ('<span class="badge warn">Kein <code>ANTHROPIC_API_KEY</code> &ndash; '
                  'Erstellung nicht möglich</span>')

    auto_an = _automatik_an()
    if auto_an:
        auto_ctrl = (
            '<span class="badge ok" style="align-self:center">&#10003; Automatik an &middot; Montag 07:00</span> '
            '<form method="post" action="/automatik" style="display:inline">'
            '<input type="hidden" name="an" value="0">'
            '<button class="btn ghost" type="submit">Automatik ausschalten</button></form>')
    else:
        auto_ctrl = (
            '<span class="badge off" style="align-self:center">Automatik aus &middot; kein Montagslauf</span> '
            '<form method="post" action="/automatik" style="display:inline">'
            '<input type="hidden" name="an" value="1">'
            '<button class="btn" type="submit">Automatik einschalten</button></form>')

    laeuft = st.get('status') == 'laeuft'
    if laeuft:
        seit = (st.get('seit') or '').replace('T', ' ')[:16]
        banner = ('<div class="statusbox laeuft"><b>Der Überblick wird gerade erstellt&hellip;</b>'
                  f'<div class="hint" style="margin-top:4px">Gestartet {seit} &middot; '
                  'Claude recherchiert die letzten 7 Tage (dauert einige Minuten). '
                  'Diese Seite aktualisiert sich automatisch.</div></div>'
                  '<script>setTimeout(function(){location.reload()},12000)</script>')
        btn = '<button class="btn" disabled>Wird erstellt&hellip;</button>'
    else:
        if st.get('status') == 'fehler':
            banner = ('<div class="statusbox"><span class="badge err">Letzter Lauf fehlgeschlagen</span>'
                      f'<div class="hint" style="margin-top:6px">{_esc(st.get("meldung") or "")}</div></div>')
        elif st.get('status') == 'fertig':
            zeit = (st.get('zeit') or '').replace('T', ' ')[:16]
            banner = f'<div class="statusbox"><span class="badge ok">&#10003; Zuletzt erstellt {zeit}</span></div>'
        else:
            banner = '<div class="statusbox"><span class="hint">Noch kein Überblick erstellt.</span></div>'
        aktiv = '' if ki_aktiv else ' disabled'
        btn = (f'<form method="post" action="/erstellen" style="display:inline">'
               f'<button class="btn" type="submit"{aktiv}>Jetzt neu erstellen</button></form>')

    kopf = (_platte('Wöchentlicher Branchenüberblick, jeden Montag früh automatisch erzeugt &ndash; '
                    'konsistent im Aufbau, aktuell im Inhalt.')
            + _subtabs('ueberblick')
            + f'<div class="row" style="margin-top:2px">{kistat}</div>'
            + banner
            + f'<div class="row" style="margin-top:14px">{btn} '
            '<a class="btn ghost" href="/einstellungen">Vorlage bearbeiten</a> '
            + auto_ctrl + '</div>')

    if briefings:
        neu = briefings[0]
        feed = (f'<div class="feed" style="margin-top:26px">{_md_html(neu["md"])}</div>')
        if len(briefings) > 1:
            zeilen = ''.join(
                f'<a href="/b/{b["id"]}">{_esc(b.get("datum") or "")} &middot; Überblick ansehen</a>'
                for b in briefings[1:])
            feed += ('<h2 style="margin-top:34px">Frühere Ausgaben</h2>'
                     f'<div class="altliste">{zeilen}</div>')
    else:
        feed = ''

    return kopf + feed


def _esc(s):
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;'))


@app.get('/', response_class=HTMLResponse)
def start(request: Request):
    return HTMLResponse(_seite(_startseite_html(), request.state.user))


@app.post('/erstellen')
def erstellen(request: Request):
    if not _lock.locked():
        threading.Thread(target=_run_generation, args=('manuell',), daemon=True).start()
    return RedirectResponse('/', status_code=303)


@app.post('/automatik')
async def automatik_umschalten(request: Request):
    form = await request.form()
    _automatik_setzen((form.get('an') or '') == '1')
    return RedirectResponse('/', status_code=303)


@app.get('/linkedin', response_class=HTMLResponse)
def linkedin_seite(request: Request):
    return HTMLResponse(_seite(_linkedin_html(), request.state.user))


@app.post('/linkedin', response_class=HTMLResponse)
async def linkedin(request: Request):
    form = await request.form()
    thema = (form.get('thema') or '').strip()
    kontext = form.get('kontext') == '1'
    draft = ''
    saved_id = ''
    if thema:
        md = ''
        if kontext:
            bs = _lade(BRIEF_PATH, []) or []
            if bs:
                md = bs[0].get('md') or ''
        try:
            draft = await run_in_threadpool(_erzeuge_linkedin, thema, md)
            saved_id = _li_speichern(draft, thema)  # automatisch speichern
        except Exception as e:  # noqa: BLE001
            draft = 'Fehler bei der Erstellung: ' + str(e)[:300]
    return HTMLResponse(_seite(_linkedin_html(draft, thema, saved_id), request.state.user))


@app.post('/linkedin/speichern')
async def linkedin_speichern(request: Request):
    form = await request.form()
    pid = (form.get('id') or '').strip()
    text = (form.get('text') or '').strip()
    if text:
        _li_aktualisieren(pid, text) if pid else _li_speichern(text)
    return RedirectResponse('/linkedin', status_code=303)


@app.post('/linkedin/{lid}/loeschen')
def linkedin_loeschen(request: Request, lid: str):
    _li_loeschen(lid)
    return RedirectResponse('/linkedin', status_code=303)


# ── Content-Pipeline (3 Agenten) ─────────────────────────────────────────────
@app.get('/content', response_class=HTMLResponse)
def content_seite(request: Request, thema: str = ''):
    return HTMLResponse(_seite(_content_html(thema=thema), request.state.user))


@app.get('/themen', response_class=HTMLResponse)
def themen_seite(request: Request):
    return HTMLResponse(_seite(_themen_html(), request.state.user))


@app.post('/themen/ideen', response_class=HTMLResponse)
async def themen_ideen(request: Request):
    form = await request.form()
    fokus = (form.get('fokus') or '').strip()
    try:
        vorschlaege = await run_in_threadpool(_ideen_generieren, fokus)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(_seite(_themen_html(fokus=fokus,
                            hinweis='Ideensuche fehlgeschlagen: ' + str(e)[:200]), request.state.user))
    return HTMLResponse(_seite(_themen_html(vorschlaege, fokus), request.state.user))


@app.post('/themen/add')
async def themen_add(request: Request):
    form = await request.form()
    titels = [t for t in form.getlist('titel') if (t or '').strip()]
    notiz = (form.get('notiz') or '').strip()
    if len(titels) == 1:
        _thema_speichern(titels[0], notiz)   # manuelles Ablegen (mit optionaler Notiz)
    else:
        for t in titels:                     # Mehrfachauswahl aus den Vorschlägen
            _thema_speichern(t)
    return RedirectResponse('/themen', status_code=303)


@app.post('/themen/{tid}/loeschen')
def themen_loeschen(request: Request, tid: str):
    _thema_loeschen(tid)
    return RedirectResponse('/themen', status_code=303)


@app.post('/content', response_class=HTMLResponse)
async def content_run(request: Request):
    form = await request.form()
    thema = (form.get('thema') or '').strip()
    kontext = form.get('kontext') == '1'
    md = ''
    if kontext:
        bs = _lade(BRIEF_PATH, []) or []
        if bs:
            md = bs[0].get('md') or ''
    if not thema and not md:
        return HTMLResponse(_seite(_content_html(
            hinweis='Bitte ein Thema angeben oder den Wochenüberblick als Grundlage nutzen '
                    '(noch kein Überblick vorhanden).'), request.state.user))
    res = None
    saved_id = ''
    try:
        res = await run_in_threadpool(_content_pipeline, thema, md)
        eintrag = {'id': secrets.token_hex(6),
                   'ts': datetime.now().isoformat(timespec='seconds'),
                   'datum': datetime.now().strftime('%d.%m.%Y'),
                   'thema': thema, **res}
        saved_id = _content_speichern(eintrag)
    except Exception as e:  # noqa: BLE001
        res = {'brief': '', 'linkedin': '', 'instagram': '',
               'pruef': 'Fehler bei der Erstellung: ' + str(e)[:300]}
    return HTMLResponse(_seite(_content_html(res, thema, saved_id), request.state.user))


@app.post('/content/speichern')
async def content_speichern(request: Request):
    form = await request.form()
    cid = (form.get('id') or '').strip()
    li = (form.get('linkedin') or '').strip()
    nl = (form.get('newsletter') or '').strip()
    ig = (form.get('instagram') or '').strip()
    if cid:
        _content_aktualisieren(cid, li, nl, ig)
    return RedirectResponse('/content', status_code=303)


@app.get('/content/vorlagen', response_class=HTMLResponse)
def content_vorlagen(request: Request, ok: str = '', reset: str = ''):
    if reset:
        _sichere(CONTENT_SPEC_PATH, {})
        return RedirectResponse('/content/vorlagen?ok=1', status_code=303)
    p = _content_prompts()
    hinweis = '<p class="msg-ok">Vorlagen gespeichert.</p>' if ok else ''

    def feld(key, titel):
        return (f'<div class="step" style="margin-top:16px"><span class="ttl">{titel}</span></div>'
                f'<textarea name="{key}" rows="12" style="width:100%;font-size:13px;'
                f'font-family:var(--mono)">{_esc(p[key])}</textarea>')

    inhalt = (_platte('Agenten-Vorlagen bearbeiten &ndash; Kurator, Texter, Prüfer')
              + '<p style="margin-top:12px"><a href="/content">&larr; Zur Content-Pipeline</a></p>'
              + hinweis
              + '<p class="hint">Die drei System-Prompts der Pipeline. Leeres Feld + Speichern = eingebaute '
              'Standard-Vorlage. Änderungen wirken ab dem nächsten Lauf.</p>'
              '<form method="post" action="/content/vorlagen">'
              + feld('kurator', '1 · Kurator (Briefing)')
              + feld('texter', '2 · Texter (LinkedIn + Instagram)')
              + feld('pruefer', '3 · Prüfer (Ampel & Hinweise)')
              + feld('bildprompt', '4 · Bild-Prompt-Designer (Nano Banana)')
              + feld('ideen', '5 · Themen-Ideengeber')
              + '<div class="row" style="margin-top:10px">'
              '<button class="btn" type="submit">Speichern</button> '
              '<a class="btn ghost" href="/content/vorlagen?reset=1">Auf Standard zurücksetzen</a>'
              '</div></form>')
    return HTMLResponse(_seite(inhalt, request.state.user))


@app.post('/content/vorlagen')
async def content_vorlagen_speichern(request: Request):
    form = await request.form()
    d = {k: (form.get(k) or '').strip() for k in ('kurator', 'texter', 'pruefer', 'bildprompt', 'ideen')}
    _sichere(CONTENT_SPEC_PATH, {k: v for k, v in d.items() if v})  # leer -> Standard
    return RedirectResponse('/content/vorlagen?ok=1', status_code=303)


@app.get('/content/export.json')
def content_export(request: Request):
    data = {'exportiert': datetime.now().isoformat(timespec='seconds'),
            'profil': _lade(PROFIL_PATH, {}),
            'prompts': _lade(CONTENT_SPEC_PATH, {}),
            'themen': _themen_laden(),
            'content': _content_laden()}
    body = json.dumps(data, ensure_ascii=False, indent=2)
    fname = 'branchenradar-backup-' + datetime.now().strftime('%Y%m%d-%H%M') + '.json'
    return Response(body, media_type='application/json; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})


@app.post('/content/import', response_class=HTMLResponse)
async def content_import(request: Request):
    form = await request.form()
    f = form.get('datei')
    if not getattr(f, 'filename', ''):
        return RedirectResponse('/content', status_code=303)
    try:
        data = json.loads((await f.read()).decode('utf-8'))
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(_seite(_content_html(hinweis='Import fehlgeschlagen: ' + str(e)[:200]),
                                   request.state.user))
    if isinstance(data.get('content'), list):
        _sichere(CONTENT_PATH, data['content'])
    if isinstance(data.get('themen'), list):
        _sichere(TOPICS_PATH, data['themen'])
    if isinstance(data.get('profil'), dict):
        _sichere(PROFIL_PATH, data['profil'])
    if isinstance(data.get('prompts'), dict):
        _sichere(CONTENT_SPEC_PATH, data['prompts'])
    return RedirectResponse('/content', status_code=303)


@app.get('/content/profil', response_class=HTMLResponse)
def content_profil(request: Request, ok: str = '', reset: str = ''):
    if reset:
        _sichere(PROFIL_PATH, {})
        return RedirectResponse('/content/profil?ok=1', status_code=303)
    hinweis = '<p class="msg-ok">Profil gespeichert.</p>' if ok else ''
    inhalt = (_platte('Profil „Über mich" &ndash; gemeinsame Grundlage aller Content-Agenten')
              + '<p style="margin-top:12px"><a href="/content">&larr; Zur Content-Pipeline</a></p>'
              + hinweis
              + '<p class="hint">Dieser Text wird jedem Agenten (Kurator, Texter, Prüfer, Bild-Prompt) und '
              'dem LinkedIn-Entwurf vorangestellt. Je konkreter (Zahlen und Fakten zu Skyport, deine '
              'Kernthemen und Haltung, was du bewusst NICHT sagst, 1–2 Stilproben aus echten Beiträgen), '
              'desto besser treffen die Ergebnisse. Leer speichern = eingebauter Standard.</p>'
              '<form method="post" action="/content/profil">'
              f'<textarea name="text" rows="20" style="width:100%;font-size:13px">{_esc(_profil())}</textarea>'
              '<div class="row" style="margin-top:10px">'
              '<button class="btn" type="submit">Speichern</button> '
              '<a class="btn ghost" href="/content/profil?reset=1">Auf Standard zurücksetzen</a>'
              '</div></form>')
    return HTMLResponse(_seite(inhalt, request.state.user))


@app.post('/content/profil')
async def content_profil_speichern(request: Request):
    form = await request.form()
    text = (form.get('text') or '').strip()
    _sichere(PROFIL_PATH, {'text': text, 'geaendert': datetime.now().isoformat(timespec='seconds')}
             if text else {})
    return RedirectResponse('/content/profil?ok=1', status_code=303)


@app.post('/content/referenz')
async def content_referenz_upload(request: Request):
    form = await request.form()
    for f in form.getlist('fotos'):
        if not getattr(f, 'filename', ''):
            continue
        daten = await f.read()
        if not daten:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in _BILD_SUFFIXE:
            ext = '.jpg'
        _sichere_bytes(REFS_DIR / (secrets.token_hex(6) + ext), daten)
    return RedirectResponse('/content', status_code=303)


@app.get('/content/referenz/{name}')
def content_referenz_datei(request: Request, name: str):
    p = REFS_DIR / _sicherer_name(name)
    if p.is_file():
        return FileResponse(str(p))
    return RedirectResponse('/content', status_code=303)


@app.post('/content/referenz/{name}/loeschen')
def content_referenz_loeschen(request: Request, name: str):
    p = REFS_DIR / _sicherer_name(name)
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    return RedirectResponse('/content', status_code=303)


@app.post('/content/stil')
async def content_stil_upload(request: Request):
    form = await request.form()
    for f in form.getlist('fotos'):
        if not getattr(f, 'filename', ''):
            continue
        daten = await f.read()
        if not daten:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in _BILD_SUFFIXE:
            ext = '.jpg'
        _sichere_bytes(STIL_DIR / (secrets.token_hex(6) + ext), daten)
    return RedirectResponse('/content', status_code=303)


@app.get('/content/stil/{name}')
def content_stil_datei(request: Request, name: str):
    p = STIL_DIR / _sicherer_name(name)
    if p.is_file():
        return FileResponse(str(p))
    return RedirectResponse('/content', status_code=303)


@app.post('/content/stil/{name}/loeschen')
def content_stil_loeschen(request: Request, name: str):
    p = STIL_DIR / _sicherer_name(name)
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    return RedirectResponse('/content', status_code=303)


@app.get('/content/bild/{name}')
def content_bild_datei(request: Request, name: str):
    p = BILDER_DIR / _sicherer_name(name)
    if p.is_file():
        return FileResponse(str(p))
    return RedirectResponse('/content', status_code=303)


@app.post('/content/gemini-test', response_class=HTMLResponse)
async def content_gemini_test(request: Request):
    msg = await run_in_threadpool(_gemini_selftest)
    hinweis = ('✓ Gemini-Verbindung OK – Bilderzeugung funktioniert.' if msg == 'OK'
               else 'Gemini-Test: ' + msg)
    return HTMLResponse(_seite(_content_html(hinweis=hinweis), request.state.user))


@app.post('/content/{cid}/bildprompt')
async def content_bildprompt_speichern(request: Request, cid: str):
    form = await request.form()
    txt = (form.get('bildprompt') or '').strip()
    liste = _content_laden()
    for e in liste:
        if e.get('id') == cid:
            e['bildprompt'] = txt
            _sichere(CONTENT_PATH, liste)
            break
    return RedirectResponse('/content', status_code=303)


@app.post('/content/{cid}/bilder3', response_class=HTMLResponse)
async def content_bilder3(request: Request, cid: str):
    try:
        await run_in_threadpool(_bilder_set_erzeugen, cid, 3)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(_seite(_content_html(hinweis='Varianten konnten nicht erzeugt werden: '
                                                 + str(e)[:300]), request.state.user))
    return RedirectResponse('/content', status_code=303)


@app.post('/content/{cid}/bild', response_class=HTMLResponse)
async def content_bild_erzeugen(request: Request, cid: str):
    form = await request.form()
    txt = (form.get('bildprompt') or '').strip()
    if txt:                                  # aktuellen Feldinhalt vor der Erzeugung sichern
        liste = _content_laden()
        for e in liste:
            if e.get('id') == cid:
                e['bildprompt'] = txt
                _sichere(CONTENT_PATH, liste)
                break
    try:
        await run_in_threadpool(_bild_fuer_content, cid)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(_seite(_content_html(hinweis='Bild konnte nicht erzeugt werden: '
                                                 + str(e)[:300]), request.state.user))
    return RedirectResponse('/content', status_code=303)


@app.post('/content/{cid}/loeschen')
def content_loeschen(request: Request, cid: str):
    _content_loeschen(cid)
    return RedirectResponse('/content', status_code=303)


@app.get('/b/{bid}', response_class=HTMLResponse)
def briefing_ansehen(request: Request, bid: str):
    briefings = _lade(BRIEF_PATH, []) or []
    b = next((x for x in briefings if x.get('id') == bid), None)
    if not b:
        return RedirectResponse('/', status_code=303)
    inhalt = (_platte(f'Ausgabe vom {_esc(b.get("datum") or "")}')
              + '<p style="margin-top:12px"><a href="/">&larr; Zur aktuellen Ausgabe</a></p>'
              + f'<div class="feed" style="margin-top:16px">{_md_html(b["md"])}</div>')
    return HTMLResponse(_seite(inhalt, request.state.user))


@app.get('/einstellungen', response_class=HTMLResponse)
def einstellungen(request: Request, ok: str = '', reset: str = ''):
    if reset:
        _sichere(SPEC_PATH, {})
        return RedirectResponse('/einstellungen?ok=1', status_code=303)
    spec = _spec()
    hinweis = '<p class="msg-ok">Vorlage gespeichert.</p>' if ok else ''
    inhalt = (_platte('Vorlage bearbeiten &ndash; Persona, Recherchefelder, Regeln, Format und Ton')
              + '<p style="margin-top:12px"><a href="/">&larr; Zurück</a></p>'
              + hinweis
              + '<p class="hint">Das ist der feste Prompt, mit dem jeder Überblick erzeugt wird. '
              'Änderungen wirken ab dem nächsten Lauf. Setzt du das Feld leer und speicherst, '
              'wird die eingebaute Standard-Vorlage wieder verwendet.</p>'
              '<form method="post" action="/einstellungen">'
              f'<textarea name="spec" rows="26" style="width:100%;font-size:13px;'
              f'font-family:var(--mono)">{_esc(spec)}</textarea>'
              '<div class="row" style="margin-top:10px">'
              '<button class="btn" type="submit">Speichern</button> '
              '<a class="btn ghost" href="/einstellungen?reset=1">Auf Standard zurücksetzen</a>'
              '</div></form>')
    return HTMLResponse(_seite(inhalt, request.state.user))


@app.post('/einstellungen')
async def einstellungen_speichern(request: Request):
    form = await request.form()
    text = (form.get('spec') or '').strip()
    if text:
        _sichere(SPEC_PATH, {'text': text,
                             'geaendert': datetime.now().isoformat(timespec='seconds')})
    else:
        _sichere(SPEC_PATH, {})  # leer → Standard
    return RedirectResponse('/einstellungen?ok=1', status_code=303)
