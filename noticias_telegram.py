#!/usr/bin/env python3
"""
Recopila noticias de los feeds RSS de medios guatemaltecos y las envía
a un chat de Telegram.

Uso:
  python3 noticias_telegram.py                  una ronda (para cron)
  python3 noticias_telegram.py --bucle          revisa cada N minutos sin parar
  python3 noticias_telegram.py --probar         revisa las fuentes, no envía nada
  python3 noticias_telegram.py --chat-id        muestra los chat_id que ve el bot
  python3 noticias_telegram.py --prueba-telegram  manda un mensaje de prueba
  python3 noticias_telegram.py --configurar     pide token y chat_id y llena config.ini
                                                (pensado para el iPhone, sin editor de texto)

Token y chat_id también se pueden dar con las variables de entorno
TELEGRAM_TOKEN y TELEGRAM_CHAT_ID.
"""

import argparse
import calendar
import configparser
import csv
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote_plus, urljoin

try:
    import feedparser
    import requests
except ImportError:
    sys.exit("Faltan librerías. Corre:  pip3 install feedparser requests")

BASE = Path(__file__).resolve().parent
ARCHIVO_CONFIG = BASE / "config.ini"
ARCHIVO_FUENTES = BASE / "fuentes.csv"
ARCHIVO_ESTADO = BASE / "estado.json"

SESION = requests.Session()
SESION.headers["User-Agent"] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) noticias-telegram/1.0"
)

MAX_VISTOS_POR_FUENTE = 400
MAX_ENLACES_GLOBALES = 4000


# ---------------------------------------------------------------- config

def cargar_config():
    if not ARCHIVO_CONFIG.exists():
        sys.exit(f"No encuentro {ARCHIVO_CONFIG.name}. Copia config.ini junto al script.")
    cp = configparser.ConfigParser()
    cp.read(ARCHIVO_CONFIG, encoding="utf-8")
    tg = cp["telegram"]
    op = cp["opciones"] if cp.has_section("opciones") else {}
    palabras = [p.strip() for p in op.get("palabras_clave", "").split(",") if p.strip()]
    # Las variables de entorno mandan sobre config.ini (así funciona en GitHub Actions)
    return {
        "token": os.environ.get("TELEGRAM_TOKEN") or tg.get("token", "").strip(),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id", "").strip(),
        "intervalo": int(op.get("intervalo_minutos", "10")),
        "max_por_fuente": int(op.get("max_por_fuente_por_ronda", "8")),
        "pausa": float(op.get("segundos_entre_mensajes", "3.5")),
        "vista_previa": op.get("vista_previa", "no").strip().lower() in ("si", "sí", "yes", "true", "1"),
        "palabras": [normalizar(p) for p in palabras],
        "solo_coyuntura": op.get("solo_coyuntura", "si").strip().lower() in ("si", "sí", "yes", "true", "1"),
        "alertas_tendencia": op.get("alertas_tendencia", "si").strip().lower() in ("si", "sí", "yes", "true", "1"),
        "umbral_tendencia": int(op.get("umbral_tendencia", "3")),
        "ventana_horas": float(op.get("ventana_tendencia_horas", "6")),
    }


def cargar_fuentes():
    if not ARCHIVO_FUENTES.exists():
        sys.exit(f"No encuentro {ARCHIVO_FUENTES.name}.")
    lineas = [
        l for l in ARCHIVO_FUENTES.read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.lstrip().startswith("#")
    ]
    fuentes = []
    for fila in csv.DictReader(lineas):
        nombre = (fila.get("nombre") or "").strip()
        if not nombre:
            continue
        fuentes.append({
            "nombre": nombre,
            "dominio": (fila.get("dominio") or "").strip().removeprefix("https://").removeprefix("http://").strip("/"),
            "feed": (fila.get("feed") or "").strip(),
            # opcional: nacional / internacional; vacío = decidir por el contenido
            "ambito": normalizar((fila.get("ambito") or "").strip()),
        })
    return fuentes


def cargar_estado():
    if ARCHIVO_ESTADO.exists():
        try:
            return json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("estado.json dañado; empiezo de cero.")
    return {"feeds": {}, "vistos": {}, "enviados": []}


def guardar_estado(estado):
    tmp = ARCHIVO_ESTADO.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ARCHIVO_ESTADO)


def normalizar(texto):
    texto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------- feeds

def url_google_news(consulta):
    return ("https://news.google.com/rss/search?q=" + quote_plus(consulta)
            + "&hl=es-419&gl=GT&ceid=GT:es-419")


def leer_feed(url):
    """Devuelve el feed parseado si tiene entradas, si no None."""
    try:
        r = SESION.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return None
    d = feedparser.parse(r.content)
    return d if d.entries else None


def descubrir_en_portada(dominio):
    """Busca <link rel="alternate" type="application/rss+xml"> en la portada."""
    for base in (f"https://{dominio}", f"https://www.{dominio}"):
        try:
            r = SESION.get(base, timeout=20)
            r.raise_for_status()
        except requests.RequestException:
            continue
        for tag in re.findall(r"<link[^>]+>", r.text, flags=re.I):
            if re.search(r"application/(rss|atom)\+xml", tag, re.I):
                m = re.search(r'href=["\']([^"\']+)', tag, re.I)
                if m:
                    yield urljoin(r.url, html.unescape(m.group(1)))
        return


def resolver_feed(fuente):
    """Encuentra un feed que funcione. Devuelve (url, tipo) o (None, None)."""
    feed, dom = fuente["feed"], fuente["dominio"]

    if feed.startswith("google:"):
        url = url_google_news(feed[len("google:"):].strip())
        return (url, "google") if leer_feed(url) else (None, None)

    candidatos = [feed] if feed else []
    if dom:
        sin_www = dom.removeprefix("www.")
        for d in (sin_www, "www." + sin_www):
            candidatos += [f"https://{d}/feed/", f"https://{d}/rss", f"https://{d}/rss.xml"]
    for url in dict.fromkeys(candidatos):
        if leer_feed(url):
            return url, "rss"

    if dom:
        for url in descubrir_en_portada(dom.removeprefix("www.")):
            if leer_feed(url):
                return url, "rss"
        url = url_google_news(f"site:{dom.removeprefix('www.')}")
        if leer_feed(url):
            return url, "google"
    return None, None


def id_entrada(e):
    return e.get("id") or e.get("link") or e.get("title", "")


def fecha_entrada(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return calendar.timegm(t) if t else 0   # feedparser entrega la fecha en UTC


# ---------------------------------------------------------------- telegram

def enviar(cfg, texto):
    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    datos = {
        "chat_id": cfg["chat_id"],
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": not cfg["vista_previa"],
    }
    for _ in range(4):
        try:
            r = SESION.post(url, data=datos, timeout=20)
        except requests.RequestException as err:
            print(f"  Error de red con Telegram: {err}")
            time.sleep(5)
            continue
        if r.status_code == 429:
            espera = r.json().get("parameters", {}).get("retry_after", 30)
            print(f"  Telegram pide esperar {espera} s")
            time.sleep(espera + 1)
            continue
        if not r.ok:
            print(f"  Telegram rechazó el mensaje: {r.text[:200]}")
            return False
        return True
    return False


def formatear(nombre, e, tipo):
    titulo = html.unescape(e.get("title", "(sin título)")).strip()
    if tipo == "google":
        # Google Noticias agrega " - Medio" al final del título
        titulo = re.sub(r"\s+-\s+[^-]{2,60}$", "", titulo)
    enlace = e.get("link", "")
    return (f"<b>{html.escape(nombre)}</b>\n"
            f"<a href=\"{html.escape(enlace, quote=True)}\">{html.escape(titulo)}</a>")


def pasa_filtro(cfg, e):
    if not cfg["palabras"]:
        return True
    texto = normalizar(e.get("title", "") + " " + e.get("summary", ""))
    return any(p in texto for p in cfg["palabras"])


# ---------------------------------------------------------------- clasificación
#
# Las listas de abajo son las de fábrica. Si existe clasificacion.ini junto al
# script, sus listas reemplazan a estas (así se ajustan desde GitHub sin tocar
# el código). Los términos se buscan al inicio de palabra, sin tildes:
# "diputad" encuentra diputado, diputada y diputados.

CLASIFICACION_BASE = {
    "excluir": """deporte, futbol, fútbol, liga nacional, selección nacional, gol, partido amistoso,
        mundial de clubes, champions, nba, nfl, béisbol, boxeo, tenis, atleta,
        farándula, espectáculo, celebridad, famoso, actriz, actor, cantante, concierto,
        telenovela, reality, serie de netflix, película, estreno, horóscopo, signo zodiacal,
        receta, cocina, moda, belleza, maquillaje, mascota, viral, tiktok, meme,
        lotería, sorteo, videojuego, gamer, iphone, android""",
    "coyuntura": """congreso, diputad, ley, iniciativa de ley, reforma, presupuesto, gobierno, presidente,
        presidencia, vicepresident, ministr, ministerio, secretar, gobernador, alcald, municipal,
        mp, ministerio público, fiscal, fiscalía, juez, jueza, juzgado, tribunal, corte, cc, csj,
        sentencia, juicio, audiencia, captura, detenid, orden de aprehensión, extradi, amparo,
        tse, elección, electoral, partido político, candidat, voto, corrupción, plaza fantasma,
        contralor, licitación, compra pública, impunidad, cicig, feci,
        protesta, manifesta, bloqueo, paro, huelga, sindicato, autoridades indígenas, 48 cantones,
        desalojo, conflictividad, comunidad, minería, hidroeléctrica, territorio,
        derechos humanos, pdh, libertad de prensa, criminaliza,
        migra, deportad, deportación, frontera, remesa, asilo, tps,
        economía, económic, inflación, precio, canasta básica, combustible, gasolina, salario,
        empleo, desempleo, banguat, banco de guatemala, impuesto, sat, arancel, exporta, importa,
        pobreza, desnutrición, hambre, salud, hospital, igss, mspas, dengue, epidemia, vacuna,
        educación, mineduc, maestro, magisterio,
        seguridad, violencia, homicidio, asesinat, ataque armado, pandilla, extorsi,
        narco, droga, crimen, cárcel, prisión, reos, pnc, policía, ejército, militar,
        conred, sismo, terremoto, volcán, erupción, lluvia, tormenta, huracán, inundación,
        deslave, emergencia, estado de calamidad, estado de sitio, sequía, incendio forestal,
        guerra, conflicto armado, bombardeo, misil, alto el fuego, sanción, onu, oea,
        cumbre, tratado, diplomátic, embajad, canciller, casa blanca, parlamento, golpe de estado""",
    "nacional": """guatemala, guatemalteco, guatemalteca, chapín, ciudad de guatemala, mixco, villa nueva,
        alta verapaz, baja verapaz, chimaltenango, chiquimula, el progreso, escuintla, huehuetenango,
        izabal, jalapa, jutiapa, petén, quetzaltenango, xela, quiché, retalhuleu, sacatepéquez,
        antigua guatemala, san marcos, santa rosa, sololá, suchitepéquez, totonicapán, zacapa,
        congreso de la república, mp, ministerio público, tse, igss, pnc, conred, banguat, insivumeh,
        mspas, mineduc, mingob, sat, pdh, cc, csj, 48 cantones""",
    "internacional": """estados unidos, ee. uu., eeuu, eua, washington, casa blanca, méxico, mexicano,
        el salvador, salvadoreñ, honduras, hondureñ, nicaragua, nicaragüense, costa rica, panamá,
        belice, cuba, haití, república dominicana, venezuela, colombia, ecuador, perú, bolivia, chile,
        argentina, brasil, uruguay, paraguay, canadá, españa, europa, unión europea, francia,
        alemania, reino unido, italia, rusia, ucrania, china, japón, corea, india, israel, gaza,
        palestin, irán, siria, líbano, onu, oea, otan, papa león, vaticano""",
    "secciones_coyuntura": "politica, justicia, economia, nacional, nacionales, comunitario, guatemala, seguridad, gobierno",
    "secciones_excluir": "deportes, deporte, futbol, escenario, entretenimiento, espectaculos, farandula, vida, estilo, viral, horoscopo, tecnologia, gastronomia, turismo",
    "secciones_internacional": "internacional, internacionales, mundo, world, america-latina, latinoamerica",
}

PALABRAS_VACIAS = set("""a al ante bajo con contra de del desde durante en entre hacia hasta para por segun sin
    sobre tras el la los las lo un una unos unas y o u e ni que se su sus le les es son fue ser ha han
    este esta estos estas ese esa eso como mas pero ya no si muy tambien hoy ayer manana dice dicen
    tras asi cual quien donde cuando todo todos toda todas otro otra nuevo nueva video fotos minuto
    guatemala guatemalteco guatemalteca gt noticia noticias""".split())


def _lista(texto):
    return [normalizar(t.strip()) for t in re.split(r"[,\n]", texto) if t.strip()]


def _patron(terminos):
    partes = []
    for t in terminos:
        t = re.escape(t)
        # Siglas cortas (mp, cc, sat) solo como palabra completa
        partes.append(rf"\b{t}\b" if len(t) <= 3 else rf"\b{t}")
    return re.compile("|".join(partes)) if partes else re.compile(r"(?!x)x")


def cargar_clasificacion():
    datos = dict(CLASIFICACION_BASE)
    archivo = BASE / "clasificacion.ini"
    if archivo.exists():
        cp = configparser.ConfigParser()
        cp.read(archivo, encoding="utf-8")
        for seccion in cp.sections():
            if "palabras" in cp[seccion] and seccion in datos:
                datos[seccion] = cp[seccion]["palabras"]
    patrones = {k: _patron(_lista(v)) for k, v in datos.items() if not k.startswith("secciones_")}
    secciones = {k: set(_lista(v)) for k, v in datos.items() if k.startswith("secciones_")}
    return patrones, secciones


def secciones_de(enlace):
    ruta = re.sub(r"^https?://[^/]+", "", enlace or "").lower()
    return set(p for p in re.split(r"[/?#]", ruta) if p)


def texto_de(e):
    resumen = re.sub(r"<[^>]+>", " ", e.get("summary", ""))[:400]
    return normalizar(html.unescape(e.get("title", "") + " " + resumen))


def es_coyuntura(e, clas):
    patrones, secciones = clas
    partes_url = secciones_de(e.get("link"))
    titulo = normalizar(html.unescape(e.get("title", "")))
    if partes_url & secciones["secciones_excluir"] or patrones["excluir"].search(titulo):
        return False
    if partes_url & secciones["secciones_coyuntura"]:
        return True
    return bool(patrones["coyuntura"].search(texto_de(e)))


def clasificar_ambito(e, fuente, clas):
    if fuente.get("ambito") in ("nacional", "internacional"):
        return fuente["ambito"]
    patrones, secciones = clas
    if secciones_de(e.get("link")) & secciones["secciones_internacional"]:
        return "internacional"
    texto = texto_de(e)
    if patrones["nacional"].search(texto):
        return "nacional"
    if patrones["internacional"].search(texto):
        return "internacional"
    return "nacional"   # las fuentes son guatemaltecas: por defecto, nacional


def palabras_clave_titulo(titulo):
    palabras = re.findall(r"[a-z0-9]+", normalizar(html.unescape(titulo or "")))
    return sorted({p for p in palabras if len(p) >= 4 and p not in PALABRAS_VACIAS})


def misma_noticia(a, b):
    a, b = set(a), set(b)
    if not a or not b:
        return False
    comunes = len(a & b)
    return comunes >= 4 or (comunes >= 2 and comunes / min(len(a), len(b)) >= 0.5)


ETIQUETAS = {"nacional": "🇬🇹 <b>Nacional</b>", "internacional": "🌎 <b>Internacional</b>"}


def formatear_clasificada(nombre, e, tipo, ambito):
    titulo = html.unescape(e.get("title", "(sin título)")).strip()
    if tipo == "google":
        titulo = re.sub(r"\s+-\s+[^-]{2,60}$", "", titulo)
    enlace = e.get("link", "")
    return (f"{ETIQUETAS[ambito]} · {html.escape(nombre)}\n"
            f"<a href=\"{html.escape(enlace, quote=True)}\">{html.escape(titulo)}</a>")


def revisar_tendencia(cfg, entrada, ventana, alertas):
    """Si varios medios distintos publican la misma noticia, manda una alerta (una sola vez)."""
    parecidas = [v for v in ventana if misma_noticia(v["tok"], entrada["tok"])]
    medios = list(dict.fromkeys(v["f"] for v in parecidas))
    if len(medios) < cfg["umbral_tendencia"]:
        return False
    if any(misma_noticia(a["tok"], entrada["tok"]) for a in alertas):
        return False
    alertas.append({"t": time.time(), "tok": entrada["tok"]})
    texto = (f"🔥 <b>TENDENCIA</b> · {ETIQUETAS[entrada['ambito']]} · {len(medios)} medios\n"
             f"<a href=\"{html.escape(entrada['link'], quote=True)}\">{html.escape(entrada['titulo'])}</a>\n"
             f"<i>{html.escape(', '.join(medios))}</i>")
    return enviar(cfg, texto)


# ---------------------------------------------------------------- rondas

def obtener_feed(fuente, estado):
    """Usa el feed guardado; si falla, lo vuelve a buscar."""
    guardado = estado["feeds"].get(fuente["nombre"])
    if guardado:
        d = leer_feed(guardado["url"])
        if d:
            return d, guardado["tipo"]
    url, tipo = resolver_feed(fuente)
    if not url:
        return None, None
    estado["feeds"][fuente["nombre"]] = {"url": url, "tipo": tipo}
    return leer_feed(url), tipo


def ronda(cfg, fuentes, estado):
    clas = cargar_clasificacion()
    enviados_set = set(estado["enviados"])
    limite = time.time() - cfg["ventana_horas"] * 3600
    ventana = [v for v in estado.get("ventana", []) if v["t"] >= limite]
    alertas = [a for a in estado.get("alertas", []) if a["t"] >= limite]
    total = tendencias = 0

    for f in fuentes:
        d, tipo = obtener_feed(f, estado)
        if not d:
            print(f"- {f['nombre']}: sin feed disponible")
            continue

        vistos = estado["vistos"].get(f["nombre"])
        primera_vez = vistos is None
        vistos = set(vistos or [])
        nuevas = [e for e in d.entries if id_entrada(e) not in vistos]
        nuevas.sort(key=fecha_entrada)

        elegidas = []
        descartadas = 0
        for e in nuevas:
            if (cfg["solo_coyuntura"] and not es_coyuntura(e, clas)) or not pasa_filtro(cfg, e):
                descartadas += 1
                continue
            titulo = html.unescape(e.get("title", "")).strip()
            if tipo == "google":
                titulo = re.sub(r"\s+-\s+[^-]{2,60}$", "", titulo)
            publicada = fecha_entrada(e) or time.time()
            entrada = {"t": min(publicada, time.time()), "f": f["nombre"], "titulo": titulo,
                       "link": e.get("link", ""), "tok": palabras_clave_titulo(titulo),
                       "ambito": clasificar_ambito(e, f, clas)}
            if entrada["t"] >= limite:   # lo viejo no cuenta para tendencias
                ventana.append(entrada)
            elegidas.append((e, entrada))

        if primera_vez:
            # No inundar el chat la primera vez: solo se marcan como vistas.
            print(f"- {f['nombre']}: primera lectura, {len(nuevas)} noticias marcadas como vistas")
        else:
            por_enviar = [(e, en) for e, en in elegidas if e.get("link") not in enviados_set]
            for e, entrada in por_enviar[-cfg["max_por_fuente"]:]:
                if enviar(cfg, formatear_clasificada(f["nombre"], e, tipo, entrada["ambito"])):
                    total += 1
                    enviados_set.add(e.get("link"))
                    estado["enviados"].append(e.get("link"))
                    time.sleep(cfg["pausa"])
            if cfg["alertas_tendencia"]:
                for _, entrada in elegidas:
                    if revisar_tendencia(cfg, entrada, ventana, alertas):
                        tendencias += 1
                        time.sleep(cfg["pausa"])
            if nuevas:
                print(f"- {f['nombre']}: {len(nuevas)} nuevas, {len(por_enviar)} coyunturales, "
                      f"{descartadas} descartadas")

        estado["ventana"] = ventana
        estado["alertas"] = alertas
        vistos.update(id_entrada(e) for e in d.entries)
        estado["vistos"][f["nombre"]] = list(vistos)[-MAX_VISTOS_POR_FUENTE:]
        estado["enviados"] = estado["enviados"][-MAX_ENLACES_GLOBALES:]
        guardar_estado(estado)
    print(f"Ronda terminada: {total} noticias y {tendencias} alertas de tendencia enviadas.")


def probar(fuentes, estado):
    print(f"Revisando {len(fuentes)} fuentes...\n")
    ok = 0
    for f in fuentes:
        url, tipo = resolver_feed(f)
        if not url:
            print(f"[X] {f['nombre']}: no encontré feed. Revisa el dominio o pon la URL en la columna feed.")
            continue
        ok += 1
        estado["feeds"][f["nombre"]] = {"url": url, "tipo": tipo}
        d = leer_feed(url)
        ultima = html.unescape(d.entries[0].get("title", ""))[:70] if d else ""
        marca = "RSS   " if tipo == "rss" else "GOOGLE"
        print(f"[OK] {marca} {f['nombre']}: {url}\n            última: {ultima}")
    guardar_estado(estado)
    print(f"\n{ok} de {len(fuentes)} fuentes funcionan.")


def mostrar_chat_ids(cfg):
    r = SESION.get(f"https://api.telegram.org/bot{cfg['token']}/getUpdates", timeout=20)
    datos = r.json()
    if not datos.get("ok"):
        sys.exit(f"Telegram respondió: {datos}")
    vistos = {}
    for u in datos["result"]:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            vistos[chat["id"]] = chat.get("title") or chat.get("first_name") or chat.get("username")
    if not vistos:
        print("No hay mensajes. Escríbele algo al bot (o agrégalo al grupo y escribe ahí) y vuelve a correr esto.")
    for cid, nombre in vistos.items():
        print(f"chat_id = {cid}   ({nombre})")


def escribir_en_config(clave, valor):
    """Cambia una línea 'clave = ...' de config.ini sin borrar los comentarios."""
    texto = ARCHIVO_CONFIG.read_text(encoding="utf-8")
    texto = re.sub(rf"(?m)^{clave}\s*=.*$", f"{clave} = {valor}", texto, count=1)
    ARCHIVO_CONFIG.write_text(texto, encoding="utf-8")


def configurar():
    print("Configuración del bot\n")
    token = input("Pega el token que te dio @BotFather: ").strip()
    if ":" not in token:
        sys.exit("Ese token no parece válido (debe tener la forma 123456:ABC...).")
    escribir_en_config("token", token)
    print("\nAhora abre Telegram y escríbele cualquier cosa al bot")
    print("(o agrégalo al grupo o canal y escribe ahí).")
    input("Cuando lo hayas hecho, regresa aquí y presiona Enter...")

    r = SESION.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    if not r.get("ok"):
        sys.exit(f"Telegram rechazó el token: {r.get('description')}")
    chats = {}
    for u in r["result"]:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[str(chat["id"])] = chat.get("title") or chat.get("first_name") or chat.get("username")
    if not chats:
        sys.exit("El bot no ha recibido mensajes todavía. Escríbele y vuelve a correr --configurar.")
    opciones = list(chats.items())
    for i, (cid, nombre) in enumerate(opciones, 1):
        print(f"  {i}. {nombre}  ({cid})")
    eleccion = input("¿A cuál chat mando las noticias? Escribe el número: ").strip() or "1"
    cid = opciones[int(eleccion) - 1][0]
    escribir_en_config("chat_id", cid)
    cfg = cargar_config()
    ok = enviar(cfg, "✅ El bot de noticias quedó conectado.")
    print("\nListo. Revisa Telegram." if ok else "\nGuardé los datos, pero el mensaje de prueba no salió.")


def main():
    ap = argparse.ArgumentParser(description="Noticias RSS a Telegram")
    ap.add_argument("--bucle", action="store_true", help="repetir cada intervalo_minutos")
    ap.add_argument("--probar", action="store_true", help="revisar fuentes sin enviar")
    ap.add_argument("--chat-id", action="store_true", help="mostrar los chat_id que ve el bot")
    ap.add_argument("--prueba-telegram", action="store_true", help="enviar un mensaje de prueba")
    ap.add_argument("--configurar", action="store_true", help="pedir token y chat_id y guardarlos")
    args = ap.parse_args()

    if args.configurar:
        configurar()
        return

    cfg = cargar_config()
    estado = cargar_estado()

    if args.probar:
        probar(cargar_fuentes(), estado)
        return
    if not cfg["token"] or "PEGA" in cfg["token"]:
        sys.exit("Falta el token del bot en config.ini.")
    if args.chat_id:
        mostrar_chat_ids(cfg)
        return
    if not cfg["chat_id"]:
        sys.exit("Falta chat_id en config.ini. Corre:  python3 noticias_telegram.py --chat-id")
    if args.prueba_telegram:
        print("Enviado." if enviar(cfg, "✅ Prueba: el bot de noticias está conectado.") else "No se pudo enviar.")
        return

    fuentes = cargar_fuentes()
    if not args.bucle:
        ronda(cfg, fuentes, estado)
        return
    print(f"Modo continuo: reviso cada {cfg['intervalo']} minutos. Ctrl+C para detener.")
    while True:
        try:
            ronda(cfg, fuentes, estado)
        except Exception as err:  # una fuente rara no debe tumbar el bucle
            print(f"Error en la ronda: {err}")
        time.sleep(cfg["intervalo"] * 60)


if __name__ == "__main__":
    main()
