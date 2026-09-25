"""Branchenradar – wöchentlicher Branchenüberblick "Möbel & Onlinehandel".

Ein Werkbank-Modul, das jeden Montag früh (und auf Knopfdruck) über die
Anthropic-API mit dem serverseitigen Web-Such-Werkzeug einen konsistenten
Newsfeed erzeugt. Der komplette Spec-Prompt (Persona, Recherchefelder,
Sorgfaltsregeln, Ausgabeformat, Ton) ist fest verdrahtet und editierbar,
damit die Ergebnisse Woche für Woche gleich aufgebaut sind – nur der Inhalt
ist neu, weil die Nachrichten neu sind.

Nur für die Rolle "admin" (kostet pro Erstellung Anthropic-Guthaben).
"""

import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

import markdown as _md
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
AUDIT_DB = DATA_DIR / 'audit.db'

MODELL = os.environ.get('BRANCHENRADAR_MODELL', 'claude-opus-5')
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
    with client.messages.stream(model=MODELL, max_tokens=8000, system=LINKEDIN_SYS,
                                messages=[{'role': 'user', 'content': user}]) as stream:
        resp = stream.get_final_message()
    return ''.join(getattr(b, 'text', '') for b in resp.content
                   if getattr(b, 'type', None) == 'text').strip()


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
