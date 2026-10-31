# El bot de noticias desde el iPhone

iOS no permite que una app se quede corriendo en segundo plano. Si el script vive en el iPhone, solo revisa noticias cuando lo abres o cuando un atajo lo dispara a una hora fija. Por eso hay dos caminos:

| | A. GitHub Actions | B. a-Shell en el iPhone |
|---|---|---|
| Frecuencia | cada 15 minutos | solo a las horas que programes |
| iPhone apagado | sigue funcionando | no funciona |
| Costo | gratis con repositorio público | gratis |
| Lo configuras desde | Safari en el iPhone | la app a-Shell |

Para noticias casi en tiempo real, el camino A. El iPhone solo lo usas para configurarlo y para leer Telegram.

Antes de cualquiera de los dos, guarda los archivos que te mandé en la app Archivos (mantén presionado cada uno > Guardar en Archivos).

---

## Paso común: crear el bot

1. En Telegram busca a **@BotFather**, mándale `/newbot` y ponle nombre.
2. Copia el token que te da (algo como `7123456789:AAH...`).
3. Escríbele cualquier mensaje a tu bot. Si lo quieres en un grupo, agrégalo y escribe algo en el grupo.

---

## Camino A: GitHub Actions (recomendado)

GitHub ejecuta el script en sus servidores cada 15 minutos.

**1. Cuenta y repositorio**

1. En Safari entra a github.com y crea una cuenta.
2. Toca **+** > **New repository**. Nombre: `noticias-telegram`. Déjalo **Public** y crea el repositorio.

Público significa que cualquiera puede ver tu lista de medios, pero el token del bot queda oculto en los secretos. En un repositorio privado, el plan gratis trae un cupo de minutos al mes que no alcanza para correr cada 15 minutos; ahí tendrías que cambiar el `*/15` del archivo .yml por `*/30` o `0 *` (cada hora).

Si algún menú no aparece en Safari, toca **aA** en la barra de direcciones > **Solicitar sitio web de escritorio**.

**2. Subir los archivos**

1. En el repositorio toca **Add file** > **Upload files** y sube `noticias_telegram.py`, `config.ini` y `fuentes.csv`. Toca **Commit changes**.
2. Toca **Add file** > **Create new file**. En el nombre escribe exactamente:
   `.github/workflows/noticias.yml`
   Abre el archivo `noticias.yml` en la app Archivos, copia todo el texto y pégalo. **Commit changes**.

No hace falta poner el token en `config.ini`. Va en el paso siguiente.

**3. Sacar el chat_id con Safari**

Abre esta dirección, cambiando TOKEN por el tuyo:

`https://api.telegram.org/botTOKEN/getUpdates`

Busca el texto `"chat":{"id":` y copia el número que sigue (en grupos empieza con `-100`).

**4. Guardar token y chat_id como secretos**

En el repositorio: **Settings** > **Secrets and variables** > **Actions** > **New repository secret**. Crea dos:

- Nombre `TELEGRAM_TOKEN`, valor: tu token.
- Nombre `TELEGRAM_CHAT_ID`, valor: el número del paso 3.

**5. Probar**

1. Pestaña **Actions** > **Noticias a Telegram** > **Run workflow**. Elige `probar` y córrelo.
2. Al terminar, toca la ejecución > **ronda** > **Revisar fuentes**. Ahí ves cada medio con `[OK]` o `[X]`.
3. Corre otra vez con `ronda`. La primera solo marca lo que ya existe como leído; desde la siguiente (a los 15 minutos) llegan las noticias nuevas a Telegram.

**Cambiar medios o palabras clave**

Abre `fuentes.csv` o `config.ini` en github.com, toca el lápiz, edita y **Commit changes**. La próxima ronda ya usa los cambios. En `config.ini` deja vacíos `token` y `chat_id`.

---

## Camino B: a-Shell en el iPhone

a-Shell es una terminal gratuita del App Store que trae Python.

**1. Instalar**

1. Descarga **a-Shell** del App Store y ábrela una vez.
2. En la app Archivos, crea dentro de **En mi iPhone > a-Shell** una carpeta llamada `noticias_telegram` y mueve ahí `noticias_telegram.py`, `config.ini` y `fuentes.csv`.
3. En a-Shell escribe:

```
cd noticias_telegram
pip install feedparser requests
```

**2. Configurar sin editar archivos**

```
python3 noticias_telegram.py --configurar
```

Te pide el token, te deja elegir el chat y manda un mensaje de prueba.

**3. Revisar fuentes y hacer la primera ronda**

```
python3 noticias_telegram.py --probar
python3 noticias_telegram.py
```

Si dejas a-Shell abierta en pantalla, `python3 noticias_telegram.py --bucle` revisa cada 10 minutos hasta que salgas de la app.

**4. Programarlo con Atajos**

1. App **Atajos** > **Automatización** > **+** > **Hora del día**.
2. Elige la hora, **Diariamente**, y marca **Ejecutar inmediatamente**.
3. Como acción busca **a-Shell** y elige **Execute Command**. Escribe:
   `cd ~/Documents/noticias_telegram && python3 noticias_telegram.py`
4. Repite con otras horas (por ejemplo 7:00, 12:00, 18:00 y 21:00). Cada automatización es una revisión.

Cada vez que corre trae todo lo publicado desde la revisión anterior, hasta 8 noticias por medio (lo cambias en `max_por_fuente_por_ronda`).

---

## Si algo falla

- **`[X]` en un medio**: el dominio en `fuentes.csv` está mal o el sitio no responde. Corrige el dominio o pon la URL del RSS en la tercera columna.
- **No llega nada a Telegram**: confirma que le escribiste al bot y que el chat_id es el correcto. En grupos, el bot tiene que ser miembro.
- **GitHub dejó de correr**: GitHub pausa las tareas programadas de repositorios sin actividad por mucho tiempo. En **Actions** aparece un botón para reactivarla.
