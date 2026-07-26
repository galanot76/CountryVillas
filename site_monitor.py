#!/usr/bin/env python3
"""
Monitor de salud web para countryvillaslanzarote.com
======================================================

Arquitectura (100% gratis, sin servidor propio):
  - GitHub Actions: hace de "reloj" (cron cada 2h) y de máquina donde
    corre el script (repo público = minutos ilimitados).
  - Supabase: guarda el estado entre ejecuciones (una tabla con una fila).
  - Resend: envía el correo de aviso.

Qué hace el script en sí (sin cambios de antes):
  1. Rastrea el sitio siguiendo únicamente enlaces internos.
  2. Detecta enlaces rotos (4xx, 5xx, timeouts) con reintentos.
  3. Detecta páginas huérfanas (histórico, no hay sitemap.xml en el sitio).
  4. Comprueba las fichas críticas de reserva (HTTP + estructura + widget
     real con navegador headless si Playwright está disponible).
  5. Compara con el estado anterior (en Supabase) y SOLO envía correo
     (vía Resend) si hay novedades.

Ver README.md para la puesta en marcha completa paso a paso.
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag
from collections import deque
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HEADLESS_AVAILABLE = True
except ImportError:
    HEADLESS_AVAILABLE = False

# ----------------------------- CONFIG -----------------------------------

START_URL = "https://www.countryvillaslanzarote.com/"
DOMAIN = "www.countryvillaslanzarote.com"
MAX_PAGES = int(os.environ.get("SITE_MONITOR_MAX_PAGES", 150))
REQUEST_TIMEOUT = 12
RETRY_COUNT = 2          # reintentos antes de dar un enlace por roto
RETRY_DELAY = 4          # segundos entre reintentos
REQUEST_DELAY = 0.3      # pausa entre peticiones (cortesía con el servidor)

# --- Control de la explosión combinatoria de URLs ------------------------
# El sitio genera varias URLs distintas (idiomas + slugs de texto) para la
# MISMA propiedad, y páginas de listado por cada combinación de zona/etiqueta.
# Verificado con datos reales: 50 alojamientos reales generaron 2.530 URLs
# rastreadas en la primera ejecución. Para evitarlo:
#  - Cada propiedad se identifica por su número de referencia final en la
#    URL (ej. "...-238563.html" -> "238563"); solo se sigue/comprueba UNA
#    URL por cada número de referencia, la primera que se encuentre.
#  - Las páginas de listado (por etiqueta, por zona, "list-view") se siguen
#    para descubrir propiedades nuevas, pero solo hasta un límite, porque
#    a partir de unas pocas ya no aportan propiedades nuevas.
PROPERTY_ID_REGEX = re.compile(r'-(\d{5,7})\.html$')
# Independiente del idioma: las páginas de listado por zona siempre llevan
# un identificador "-dNNN" (ej. "-d880", "-d459458"), tanto en inglés
# ("rentals-arrecife-d880") como en español ("alquileres-arrecife-d880").
# Se añaden además palabras clave conocidas (inglés y español) como red de
# seguridad para listados por etiqueta, que no llevan ese identificador.
LISTING_HINT_REGEX = re.compile(
    r'-d\d+(/|$)|/tag-|list-view|/categoria-|vista-lista|/rentals/rentals-|'
    r'/rentals/holidays-rentals|/alquiler/alquileres-|/alquiler/alquiler-alquileres'
)
MAX_LISTING_PAGES = int(os.environ.get("SITE_MONITOR_MAX_LISTING_PAGES", 25))
# Extensiones de archivo que no son páginas (imágenes, documentos...) --
# no tiene sentido rastrearlas como si fueran contenido navegable.
NON_PAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".css", ".js",
    ".mp4", ".mov", ".woff", ".woff2", ".ttf",
)

# --- Supabase (estado persistente entre ejecuciones) ----------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL")            # ej: https://xxxx.supabase.co
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")    # service_role key (secreta)
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "site_monitor_state")

# --- Modo de ejecución: "completo" (rastreo entero) o "critico" (solo las
# fichas críticas + motor de reservas, pensado para correr más a menudo).
# Cada modo guarda su propio estado en Supabase para no pisarse entre sí.
MODE = os.environ.get("SITE_MONITOR_MODE", "completo").strip().lower()
if MODE not in ("completo", "critico"):
    MODE = "completo"
SUPABASE_ROW_KEY = f"countryvillaslanzarote_{MODE}"

# Si no hay Supabase configurado, cae de vuelta a un JSON local (útil para
# probar el script en tu propio ordenador antes de subirlo a GitHub).
STATE_FILE = os.environ.get("SITE_MONITOR_STATE_FILE", f"site_monitor_state_{MODE}.json")

# --- Resend (envío de email) ----------------------------------------------
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
EMAIL_FROM = os.environ.get("SITE_MONITOR_EMAIL_FROM", "onboarding@resend.dev")
EMAIL_TO = os.environ.get("SITE_MONITOR_EMAIL_TO")
# Varios destinatarios: separarlos por coma en el secret SITE_MONITOR_EMAIL_TO
# (ej. "andres@correo.com,tecnico@correo.com"). Aquí se trocean en una lista,
# que es lo que espera de verdad el campo "to" de la API de Resend.
EMAIL_TO_LIST = [addr.strip() for addr in EMAIL_TO.split(",")] if EMAIL_TO else []

# --- Páginas críticas (fichas reservables) -------------------------------
# Lista de URLs de producto/reserva que se comprueban SIEMPRE, en cada
# ejecución, con prioridad -- una caída aquí significa reservas perdidas.
# Se puede rellenar a mano aquí, o vía un fichero de texto (una URL por
# línea) apuntado por SITE_MONITOR_CRITICAL_PAGES_FILE.
CRITICAL_PAGES = [
    "https://www.countryvillaslanzarote.com/es/alquiler/apartamento-puerto-del-carmen-casa-duran-sea-views-puerto-del-carmen-lanzarote-686183.html",
]
_critical_file = os.environ.get("SITE_MONITOR_CRITICAL_PAGES_FILE")
if _critical_file and os.path.exists(_critical_file):
    with open(_critical_file, "r", encoding="utf-8") as f:
        CRITICAL_PAGES = [line.strip() for line in f if line.strip()]

# Marcadores que deben seguir presentes en el HTML de una ficha reservable.
# Si desaparecen, algo se rompió en la plantilla (no es JS, es estructura).
CRITICAL_PAGE_MARKERS = ['id="disponibilidadPrecio"', 'id="linkBotonReserva"']

# Dominio externo del que depende el motor de reservas (Avantio). Si este
# dominio falla, probablemente TODAS las fichas reservables están afectadas.
BOOKING_ENGINE_HEALTH_URL = "https://crs.avantio.com/default/js/jquery-3.4.1.min.js"

# --- Chequeo funcional con navegador real (headless), opcional -----------
# Requiere: pip install playwright && playwright install --with-deps chromium
# Si no está instalado, el script sigue funcionando con el chequeo HTTP+HTML
# de siempre, simplemente sin esta capa extra.
USE_HEADLESS_CHECK = os.environ.get("SITE_MONITOR_USE_HEADLESS", "1") == "1" and HEADLESS_AVAILABLE
HEADLESS_TIMEOUT_MS = 30000
HEADLESS_EXTRA_WAIT_MS = 3000
PRICE_REGEX = re.compile(r'\d[\d.,]*\s?€')
MIN_PRICE_MATCHES = 1        # al menos un precio real visible en la página
MIN_CALENDAR_CHARS = 100     # el bloque de calendario debe tener contenido real
AVANTIO_DOMAIN_HINT = "avantio"

USER_AGENT = "Mozilla/5.0 (compatible; CVLanzaroteHealthCheck/1.0)"

# --------------------------------------------------------------------------


def fetch_with_retry(session, url):
    """Devuelve (status_code_o_string_de_error, referrer_ok). Reintenta
    antes de dar un fallo por bueno, para no generar falsas alarmas."""
    last_error = None
    for attempt in range(RETRY_COUNT + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            return resp.status_code, resp
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)
    return f"ERROR_CONEXION: {last_error}", None


def crawl_site():
    """Rastrea el sitio en anchura (BFS) siguiendo solo enlaces internos,
    sin parámetros de búsqueda (para no caer en las combinaciones del
    motor de reservas Avantio)."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    visited = set()
    queue = deque([START_URL])
    referrers = {START_URL: None}
    page_status = {}     # url -> status (int o string de error)
    broken = []          # lista de dicts con detalle de enlaces rotos
    seen_property_ids = set()   # para no comprobar la misma propiedad varias veces
    listing_pages_queued = 0    # para no perseguir todas las combinaciones de zona/etiqueta

    while queue and len(visited) < MAX_PAGES:
        url = queue.popleft()
        url, _ = urldefrag(url)
        if url in visited:
            continue
        visited.add(url)

        status, resp = fetch_with_retry(session, url)
        page_status[url] = status

        if isinstance(status, int) and status >= 400:
            broken.append({"url": url, "status": status, "referrer": referrers.get(url)})
        elif isinstance(status, str):
            broken.append({"url": url, "status": status, "referrer": referrers.get(url)})

        if resp is not None and resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                full = urljoin(url, href)
                full, _ = urldefrag(full)
                parsed = urlparse(full)
                if parsed.netloc != DOMAIN:
                    continue
                if parsed.query:
                    # nos saltamos combinaciones de búsqueda/reserva
                    continue
                if parsed.path.lower().endswith(NON_PAGE_EXTENSIONS):
                    continue  # imágenes, PDFs, etc. -- no son páginas a comprobar

                prop_match = PROPERTY_ID_REGEX.search(parsed.path)
                if prop_match:
                    prop_id = prop_match.group(1)
                    if prop_id in seen_property_ids:
                        continue  # ya tenemos una URL para esta propiedad, no duplicar
                    seen_property_ids.add(prop_id)
                elif LISTING_HINT_REGEX.search(parsed.path):
                    if listing_pages_queued >= MAX_LISTING_PAGES:
                        continue  # ya hemos seguido bastantes páginas de listado
                    listing_pages_queued += 1

                if full not in visited and full not in queue:
                    queue.append(full)
                    referrers.setdefault(full, url)

        time.sleep(REQUEST_DELAY)

    linked_urls = set(referrers.keys())  # todo lo que hemos visto enlazado
    return {
        "linked_urls": linked_urls,
        "page_status": page_status,
        "broken": broken,
        "referrers": referrers,
        "visited_count": len(visited),
    }


def check_orphan_candidates(session, candidate_urls):
    """Para URLs que antes estaban enlazadas y ahora no: comprobamos si
    el servidor todavía las sirve (200). Si sí, son huérfanas de verdad
    (existen pero nadie las enlaza). Si no, simplemente ya no existen
    y no hace falta avisar de nada."""
    confirmed_orphans = []
    for url in candidate_urls:
        status, _ = fetch_with_retry(session, url)
        if status == 200:
            confirmed_orphans.append(url)
        time.sleep(REQUEST_DELAY)
    return confirmed_orphans


def check_critical_pages(session):
    """Comprueba las fichas reservables marcadas como críticas: estado HTTP
    + presencia de los marcadores estructurales de la ficha de reserva, y,
    si Playwright está disponible, además un chequeo funcional real con
    navegador headless (precio visible, calendario cargado, peticiones a
    Avantio sin fallos) -- esto último es lo único que detecta una caída
    "silenciosa" del widget de JS que el chequeo HTTP no vería."""
    results = []
    for url in CRITICAL_PAGES:
        status, resp = fetch_with_retry(session, url)
        entry = {"url": url, "status": status, "missing_markers": [], "widget_problems": []}
        if status == 200 and resp is not None:
            body = resp.text
            for marker in CRITICAL_PAGE_MARKERS:
                if marker not in body:
                    entry["missing_markers"].append(marker)
        results.append(entry)
        time.sleep(REQUEST_DELAY)

    if USE_HEADLESS_CHECK:
        urls_to_check = [r["url"] for r in results if r["status"] == 200]
        try:
            headless_results = check_critical_pages_headless(urls_to_check)
            for entry in results:
                hr = headless_results.get(entry["url"])
                if hr:
                    entry["widget_problems"] = hr["widget_problems"]
        except Exception as e:
            print(f"Aviso: el chequeo headless falló por completo ({e}). "
                  f"Se sigue solo con el chequeo HTTP/HTML.")
    elif not HEADLESS_AVAILABLE:
        print("Aviso: Playwright no está instalado -- solo se hace chequeo HTTP/HTML "
              "de las páginas críticas (no se verifica el widget de JS). "
              "Instalar con: pip install playwright && playwright install --with-deps chromium")

    return results


def check_booking_engine_dependency(session):
    """Comprueba si el dominio externo del motor de reservas (Avantio)
    responde. Si falla, es muy probable que afecte a TODAS las fichas
    reservables a la vez, no solo a una."""
    status, _ = fetch_with_retry(session, BOOKING_ENGINE_HEALTH_URL)
    return status


def check_critical_pages_headless(urls):
    """Abre cada ficha crítica en un navegador real (headless) y comprueba
    señales de que el widget de reserva de Avantio realmente funcionó:
      - hay precios en € visibles en la página (no vacíos)
      - el bloque de calendario tiene contenido real
      - las peticiones al dominio de Avantio no han fallado
    Esto es lo único que puede detectar una caída "silenciosa" del widget
    de JS que un chequeo HTTP normal no vería (la página seguiría dando 200)."""
    results = {}
    if not urls:
        return results

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for url in urls:
            problems = []
            avantio_requests = []
            avantio_failed = []
            page = browser.new_page(ignore_https_errors=True)

            def on_response(resp, _reqs=avantio_requests):
                if AVANTIO_DOMAIN_HINT in resp.url:
                    _reqs.append((resp.url, resp.status))

            def on_request_failed(req, _fails=avantio_failed):
                if AVANTIO_DOMAIN_HINT in req.url:
                    _fails.append((req.url, req.failure))

            page.on("response", on_response)
            page.on("requestfailed", on_request_failed)

            try:
                try:
                    page.goto(url, timeout=HEADLESS_TIMEOUT_MS, wait_until="networkidle")
                except Exception:
                    # si networkidle no llega a tiempo (scripts que hacen polling),
                    # probamos con una carga básica + espera fija
                    page.goto(url, timeout=HEADLESS_TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_timeout(HEADLESS_EXTRA_WAIT_MS)

                body_text = page.locator("body").inner_text()
                price_matches = PRICE_REGEX.findall(body_text)
                if len(price_matches) < MIN_PRICE_MATCHES:
                    problems.append("No se ven precios (€) reales en la página tras cargar el widget")

                if page.locator("#bloque_formato_calendarios").count() > 0:
                    cal_text = page.locator("#bloque_formato_calendarios").inner_text()
                    if len(cal_text.strip()) < MIN_CALENDAR_CHARS:
                        problems.append("El calendario de disponibilidad está vacío o no cargó")

                failed_avantio = [u for u, _ in avantio_failed]
                if failed_avantio:
                    problems.append(f"{len(failed_avantio)} petición(es) al motor de reservas (Avantio) fallaron")

                bad_status_avantio = [(u, s) for u, s in avantio_requests if s and s >= 400]
                if bad_status_avantio:
                    problems.append(f"{len(bad_status_avantio)} petición(es) a Avantio devolvieron error HTTP")

            except Exception as e:
                problems.append(f"No se pudo comprobar el widget con navegador: {e}")
            finally:
                page.close()

            results[url] = {
                "widget_problems": problems,
                "avantio_requests_count": len(avantio_requests),
            }
        browser.close()
    return results


def load_state():
    default = {"known_urls": [], "errors": {}, "reported_orphans": []}

    if SUPABASE_URL and SUPABASE_KEY:
        try:
            resp = requests.get(
                f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
                params={"id": f"eq.{SUPABASE_ROW_KEY}", "select": "data"},
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                },
                timeout=15,
            )
            resp.raise_for_status()
            rows = resp.json()
            if rows:
                return rows[0]["data"]
            return default
        except Exception as e:
            print(f"Aviso: no se pudo leer el estado de Supabase ({e}). "
                  f"Se asume que es la primera ejecución.")
            return default

    # Fallback local (sin Supabase configurado, útil para pruebas en local)
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_state(state):
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
                params={"on_conflict": "id"},
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json={"id": SUPABASE_ROW_KEY, "data": state},
                timeout=15,
            )
            resp.raise_for_status()
            return
        except Exception as e:
            print(f"ERROR: no se pudo guardar el estado en Supabase ({e}). "
                  f"Se guarda también en local como respaldo.")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def build_email_body(new_errors, resolved_errors, new_orphans, total_pages, is_first_run,
                      critical_results=None, booking_engine_status=None,
                      prev_critical_failing=None, only_critical=False):
    lines = []
    lines.append(f"Informe de salud web - countryvillaslanzarote.com")
    lines.append(f"Fecha: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if not only_critical:
        lines.append(f"Páginas internas rastreadas: {total_pages}")
    lines.append("")

    # --- sección de páginas críticas: se muestra siempre que haya problema ---
    critical_problems = []
    if critical_results:
        for r in critical_results:
            if r["status"] != 200 or r["missing_markers"] or r["widget_problems"]:
                critical_problems.append(r)

    if critical_problems:
        lines.append(f"🔴 FICHAS DE RESERVA CON PROBLEMAS AHORA MISMO ({len(critical_problems)}):")
        for r in critical_problems:
            lines.append(f"  - {r['url']}")
            if r["status"] != 200:
                lines.append(f"    Estado HTTP: {r['status']}")
            if r["missing_markers"]:
                lines.append(f"    Faltan elementos de la plantilla de reserva: {', '.join(r['missing_markers'])}")
            if r["widget_problems"]:
                lines.append(f"    Problemas del widget de reserva (detectados con navegador real):")
                for p in r["widget_problems"]:
                    lines.append(f"      · {p}")
        lines.append("")
    elif prev_critical_failing:
        lines.append("✓ Las fichas de reserva críticas que antes fallaban ya funcionan de nuevo.")
        lines.append("")

    if booking_engine_status is not None and booking_engine_status != 200:
        lines.append(f"🔴 ALERTA: el motor de reservas externo (Avantio) no responde bien (estado: {booking_engine_status}).")
        lines.append("   Esto puede estar afectando a TODAS las fichas reservables del sitio a la vez,")
        lines.append("   no solo a una. Revisar con Avantio / equipo técnico como prioridad.")
        lines.append("")

    if is_first_run:
        lines.append("Este es el primer rastreo: se ha creado la línea base.")
        lines.append("A partir de ahora solo recibirás avisos cuando haya cambios.")
        return "\n".join(lines)

    if new_errors:
        lines.append(f"⚠ ENLACES ROTOS NUEVOS ({len(new_errors)}):")
        for e in new_errors:
            lines.append(f"  - {e['url']}")
            lines.append(f"    Estado: {e['status']}")
            lines.append(f"    Enlazado desde: {e['referrer'] or '(página de inicio o desconocido)'}")
        lines.append("")

    if resolved_errors:
        lines.append(f"✓ Enlaces que antes fallaban y ya funcionan ({len(resolved_errors)}):")
        for url in resolved_errors:
            lines.append(f"  - {url}")
        lines.append("")

    if new_orphans:
        lines.append(f"⚠ PÁGINAS HUÉRFANAS NUEVAS ({len(new_orphans)}):")
        lines.append("(Existen y responden correctamente, pero ningún enlace")
        lines.append("interno del sitio lleva ya a ellas)")
        for url in new_orphans:
            lines.append(f"  - {url}")
        lines.append("")

    if not only_critical:
        if not new_errors and not resolved_errors and not new_orphans and not critical_problems:
            lines.append("Sin novedades desde el último rastreo.")

    lines.append("")
    lines.append("---")
    lines.append("Este correo se genera automáticamente. Puedes reenviarlo tal cual")
    lines.append("a tu equipo técnico para que revisen los puntos marcados con ⚠ o 🔴.")
    lines.append("")
    if USE_HEADLESS_CHECK:
        lines.append("Las fichas críticas se comprueban con navegador real (headless):")
        lines.append("se verifica que aparezcan precios reales, que el calendario de")
        lines.append("disponibilidad cargue con contenido, y que las peticiones al motor")
        lines.append("de reservas (Avantio) no fallen. Esto SÍ detecta caídas del widget")
        lines.append("de JavaScript, no solo caídas del servidor.")
    else:
        lines.append("Nota: las fichas de reserva se comprueban por código HTTP y por la")
        lines.append("presencia del bloque de precio/disponibilidad en el HTML. Esto NO")
        lines.append("garantiza que el widget de reserva (JavaScript) calcule bien el precio")
        lines.append("o la disponibilidad en el navegador. Para esa garantía adicional hace")
        lines.append("falta instalar Playwright (ver README) y se activará automáticamente.")
    return "\n".join(lines)


def send_email(subject, body):
    if not RESEND_API_KEY or not EMAIL_TO_LIST:
        print("Resend no configurado (faltan RESEND_API_KEY / SITE_MONITOR_EMAIL_TO). No se envía email.")
        print("--- CONTENIDO DEL INFORME ---")
        print(body)
        return

    html_body = "<pre style='font-family: monospace; white-space: pre-wrap;'>" + \
        body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</pre>"

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": EMAIL_FROM,
                "to": EMAIL_TO_LIST,
                "subject": subject,
                "html": html_body,
                "text": body,
            },
            timeout=15,
        )
        resp.raise_for_status()
        print(f"Email enviado a {', '.join(EMAIL_TO_LIST)} vía Resend.")
    except requests.exceptions.HTTPError as e:
        print(f"ERROR enviando email vía Resend: {e} -- respuesta: {resp.text}")
    except Exception as e:
        print(f"ERROR enviando email vía Resend: {e}")


def main_completo():
    """Rastreo completo del sitio: enlaces rotos + huérfanas + fichas
    críticas. Pensado para correr cada 6-12h (no hace falta más a menudo:
    un enlace roto no es una emergencia de minutos, y las huérfanas son
    un problema de días/semanas, no de horas)."""
    print(f"[{datetime.now().isoformat()}] Iniciando rastreo completo...")
    result = crawl_site()

    state = load_state()
    is_first_run = len(state["known_urls"]) == 0

    prev_known = set(state["known_urls"])
    prev_errors = state["errors"]  # dict url -> status (como string)
    prev_reported_orphans = set(state.get("reported_orphans", []))

    current_linked = result["linked_urls"]
    current_errors = {e["url"]: str(e["status"]) for e in result["broken"]}

    # --- diff de errores ---
    new_errors = [e for e in result["broken"] if e["url"] not in prev_errors]
    resolved_errors = [u for u in prev_errors if u not in current_errors]

    # --- candidatos a huérfanas: estaban enlazadas antes, ya no ---
    candidate_orphan_urls = prev_known - current_linked
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    confirmed_orphans = check_orphan_candidates(session, candidate_orphan_urls) if candidate_orphan_urls else []
    new_orphans = [u for u in confirmed_orphans if u not in prev_reported_orphans]

    # --- páginas críticas de reserva: también aquí, aunque su cadencia
    # principal de vigilancia es el modo "critico" ---
    critical_session = requests.Session()
    critical_session.headers.update({"User-Agent": USER_AGENT})
    critical_results = check_critical_pages(critical_session)
    booking_engine_status = check_booking_engine_dependency(critical_session)

    prev_critical_failing = set(state.get("critical_failing", []))
    current_critical_failing = {
        r["url"] for r in critical_results if r["status"] != 200 or r["missing_markers"] or r["widget_problems"]
    }

    critical_now_failing = bool(current_critical_failing)
    critical_recovered = bool(prev_critical_failing - current_critical_failing) and not critical_now_failing
    booking_engine_down = booking_engine_status != 200

    # --- construir y enviar email si procede ---
    if (is_first_run or new_errors or resolved_errors or new_orphans
            or critical_now_failing or critical_recovered or booking_engine_down):
        body = build_email_body(
            new_errors, resolved_errors, new_orphans,
            total_pages=result["visited_count"],
            is_first_run=is_first_run,
            critical_results=critical_results,
            booking_engine_status=booking_engine_status,
            prev_critical_failing=prev_critical_failing,
        )
        subject = "Country Villas Lanzarote - Informe de salud web"
        if critical_now_failing or booking_engine_down:
            subject = "🔴 Country Villas Lanzarote - FICHA(S) DE RESERVA CON PROBLEMAS"
        elif not is_first_run and (new_errors or new_orphans):
            subject = f"⚠ Country Villas Lanzarote - {len(new_errors)} error(es), {len(new_orphans)} huérfana(s)"
        send_email(subject, body)
    else:
        print("Sin novedades. No se envía email.")

    # --- guardar nuevo estado ---
    all_reported_orphans = prev_reported_orphans | set(confirmed_orphans)
    # si una huérfana vuelve a enlazarse, la quitamos de la lista para poder re-detectarla en el futuro
    all_reported_orphans = {u for u in all_reported_orphans if u in confirmed_orphans}

    new_state = {
        "known_urls": list(current_linked),
        "errors": current_errors,
        "reported_orphans": list(all_reported_orphans),
        "critical_failing": list(current_critical_failing),
        "last_run": datetime.now(timezone.utc).isoformat(),
    }
    save_state(new_state)
    print(f"[{datetime.now().isoformat()}] Rastreo completo terminado. "
          f"{len(current_linked)} URLs, {len(current_errors)} con error, "
          f"{len(confirmed_orphans)} huérfanas confirmadas, "
          f"{len(current_critical_failing)} fichas críticas con problemas.")


def main_critico():
    """Solo las fichas críticas de reserva + el motor de reservas externo
    (Avantio). No rastrea el resto del sitio -- pensado para correr cada
    30-60 min, porque aquí sí importa el tiempo de detección (una caída
    sin detectar son reservas perdidas), y es barato al ser una sola
    página (o pocas)."""
    print(f"[{datetime.now().isoformat()}] Iniciando chequeo de fichas críticas...")

    state = load_state()
    is_first_run = "critical_failing" not in state or state.get("last_run") is None

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    critical_results = check_critical_pages(session)
    booking_engine_status = check_booking_engine_dependency(session)

    prev_critical_failing = set(state.get("critical_failing", []))
    current_critical_failing = {
        r["url"] for r in critical_results if r["status"] != 200 or r["missing_markers"] or r["widget_problems"]
    }

    critical_now_failing = bool(current_critical_failing)
    critical_recovered = bool(prev_critical_failing - current_critical_failing) and not critical_now_failing
    booking_engine_down = booking_engine_status != 200
    booking_engine_recovered = state.get("booking_engine_down", False) and not booking_engine_down

    if (is_first_run or critical_now_failing or critical_recovered
            or booking_engine_down or booking_engine_recovered):
        body = build_email_body(
            new_errors=[], resolved_errors=[], new_orphans=[],
            total_pages=0,
            is_first_run=False,  # no queremos el mensaje de "línea base del rastreo completo" aquí
            critical_results=critical_results,
            booking_engine_status=booking_engine_status,
            prev_critical_failing=prev_critical_failing,
            only_critical=True,
        )
        if is_first_run and not critical_now_failing and not booking_engine_down:
            body = ("Chequeo de fichas críticas activado. Todo correcto por ahora.\n\n" + body)
        subject = "Country Villas Lanzarote - Chequeo de fichas críticas"
        if critical_now_failing or booking_engine_down:
            subject = "🔴 Country Villas Lanzarote - FICHA(S) DE RESERVA CON PROBLEMAS"
        elif critical_recovered or booking_engine_recovered:
            subject = "✓ Country Villas Lanzarote - Fichas críticas recuperadas"
        send_email(subject, body)
    else:
        print("Sin novedades en fichas críticas. No se envía email.")

    new_state = dict(state)  # conserva cualquier otra clave que ya tuviera esta fila
    new_state["critical_failing"] = list(current_critical_failing)
    new_state["booking_engine_down"] = booking_engine_down
    new_state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(new_state)
    print(f"[{datetime.now().isoformat()}] Chequeo de fichas críticas terminado. "
          f"{len(current_critical_failing)} con problemas. "
          f"Motor de reservas: {'CAÍDO' if booking_engine_down else 'OK'}.")


if __name__ == "__main__":
    if MODE == "critico":
        main_critico()
    else:
        main_completo()
