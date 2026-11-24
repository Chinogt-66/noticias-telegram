#!/usr/bin/env python3
"""
Recopila noticias y las envía a un chat o grupo de Telegram, solo como texto.

Enfoque por defecto (enfoque = italia en config.ini):
  - personas de nacionalidad italiana en Guatemala (seguridad, legal, migración,
    política, economía, social, cultura)
  - noticias que involucran a Guatemala e Italia
Con enfoque = coyuntura vuelve al modo anterior (coyuntura nacional e internacional).

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
        "palabras": [normalizar(p) for p in palabras],
        # italia = comunidad italiana en Guatemala y relación Guatemala–Italia
        # coyuntura = noticias de coyuntura nacional e internacional (modo anterior)
        "enfoque": normalizar(op.get("enfoque", "italia").strip()) or "italia",
        "solo_coyuntura": op.get("solo_coyuntura", "si").strip().lower() in ("si", "sí", "yes", "true", "1"),
        "alertas_tendencia": op.get("alertas_tendencia", "si").strip().lower() in ("si", "sí", "yes", "true", "1"),
        "umbral_tendencia": int(op.get("umbral_tendencia", "3")),
        "ventana_horas": float(op.get("ventana_tendencia_horas", "6")),
        "solo_hoy": op.get("solo_de_hoy", "si").strip().lower() in ("si", "sí", "yes", "true", "1"),
        # Guatemala está en UTC-6 todo el año (no cambia de horario)
        "utc_offset": float(op.get("utc_offset_horas", "-6")),
        # Resumen con IA: se activa solo si existe la variable ANTHROPIC_API_KEY
        "clave_ia": os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        "usar_ia": op.get("resumen_ia", "si").strip().lower() in ("si", "sí", "yes", "true", "1"),
        "modelo_ia": op.get("modelo_ia", "claude-haiku-5-5").strip(),
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

EDICIONES_GOOGLE = {
    "google": "&hl=es-419&gl=GT&ceid=GT:es-419",   # Google Noticias Guatemala, en español
    "google-it": "&hl=it&gl=IT&ceid=IT:it",         # Google Noticias Italia, en italiano
}


def url_google_news(consulta, edicion="google"):
    return ("https://news.google.com/rss/search?q=" + quote_plus(consulta)
            + EDICIONES_GOOGLE[edicion])


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

    for edicion in ("google-it", "google"):
        if feed.startswith(edicion + ":"):
            url = url_google_news(feed[len(edicion) + 1:].strip(), edicion)
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

def destinos(cfg):
    """TELEGRAM_CHAT_ID admite uno o varios chats separados por coma."""
    return [c.strip() for c in str(cfg["chat_id"]).split(",") if c.strip()]


def enviar(cfg, texto):
    """Manda solo texto, sin imagen y sin vista previa del enlace."""
    ok = False
    for chat in destinos(cfg):
        ok = llamar_telegram(cfg, "sendMessage", {
            "chat_id": chat,
            "text": texto,
            "parse_mode": "HTML",
            "link_preview_options": json.dumps({"is_disabled": True}),
        }) or ok
    return ok


def llamar_telegram(cfg, metodo, datos, silencioso=False):
    url = f"https://api.telegram.org/bot{cfg['token']}/{metodo}"
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
            # Cuando un grupo se convierte en supergrupo, Telegram le cambia el ID
            try:
                nuevo_id = r.json().get("parameters", {}).get("migrate_to_chat_id")
            except ValueError:
                nuevo_id = None
            if nuevo_id:
                print(f"  ATENCIÓN: el grupo cambió de ID. Cambia el secreto TELEGRAM_CHAT_ID a {nuevo_id}")
                viejo = str(datos.get("chat_id"))
                cfg["chat_id"] = ",".join(str(nuevo_id) if c == viejo else c for c in destinos(cfg))
                datos = dict(datos, chat_id=nuevo_id)
                continue
            if not silencioso:
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
    # ---- Enfoque Italia ----
    # Cualquier mención de Italia o de italianos (en español y en italiano)
    "italia": """italia, italian, italoguatemal, italo-guatemal, embajada de italia, embajador de italia,
        consulado de italia, ambasciata, farnesina, connazional, cooperacion italiana, aics,
        meloni, tajani, mattarella, guardia di finanza, carabinieri, ansa""",
    # Conexión con Guatemala (en títulos de medios extranjeros)
    "guatemala": """guatemal, ciudad de guatemala, antigua guatemala, peten, izabal, quetzaltenango, xela,
        huehuetenango, solola, atitlan, alta verapaz, coban, escuintla, chiquimula, zacapa, jutiapa,
        san marcos, quiche, totonicapan, retalhuleu, mazatenango, chimaltenango, mixco, villa nueva""",
    # Personas italianas en Guatemala (comunidad, residentes, casos individuales)
    "comunidad_italiana": """ciudadano italiano, ciudadana italiana, ciudadanos italianos, nacionalidad italiana,
        origen italiano, origen italiana, italiano residente, italiana residente, residentes italianos,
        empresario italiano, empresaria italiana, inversionista italiano, sacerdote italiano,
        misionero italiano, misionera italiana, religiosa italiana, monja italiana, turista italian,
        comunidad italiana, colectividad italiana, italoguatemal, connazional, cittadino italiano,
        italiano arrestato, italiana arrestata, italiano detenido, italiana detenida, italiano capturado,
        italiana capturada""",
    # Relación entre Estados, gobiernos e instituciones
    "bilateral": """embajada, embajador, ambasciat, consulado, consul general, canciller, minex, farnesina,
        gobierno de italia, gobierno italiano, governo italiano, cooperacion italiana, cooperazione, aics,
        bilateral, acuerdo, convenio, memorando, visita oficial, delegacion, ministro, ministra,
        presidente, meloni, tajani, mattarella, donacion, donativo, exporta, importa, comercio,
        inversion, union europea, instituto italiano, camara de comercio italiana, camara italiana,
        escuela italiana, istituto italiano""",
    # Temas (en este orden de prioridad)
    "tema_seguridad": """captur, detenid, arrest, homicid, asesinat, ataque, secuestr, robo, asalt, extorsi,
        narco, droga, cocaina, pandill, violencia, crimen, criminal, desaparecid, balead, ataque armado,
        pnc, policia, ejercito, interpol, mafia, ndrangheta, camorra, rapina, omicidio, ucciso""",
    "tema_legal": """juez, jueza, juzgado, tribunal, corte, sentencia, juicio, audiencia, mp, fiscal,
        fiscalia, extradi, orden de captura, orden de aprehension, demanda, amparo, condena, proceso penal,
        acusad, denuncia, ley, decreto, reglamento, tribunale, processo, condanna, estradizione""",
    "tema_migracion": """visa, visado, pasaporte, consulado, consular, migracion, migratori, residencia,
        nacionalidad, ciudadania, doble nacionalidad, deport, expuls, igm, voto en el exterior, aire,
        cittadinanza, rimpatri""",
    "tema_politica": """canciller, minex, gobierno, presidente, ministro,
        ministra, congreso, diputad, diplomati, bilateral, visita oficial, acuerdo, convenio, eleccion,
        parlamento, senado, governo""",
    "tema_economia": """empresa, empresari, inversi, comercio, export, import, negocio, camara de comercio,
        turismo, cafe, empleo, arancel, mercado, industria, banco, financ, impuesto, sat, economi,
        aziend, investiment""",
    "tema_social": """cooperaci, cooperazione, donaci, donativo, salud, hospital, educaci, escuela, comunidades,
        iglesia, sacerdote, misioner, religios, parroquia, ong, voluntari, desastre, emergencia, damnificad,
        pobreza, nutrici, ninez, mujeres, indigena""",
    "tema_cultura": """cultur, festival, arte, artista, musica, cine, idioma, lengua italiana, beca,
        universidad, patrimonio, arqueolog, exposicion, instituto italiano""",
    # En el enfoque Italia solo se descarta deporte y entretenimiento sin relevancia
    "excluir_italia": """deporte, futbol, serie a, calcio, champions, juventus, milan, napoli, ferrari,
        formula 1, motogp, giro de italia, tenis, sinner, horoscopo, receta, pizza, lasagna, lasana,
        loteria, videojuego, farandula, celebridad, reality""",
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


def es_de_hoy(e, cfg):
    """True si la noticia se publicó hoy, según la hora de Guatemala.
    Las que no traen fecha se descartan: no hay forma de saber si son viejas."""
    publicada = fecha_entrada(e)
    if not publicada:
        return False
    desfase = cfg["utc_offset"] * 3600
    dia = lambda ts: time.strftime("%Y-%m-%d", time.gmtime(ts + desfase))
    return dia(publicada) == dia(time.time())


def misma_noticia(a, b):
    a, b = set(a), set(b)
    if not a or not b:
        return False
    comunes = len(a & b)
    return comunes >= 4 or (comunes >= 2 and comunes / min(len(a), len(b)) >= 0.5)


ETIQUETAS = {
    "nacional": "🇬🇹 <b>Nacional</b>",
    "internacional": "🌎 <b>Internacional</b>",
    "comunidad": "🇮🇹 <b>Italianos en Guatemala</b>",
    "bilateral": "🇬🇹🇮🇹 <b>Guatemala–Italia</b>",
}

TEMAS = [   # (clave, etiqueta) en orden de prioridad
    ("tema_seguridad", "🚨 Seguridad"),
    ("tema_legal", "⚖️ Legal y justicia"),
    ("tema_migracion", "🛂 Migración y consular"),
    ("tema_politica", "🏛️ Política y diplomacia"),
    ("tema_economia", "💼 Economía"),
    ("tema_social", "🤝 Social"),
    ("tema_cultura", "🎭 Cultura y educación"),
]


def dominio_de(url):
    return re.sub(r"^www\.", "", re.sub(r"^https?://", "", url or "").split("/")[0].lower())


def medio_de(e, fuente, tipo):
    """Nombre del medio que publicó la nota. En Google Noticias es el medio original."""
    if tipo == "google":
        fuente_g = e.get("source") or {}
        if fuente_g.get("title"):
            return fuente_g["title"].strip()
        m = re.search(r"\s+-\s+([^-]{2,60})$", html.unescape(e.get("title", "")))
        if m:
            return m.group(1).strip()
    return fuente["nombre"]


def es_medio_guatemalteco(e, fuente, tipo, dominios_gt):
    if tipo == "google":
        dom = dominio_de((e.get("source") or {}).get("href", ""))
        return bool(dom) and (dom.endswith(".gt") or dom in dominios_gt)
    return fuente.get("ambito") != "internacional"


def relacion_con_italia(e, fuente, tipo, clas, dominios_gt):
    """Devuelve 'comunidad', 'bilateral' o None si la nota no trata de Italia y Guatemala."""
    patrones, secciones = clas
    titulo = normalizar(html.unescape(e.get("title", "")))
    texto = texto_de(e)
    if not patrones["italia"].search(texto) or patrones["excluir_italia"].search(titulo):
        return None
    # Un medio guatemalteco hablando de un italiano casi siempre es un hecho en Guatemala,
    # salvo que la nota esté en su sección internacional. Los medios extranjeros deben
    # mencionar a Guatemala.
    seccion_internacional = bool(secciones_de(e.get("link")) & secciones["secciones_internacional"])
    guatemalteco = es_medio_guatemalteco(e, fuente, tipo, dominios_gt)
    if not patrones["guatemala"].search(texto) and (not guatemalteco or seccion_internacional):
        return None
    if patrones["comunidad_italiana"].search(texto):
        return "comunidad"
    if patrones["bilateral"].search(texto):
        return "bilateral"
    return "comunidad" if re.search(r"\bitalian[oa]s?\b", texto) else "bilateral"


def tema_de(e, clas, ambito=None):
    patrones, _ = clas
    texto = texto_de(e)
    for clave, etiqueta in TEMAS:
        if patrones[clave].search(texto):
            return etiqueta
    return "🏛️ Política y diplomacia" if ambito == "bilateral" else "📰 General"


def formatear_clasificada(nombre, e, tipo, ambito):
    titulo = html.unescape(e.get("title", "(sin título)")).strip()
    if tipo == "google":
        titulo = re.sub(r"\s+-\s+[^-]{2,60}$", "", titulo)
    enlace = e.get("link", "")
    return (f"{ETIQUETAS[ambito]} · {html.escape(nombre)}\n"
            f"<a href=\"{html.escape(enlace, quote=True)}\">{html.escape(titulo)}</a>")


# ---------------------------------------------------------------- título y resumen

def limpiar_html(texto):
    texto = re.sub(r"(?is)<(script|style|noscript|figure|figcaption)[^>]*>.*?</\1>", " ", texto or "")
    texto = re.sub(r"<[^>]+>", " ", texto)
    return re.sub(r"\s+", " ", html.unescape(texto)).strip()


_PAGINAS = {}


def descargar_pagina(enlace):
    """Baja el HTML de la nota una sola vez (lo usan el resumen y la imagen)."""
    if not enlace or "news.google.com" in enlace:
        return ""
    if enlace not in _PAGINAS:
        try:
            r = SESION.get(enlace, timeout=15)
            r.raise_for_status()
            _PAGINAS[enlace] = r.text
        except requests.RequestException:
            _PAGINAS[enlace] = ""
        if len(_PAGINAS) > 50:
            _PAGINAS.pop(next(iter(_PAGINAS)))
    return _PAGINAS[enlace]


def texto_de_la_nota(e):
    """Intenta leer los párrafos de la nota; si no se puede, usa el resumen del feed."""
    resumen_feed = limpiar_html(e.get("summary", ""))
    pagina = descargar_pagina(e.get("link", ""))
    if not pagina:
        return resumen_feed
    parrafos = [limpiar_html(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", pagina)]
    cuerpo = " ".join(p for p in parrafos if len(p) > 60)
    return (cuerpo or resumen_feed)[:3500]


def resumen_simple(e):
    """Resumen gratis: la primera frase que trae el feed, corta."""
    if "news.google.com" in e.get("link", ""):
        return ""   # el "resumen" de Google Noticias solo repite el título
    texto = limpiar_html(e.get("summary", ""))
    texto = re.sub(r"\s*(The post|La entrada|Leer más|Seguir leyendo).*$", "", texto, flags=re.I)
    resumen = re.split(r"(?<=[.!?])\s+", texto)[0].strip()
    if len(resumen) > 180:
        resumen = resumen[:180].rsplit(" ", 1)[0] + "…"
    return resumen


INSTRUCCIONES_IA = """Eres editor de un servicio de alertas de noticias para Guatemala.
Recibes una nota periodística y devuelves SOLO un JSON con esta forma:
{"titulo": "...", "resumen": ["...", "..."]}

titulo: máximo 90 caracteres, en español, directo y atractivo, que invite a leer.
Debe decir lo mismo que la nota: nada de exagerar, inventar cifras, opinar ni usar
preguntas tipo carnada. Puede empezar con el dato más fuerte.

resumen: exactamente 2 viñetas, cada una de máximo 20 palabras. La primera dice qué
pasó; la segunda, por qué importa o qué sigue. Usa datos concretos de la nota
(nombres, cifras, lugares). Si la nota no da para dos, escribe una sola.
No agregues nada que no esté en el texto."""


def resumen_con_ia(cfg, titulo, medio, texto):
    """Pide a Claude título y resumen. Devuelve (titulo, [viñetas]) o None si falla."""
    cuerpo = {
        "model": cfg["modelo_ia"],
        "max_tokens": 400,
        "system": INSTRUCCIONES_IA,
        "messages": [{"role": "user", "content":
                      f"Medio: {medio}\nTítulo original: {titulo}\n\nTexto de la nota:\n{texto or '(sin texto)'}"}],
    }
    try:
        r = SESION.post("https://api.anthropic.com/v1/messages", json=cuerpo, timeout=60,
                        headers={"x-api-key": cfg["clave_ia"], "anthropic-version": "2023-06-01"})
        r.raise_for_status()
        salida = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        datos = json.loads(re.search(r"\{.*\}", salida, re.S).group(0))
        nuevo = str(datos.get("titulo", "")).strip()
        vinetas = [str(v).strip() for v in datos.get("resumen", []) if str(v).strip()][:2]
        return (nuevo or titulo), vinetas
    except Exception as err:   # si la IA falla, la nota igual se manda con el resumen simple
        print(f"  IA no disponible para esta nota: {err}")
        return None


def titulo_y_resumen(cfg, nombre, e, entrada):
    titulo, vinetas = entrada["titulo"], []
    if cfg["usar_ia"] and cfg["clave_ia"]:
        res = resumen_con_ia(cfg, titulo, nombre, texto_de_la_nota(e))
        if res:
            titulo, vinetas = res
    if not vinetas:
        simple = resumen_simple(e)
        if simple and normalizar(simple[:60]) != normalizar(titulo[:60]):
            vinetas = [simple]
    return titulo, vinetas


def armar_texto(nombre, entrada, titulo, vinetas, limite=4000):
    """Arma el mensaje en HTML. Si no cabe en el límite, quita viñetas desde el final."""
    vinetas = list(vinetas)
    while True:
        cabecera = ETIQUETAS[entrada["ambito"]]
        if entrada.get("tema"):
            cabecera += f" · {entrada['tema']}"
        lineas = [f"{cabecera}\n<i>{html.escape(nombre)}</i>",
                  f"<b>{html.escape(titulo)}</b>"]
        if vinetas:
            lineas.append("")
            lineas += [f"• {html.escape(v)}" for v in vinetas]
        lineas.append(f"\n<a href=\"{html.escape(entrada['link'], quote=True)}\">Leer la nota completa</a>")
        texto = "\n".join(lineas)
        if len(texto) <= limite or not vinetas:
            return texto
        vinetas.pop()


def construir_mensaje(cfg, nombre, e, entrada):
    titulo, vinetas = titulo_y_resumen(cfg, nombre, e, entrada)
    return armar_texto(nombre, entrada, titulo, vinetas)


def publicar_nota(cfg, nombre, e, entrada):
    """Manda la nota como texto (sin imagen)."""
    titulo, vinetas = titulo_y_resumen(cfg, nombre, e, entrada)
    return enviar(cfg, armar_texto(nombre, entrada, titulo, vinetas))


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
    enfoque_italia = cfg["enfoque"] == "italia"
    dominios_gt = {f["dominio"].removeprefix("www.") for f in fuentes
                   if f["dominio"] and f.get("ambito") != "internacional"}
    enviados_set = set(estado["enviados"])
    limite = time.time() - cfg["ventana_horas"] * 3600
    ventana = [v for v in estado.get("ventana", []) if v["t"] >= limite]
    alertas = [a for a in estado.get("alertas", []) if a["t"] >= limite]
    # Títulos enviados en las últimas 48 h: la misma nota puede salir en varias búsquedas
    titulos_enviados = [t for t in estado.get("titulos_enviados", []) if t["t"] >= time.time() - 48 * 3600]
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
            if cfg["solo_hoy"] and not es_de_hoy(e, cfg):
                descartadas += 1
                continue
            if not pasa_filtro(cfg, e):
                descartadas += 1
                continue
            tema = None
            if enfoque_italia:
                ambito = relacion_con_italia(e, f, tipo, clas, dominios_gt)
                if not ambito:
                    descartadas += 1
                    continue
                tema = tema_de(e, clas, ambito)
            else:
                if cfg["solo_coyuntura"] and not es_coyuntura(e, clas):
                    descartadas += 1
                    continue
                ambito = clasificar_ambito(e, f, clas)
            titulo = html.unescape(e.get("title", "")).strip()
            if tipo == "google":
                titulo = re.sub(r"\s+-\s+[^-]{2,60}$", "", titulo)
            publicada = fecha_entrada(e) or time.time()
            entrada = {"t": min(publicada, time.time()), "f": medio_de(e, f, tipo), "titulo": titulo,
                       "link": e.get("link", ""), "tok": palabras_clave_titulo(titulo),
                       "ambito": ambito, "tema": tema}
            if entrada["t"] >= limite:   # lo viejo no cuenta para tendencias
                ventana.append(entrada)
            elegidas.append((e, entrada))

        if primera_vez:
            # No inundar el chat la primera vez: solo se marcan como vistas.
            print(f"- {f['nombre']}: primera lectura, {len(nuevas)} noticias marcadas como vistas")
        else:
            por_enviar = [(e, en) for e, en in elegidas if e.get("link") not in enviados_set]
            for e, entrada in por_enviar[-cfg["max_por_fuente"]:]:
                repetida = any(t["titulo"] == normalizar(entrada["titulo"]) or
                               (len(set(t["tok"]) & set(entrada["tok"])) >= 3 and
                                len(set(t["tok"]) & set(entrada["tok"])) / max(1, min(len(t["tok"]), len(entrada["tok"]))) >= 0.75)
                               for t in titulos_enviados)
                if repetida:
                    continue
                if publicar_nota(cfg, entrada["f"], e, entrada):
                    total += 1
                    enviados_set.add(e.get("link"))
                    estado["enviados"].append(e.get("link"))
                    titulos_enviados.append({"t": time.time(), "titulo": normalizar(entrada["titulo"]),
                                             "tok": entrada["tok"]})
                    time.sleep(cfg["pausa"])
            if cfg["alertas_tendencia"]:
                for _, entrada in elegidas:
                    if revisar_tendencia(cfg, entrada, ventana, alertas):
                        tendencias += 1
                        time.sleep(cfg["pausa"])
            if nuevas:
                print(f"- {f['nombre']}: {len(nuevas)} nuevas, {len(por_enviar)} relevantes, "
                      f"{descartadas} descartadas")

        estado["ventana"] = ventana
        estado["alertas"] = alertas
        estado["titulos_enviados"] = titulos_enviados
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
