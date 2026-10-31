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
    return time.mktime(t) if t else 0


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
    enviados_set = set(estado["enviados"])
    total = 0
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

        if primera_vez:
            # No inundar el chat la primera vez: solo se marcan como vistas.
            print(f"- {f['nombre']}: primera lectura, {len(nuevas)} noticias marcadas como vistas")
        else:
            por_enviar = [e for e in nuevas
                          if pasa_filtro(cfg, e) and e.get("link") not in enviados_set]
            for e in por_enviar[-cfg["max_por_fuente"]:]:
                if enviar(cfg, formatear(f["nombre"], e, tipo)):
                    total += 1
                    enviados_set.add(e.get("link"))
                    estado["enviados"].append(e.get("link"))
                    time.sleep(cfg["pausa"])
            if nuevas:
                print(f"- {f['nombre']}: {len(nuevas)} nuevas, {len(por_enviar)} pasan el filtro")

        vistos.update(id_entrada(e) for e in d.entries)
        estado["vistos"][f["nombre"]] = list(vistos)[-MAX_VISTOS_POR_FUENTE:]
        estado["enviados"] = estado["enviados"][-MAX_ENLACES_GLOBALES:]
        guardar_estado(estado)
    print(f"Ronda terminada: {total} mensajes enviados.")


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
