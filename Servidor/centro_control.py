# Proyecto: SecurityBike AI - Servidor Central (Centro de Control)
#
# Integrantes:
# - LUIS FERNANDO MIRELES RODRIGUEZ (23240002)
# - JOSE RODOLFO LOPEZ TORRES (22240496)
# - VICTOR MANUEL ORTEGA GOMEZ (23240031)
#
# Carrera: Ingenieria en Sistemas Computacionales
#
# Objetivo del programa:
# Este es el cerebro del sistema. Levanta una pagina web con Flask, muestra el
# video de la camara IP (DroidCam), usa inteligencia artificial (DeepFace) para reconocer
# rostros, se conecta a Firebase para guardar usuarios y accesos, y manda la
# orden de abrir la cerradura por MQTT (y por UDP como respaldo).
# Tambien valida quien entra a la pagina con tokens de Firebase y separa los
# permisos entre rol "admin" y rol "alumno".
# ======================

# CONFIGURACION GENERAL (librerias)
import base64
import json
import os
import socket
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, g
from flask_cors import CORS

# Estas librerias son "pesadas". Las importamos con try/except para que el
# servidor no se caiga si alguna no esta instalada todavia. La pagina y
# /api/status deben funcionar aunque falte la camara o la IA.
try:
    import cv2
    import numpy as np
except Exception as exc:
    cv2 = None
    np = None
    print(f"[WARN] OpenCV/numpy no disponible: {exc}")

try:
    import firebase_admin
    from firebase_admin import credentials, firestore, auth as fb_auth
except Exception as exc:
    firebase_admin = None
    credentials = None
    firestore = None
    fb_auth = None
    print(f"[WARN] Firebase Admin SDK no disponible: {exc}")

try:
    from deepface import DeepFace
except Exception as exc:
    DeepFace = None
    print(f"[WARN] DeepFace no disponible: {exc}")

try:
    import paho.mqtt.client as mqtt
except Exception as exc:
    mqtt = None
    print(f"[WARN] paho-mqtt no disponible: {exc}")


# CONFIGURACION GENERAL (rutas y carga de config.json)
ROOT = Path(__file__).resolve().parent          # carpeta Servidor/
INTERFAZ_DIR = ROOT.parent / "Interfaz"         # carpeta de la pagina web
CONFIG_FILE = ROOT / "config.json"

# Valores por defecto. Si falta config.json se crea con estos datos.
CONFIG_DEFAULT = {
    "url_camara": "http://192.168.137.207:4747/video",
    "tipo_camara": "DroidCam",
    "nombre_camara": "Camara IP del telefono",
    "camara_opcional": True,
    "ip_cerradura_udp": "192.168.1.20",
    "puerto_udp": 5051,
    "usar_udp_respaldo": True,
    "mqtt_broker": "test.mosquitto.org",
    "mqtt_port": 1883,
    "mqtt_client_id": "securitybike_servidor",
    "firebase_key_path": "tu_llave_firebase.json",
    "carpeta_bd": "alumnos_db",
    "modelo_deepface": "Facenet",
    "detector_backend": "opencv",
    "modo_demo_ia": True,
    "resize_ia_ancho": 480,
    "intervalo_ia_segundos": 0.7,
    "frames_confirmacion": 1,
    "umbral_reconocimiento": None,
    "cooldown_apertura_segundos": 5,
    "cooldown_denegado_ia_segundos": 10,
    "mostrar_desconocidos": True,
    "host": "0.0.0.0",
    "puerto_servidor": 5000,
    "debug": False,
    "admin_emails": ["admin@securitybike.local"],
}


def cargar_config():
    # Lee config.json. Si no existe lo crea con los valores por defecto.
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(CONFIG_DEFAULT, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("[INFO] Se creo config.json con valores de ejemplo. Revisalo antes de probar.")
    try:
        datos = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] No se pudo leer config.json, se usaran valores por defecto: {exc}")
        datos = {}
    config = CONFIG_DEFAULT.copy()
    config.update(datos)
    return config


CONFIG = cargar_config()

# Carpeta donde se guardan los rostros locales para la IA. Se crea sola.
CARPETA_BD = ROOT / CONFIG["carpeta_bd"]
CARPETA_BD.mkdir(exist_ok=True)
print(f"[OK] Carpeta de rostros lista: {CARPETA_BD}")

# Carpeta para las fotos de evidencia de entradas/salidas. Se crea sola.
CARPETA_EVID = ROOT / "evidencias"
CARPETA_EVID.mkdir(exist_ok=True)

# Lista de correos que seran administradores.
ADMIN_EMAILS = [c.strip().lower() for c in CONFIG.get("admin_emails", []) if c]


# CONFIGURACION GENERAL (app Flask y estado del sistema)
app = Flask(__name__)
CORS(app)

# Socket UDP de respaldo para abrir la cerradura si MQTT falla.
sock_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# Estado en memoria del sistema. Se muestra en los badges de la pagina.
estado = {
    "servidorFlask": True,
    "firebase": False,
    "mqtt": False,
    "camara": False,
    "cerradura": False,
    "urlCamara": CONFIG["url_camara"],
    "mensaje": "Servidor iniciado",
    "mensajeCamara": "",
    "alumnos": 0,
    "ultimoReconocimiento": None,
}
estado_lock = threading.Lock()

# Estado del modo "escuchar tarjeta RFID" para enlazar tarjetas a un alumno.
# el endpoint /api/rfid/estado filtra y nunca muestra datos de otros usuarios.
rfid_estado = {
    "activo": False,
    "uidDestino": None,
    "nombreDestino": None,
    "iniciadoPor": None,
    "ultimaTarjeta": None,        # ultima tarjeta detectada (global, solo admin)
    "msg": "Sin actividad RFID",
    "ultimoEvento": "",           # descripcion del ultimo evento RFID (solo admin)
    "ultimoEnlaceUid": None,      # uid del alumno del ultimo enlace exitoso
    "ultimoEnlaceTarjeta": None,  # tarjeta del ultimo enlace exitoso
    "timestamp": 0,
}
rfid_lock = threading.Lock()

# Variables de control de la IA y de la cerradura.
db = None                       # cliente de Firestore
cliente_mqtt = None             # cliente MQTT
ultimo_actuador_visto = 0.0     # ultima vez que el actuador reporto por MQTT
ultima_apertura = 0.0           # control de cooldown de apertura
ultimo_ambiente = {}            # ultimos datos de sensores (DHT11 + LDR) por MQTT


def actualizar_estado(**kwargs):
    # Actualiza el estado del sistema de forma segura entre hilos.
    with estado_lock:
        estado.update(kwargs)


# CONFIGURACION DE FIREBASE
def inicializar_firebase():
    # Conecta con Firebase usando la llave de servicio. Si falla, el sistema
    # sigue vivo pero marca firebase = False.
    global db
    if firebase_admin is None:
        actualizar_estado(firebase=False, mensaje="firebase-admin no instalado")
        return
    ruta_llave = ROOT / CONFIG["firebase_key_path"]
    if not ruta_llave.exists():
        actualizar_estado(firebase=False, mensaje=f"Falta la llave {ruta_llave.name}")
        print(f"[WARN] No se encontro {ruta_llave.name}. Copia tu llave real en Servidor/.")
        return
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(str(ruta_llave))
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        actualizar_estado(firebase=True)
        print("[OK] Conectado a Firebase.")
    except Exception as exc:
        actualizar_estado(firebase=False, mensaje=f"Error Firebase: {exc}")
        print(f"[WARN] Error al conectar con Firebase: {exc}")


def marca_tiempo():
    # Devuelve el timestamp del servidor de Firestore, o la hora local si no hay.
    if firestore is not None:
        return firestore.SERVER_TIMESTAMP
    return datetime.now().isoformat()


# CONFIGURACION MQTT
# Topicos que usa el sistema.
TOPICO_COMANDO = "securitybike/acceso/comando"      # servidor publica ABRIR
TOPICO_EVENTO = "securitybike/acceso/evento"        # servidor publica accesos
TOPICO_RFID = "securitybike/actuador/rfid"          # actuador publica tarjetas
TOPICO_ACTUADOR = "securitybike/actuador/status"    # actuador publica su estado
TOPICO_IA_STATUS = "securitybike/ia/status"         # servidor publica estado IA
TOPICO_IA_RECONOCE = "securitybike/ia/reconocimiento"
TOPICO_CAM_STATUS = "securitybike/camara/status"
TOPICO_CAM_IP = "securitybike/camara/ip"
TOPICO_FIREBASE = "securitybike/firebase/status"
TOPICO_SENSORES = "securitybike/sensores/ambiente"  # actuador publica DHT11 + LDR


def inicializar_mqtt():
    # Conecta al broker MQTT y se suscribe a los topicos que le interesan.
    global cliente_mqtt
    if mqtt is None:
        actualizar_estado(mqtt=False)
        print("[WARN] paho-mqtt no instalado. Se usara solo UDP de respaldo.")
        return
    try:
        # paho-mqtt 2.x pide indicar la version del API de callbacks.
        try:
            cliente_mqtt = mqtt.Client(
                client_id=CONFIG["mqtt_client_id"],
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            )
        except (AttributeError, TypeError):
            cliente_mqtt = mqtt.Client(client_id=CONFIG["mqtt_client_id"])

        cliente_mqtt.on_connect = mqtt_conectado
        cliente_mqtt.on_message = mqtt_mensaje
        cliente_mqtt.on_disconnect = mqtt_desconectado
        cliente_mqtt.connect(CONFIG["mqtt_broker"], int(CONFIG["mqtt_port"]), 60)
        cliente_mqtt.loop_start()
        print(f"[OK] Conectando a MQTT {CONFIG['mqtt_broker']}:{CONFIG['mqtt_port']}")
    except Exception as exc:
        actualizar_estado(mqtt=False)
        print(f"[WARN] No se pudo conectar a MQTT: {exc}")


def mqtt_conectado(cliente, userdata, flags, rc):
    # Cuando se conecta, se suscribe a los topicos del actuador y la camara.
    if rc == 0:
        actualizar_estado(mqtt=True)
        cliente.subscribe(TOPICO_RFID)
        cliente.subscribe(TOPICO_ACTUADOR)
        cliente.subscribe(TOPICO_CAM_STATUS)
        cliente.subscribe(TOPICO_CAM_IP)
        cliente.subscribe(TOPICO_SENSORES)
        publicar_mqtt(TOPICO_FIREBASE, {"firebase": estado["firebase"]})
        print("[OK] MQTT conectado y suscrito a topicos.")
    else:
        actualizar_estado(mqtt=False)
        print(f"[WARN] MQTT respondio con codigo {rc}")


def mqtt_desconectado(cliente, userdata, rc):
    actualizar_estado(mqtt=False)
    print("[WARN] MQTT desconectado.")


def mqtt_mensaje(cliente, userdata, msg):
    # Aqui llegan los mensajes de los topicos suscritos.
    global ultimo_actuador_visto, ultimo_ambiente
    try:
        texto = msg.payload.decode("utf-8")
    except Exception:
        texto = ""
    try:
        datos = json.loads(texto)
    except Exception:
        datos = {"valor": texto}

    if msg.topic == TOPICO_RFID:
        # Llego una tarjeta RFID desde el actuador (puede venir como texto o lista).
        uid_tarjeta = datos.get("uidTarjeta") or datos.get("valor") or ""
        if uid_tarjeta:
            procesar_rfid(uid_tarjeta)
    elif msg.topic == TOPICO_ACTUADOR:
        # El actuador esta vivo: marcamos la cerradura como conectada.
        ultimo_actuador_visto = time.time()
        actualizar_estado(cerradura=True)
    elif msg.topic == TOPICO_CAM_STATUS:
        actualizar_estado(camara=bool(datos.get("ok", estado["camara"])))
    elif msg.topic == TOPICO_CAM_IP:
        ip = datos.get("ip")
        if ip:
            actualizar_estado(urlCamara=f"http://{ip}/stream")
    elif msg.topic == TOPICO_SENSORES:
        # Guardamos el ultimo ambiente (temperatura, humedad, dia/noche).
        if isinstance(datos, dict):
            ultimo_ambiente = datos


def publicar_mqtt(topico, payload):
    # Publica un diccionario como JSON en un topico. Devuelve True si pudo.
    if cliente_mqtt is None:
        return False
    try:
        cliente_mqtt.publish(topico, json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as exc:
        print(f"[WARN] No se pudo publicar en {topico}: {exc}")
        return False


# CONFIGURACION DE SEGURIDAD (tokens y roles)
def obtener_rol_por_email(email):
    # Decide el rol SOLO por la lista de admin_emails de config.json.
    if email and email.lower() in ADMIN_EMAILS:
        return "admin"
    return "alumno"


def obtener_rol(uid, email):
    # El correo en admin_emails SIEMPRE manda: si esta en la lista, es admin,
    # aunque en Firestore haya quedado guardado como alumno.
    if email and email.lower() in ADMIN_EMAILS:
        return "admin"
    # Si no es admin por correo, se respeta el rol guardado en Firestore.
    if db is not None and uid:
        try:
            doc = db.collection("usuarios").document(uid).get()
            if doc.exists:
                data = doc.to_dict() or {}
                if data.get("rol"):
                    return data["rol"]
        except Exception:
            pass
    return "alumno"


def verificar_token():
    # Lee el header Authorization: Bearer <idToken> y lo valida con Firebase.
    # Devuelve (usuario, error). Si todo bien, error es None.
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, "Falta el token de autenticacion"
    token = header.split(" ", 1)[1].strip()
    if fb_auth is None or not estado["firebase"]:
        return None, "Firebase no esta disponible en el servidor"
    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception as exc:
        return None, f"Token invalido: {exc}"
    uid = decoded.get("uid")
    email = decoded.get("email", "")
    usuario = {"uid": uid, "email": email, "rol": obtener_rol(uid, email)}
    return usuario, None


def require_auth(func):
    # Decorador: solo deja pasar si hay un token valido.
    @wraps(func)
    def wrapper(*args, **kwargs):
        usuario, error = verificar_token()
        if error:
            return jsonify(ok=False, error=error), 401
        g.usuario = usuario
        return func(*args, **kwargs)
    return wrapper


def require_admin(func):
    # Decorador: solo deja pasar si el usuario es admin.
    @wraps(func)
    def wrapper(*args, **kwargs):
        usuario, error = verificar_token()
        if error:
            return jsonify(ok=False, error=error), 401
        if usuario["rol"] != "admin":
            return jsonify(ok=False, error="Solo el administrador puede hacer esto"), 403
        g.usuario = usuario
        return func(*args, **kwargs)
    return wrapper


# PROCESAMIENTO Y SALIDA (rostros, accesos y cerradura)
def decodificar_rostro(foto_base64):
    # Convierte la foto Base64 en imagen de OpenCV. Devuelve (img, error).
    if cv2 is None or np is None:
        return None, "OpenCV no esta disponible para procesar el rostro"
    if not foto_base64 or "," not in foto_base64:
        return None, "La foto no llego en formato Base64 valido"
    try:
        solo_datos = foto_base64.split(",", 1)[1]
        img_bytes = base64.b64decode(solo_datos)
        arreglo = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(arreglo, cv2.IMREAD_COLOR)
        if img is None:
            return None, "No se pudo decodificar la imagen"
        return img, None
    except Exception as exc:
        return None, f"Error al leer la imagen: {exc}"


def validar_imagen_rostro(img):
    # Revisa que la imagen sea un rostro usable: no vacia, no negra y con cara.
    # Devuelve (valido, motivo). Asi evitamos que imagenes negras entren a la IA.
    if img is None or getattr(img, "size", 0) == 0:
        return False, "La imagen esta vacia."
    alto, ancho = img.shape[:2]
    if alto < 60 or ancho < 60:
        return False, "La imagen es demasiado pequena."
    # Brillo promedio: una imagen casi negra se rechaza.
    gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if float(gris.mean()) < 25:
        return False, "La imagen es invalida (muy oscura). Captura el rostro con buena iluminacion."
    # Si DeepFace esta disponible, exigimos que haya un rostro detectable.
    if DeepFace is not None:
        try:
            caras = DeepFace.extract_faces(img_path=img, enforce_detection=True)
            if not caras:
                return False, "No se detecto un rostro. Captura de frente y con buena luz."
        except ValueError:
            return False, "No se detecto un rostro. Captura de frente y con buena luz."
        except Exception:
            pass  # otros errores no deben bloquear; el brillo ya filtra imagenes negras
    return True, ""


def obtener_uid_desde_identity(identity_path):
    # Saca el uid del alumno desde la ruta que devuelve DeepFace.
    # Soporta nuevo formato (alumnos_db/UID/rostro_1.jpg) y viejo (alumnos_db/UID.jpg),
    # con rutas de Windows (\) o de Linux (/).
    p = Path(str(identity_path).replace("\\", "/"))
    padre = p.parent.name
    if padre and padre != CARPETA_BD.name:
        return padre  # nuevo: la carpeta padre es el uid
    return p.stem     # viejo: el nombre del archivo es el uid


def normalizar_lista_fotos(datos):
    # Devuelve siempre una lista de fotos. Acepta el formato nuevo (fotos: [...])
    # y el viejo (foto / fotoBase64 individual) para no romper compatibilidad.
    fotos = datos.get("fotos")
    if isinstance(fotos, list):
        return [f for f in fotos if f]
    una = datos.get("fotoBase64") or datos.get("foto")
    return [una] if una else []


def guardar_fotos_biometricas(uid, fotos, modo="replace"):
    # Guarda varias fotos del alumno en alumnos_db/{uid}/rostro_N.jpg.
    # modo "replace" borra las anteriores; modo "append" agrega mas.
    # Devuelve (cantidad_guardada, principal_base64, ruta_principal).
    carpeta = CARPETA_BD / uid
    carpeta.mkdir(parents=True, exist_ok=True)

    if modo == "replace":
        for f in carpeta.glob("*.*"):
            try:
                f.unlink()
            except Exception:
                pass
        inicio = 0
    else:  # append: continuar la numeracion existente
        inicio = len(list(carpeta.glob("rostro_*.jpg")))

    guardadas = 0
    principal_b64 = None
    ruta_principal = None
    for foto in fotos:
        img, error = decodificar_rostro(foto)
        if error:
            continue
        valido, _motivo = validar_imagen_rostro(img)
        if not valido:
            continue
        idx = inicio + guardadas + 1
        ruta = carpeta / f"rostro_{idx}.jpg"
        cv2.imwrite(str(ruta), img)
        if principal_b64 is None:
            principal_b64 = foto
            ruta_principal = ruta
        guardadas += 1

    limpiar_cache_deepface()
    actualizar_estado(alumnos=contar_alumnos())
    return guardadas, principal_b64, ruta_principal


def contar_fotos(uid):
    # Cuantas fotos biometricas tiene un alumno (carpeta nueva o archivo viejo).
    carpeta = CARPETA_BD / uid
    if carpeta.exists() and carpeta.is_dir():
        total = 0
        for patron in ("*.jpg", "*.jpeg", "*.png"):
            total += len(list(carpeta.glob(patron)))
        return total
    return 1 if (CARPETA_BD / f"{uid}.jpg").exists() else 0


def quitar_rostro_local(uid):
    # Borra los rostros locales de un usuario (al desactivarlo) para que la IA no lo use.
    carpeta = CARPETA_BD / uid
    try:
        if carpeta.exists() and carpeta.is_dir():
            for f in carpeta.glob("*.*"):
                try:
                    f.unlink()
                except Exception:
                    pass
            try:
                carpeta.rmdir()
            except Exception:
                pass
        legacy = CARPETA_BD / f"{uid}.jpg"
        if legacy.exists():
            legacy.unlink()
        limpiar_cache_deepface()
        actualizar_estado(alumnos=contar_alumnos())
    except Exception:
        pass


def actualizar_rostro_usuario(actor, uid, fotos, modo="replace"):
    # Actualiza el rostro de un usuario con una o varias fotos.
    # Permisos: el admin puede a cualquiera; el alumno solo a si mismo.
    if actor["rol"] != "admin" and actor["uid"] != uid:
        return jsonify(ok=False, error="No puedes cambiar el rostro de otro usuario"), 403
    if not fotos:
        return jsonify(ok=False, error="No llegaron fotos para guardar"), 400

    objetivo = obtener_usuario(uid) or {}
    rol_obj = objetivo.get("rol", "alumno")

    # El admin NO entra a la IA: solo guardamos una foto para mostrarla en la tabla.
    if rol_obj != "alumno":
        img, error = decodificar_rostro(fotos[0])
        if error:
            return jsonify(ok=False, error=error, msg=error), 400
        if db is not None:
            try:
                db.collection("usuarios").document(uid).set(
                    {"rostroBase64": fotos[0], "biometricoValido": False,
                     "actualizadoEn": marca_tiempo()}, merge=True)
            except Exception as exc:
                return jsonify(ok=False, error=str(exc)), 500
        return jsonify(ok=True, msg="Rostro actualizado.")

    # Alumno: guardamos varias fotos para que la IA reconozca mejor.
    cantidad, principal, _ruta = guardar_fotos_biometricas(uid, fotos, modo)
    if cantidad == 0:
        return jsonify(ok=False, error="Ninguna foto fue valida. Captura de frente y con buena luz.",
                       msg="Ninguna foto fue valida."), 400

    total = contar_fotos(uid)
    cambios = {
        "biometricoValido": True,
        "cantidadRostros": total,
        "rostroLocalPath": f"{CONFIG['carpeta_bd']}/{uid}/rostro_1.jpg",
        "rostrosActualizadosEn": marca_tiempo(),
        "actualizadoEn": marca_tiempo(),
    }
    # En replace ponemos la nueva foto principal; en append solo si no habia ninguna.
    if modo == "replace" and principal:
        cambios["rostroBase64"] = principal
    elif modo == "append" and not objetivo.get("rostroBase64") and principal:
        cambios["rostroBase64"] = principal
    if db is not None:
        try:
            db.collection("usuarios").document(uid).set(cambios, merge=True)
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 500
    registrar_acceso(uid, objetivo.get("nombreCompleto", ""), "BIOMETRICO", "PERMITIDO",
                     f"Biometrico actualizado con {total} fotos", "servidor")
    return jsonify(ok=True, cantidad=total,
                   msg=f"Rostro actualizado con {cantidad} foto(s). Total guardadas: {total}.")


def cambiar_activo(uid, activo):
    # Activa o desactiva un alumno (desactivacion logica, sin borrar datos).
    if db is None:
        return jsonify(ok=False, error="Firebase no disponible"), 503
    perfil = obtener_usuario(uid) or {}
    try:
        db.collection("usuarios").document(uid).set(
            {"activo": activo, "actualizadoEn": marca_tiempo()}, merge=True)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    detalle = "Alumno reactivado" if activo else "Alumno desactivado"
    registrar_acceso(uid, perfil.get("nombreCompleto", ""), "ADMIN", "PERMITIDO", detalle, "admin")
    if not activo:
        # Quitamos sus rostros locales para que la IA deje de reconocerlo.
        quitar_rostro_local(uid)
    return jsonify(ok=True, msg=detalle)


def limpiar_cache_deepface():
    # DeepFace crea archivos .pkl con las caras procesadas (incluso en subcarpetas).
    # Al cambiar fotos hay que borrarlos para que tome los rostros nuevos.
    if not CARPETA_BD.exists():
        return
    for archivo in CARPETA_BD.rglob("*.pkl"):
        try:
            archivo.unlink()
        except Exception:
            pass


def contar_alumnos():
    # Cuenta cuantos ALUMNOS distintos tienen rostro guardado (carpeta nueva o
    # archivo viejo), ignorando la carpeta 'ignorados'.
    if not CARPETA_BD.exists():
        return 0
    uids = set()
    for item in CARPETA_BD.iterdir():
        if item.name == "ignorados":
            continue
        if item.is_dir():
            tiene = any(item.glob(p) for p in ("*.jpg", "*.jpeg", "*.png"))
            if tiene:
                uids.add(item.name)
        elif item.suffix.lower() in (".jpg", ".jpeg", ".png"):
            uids.add(item.stem)
    return len(uids)


def obtener_usuario(uid):
    # Trae un usuario de Firestore por su uid.
    if db is None or not uid:
        return None
    try:
        doc = db.collection("usuarios").document(uid).get()
        if doc.exists:
            return doc.to_dict()
    except Exception:
        pass
    return None


def registrar_acceso(uid, nombre, metodo, estado_acceso, detalle="", origen="servidor",
                     uid_tarjeta=None, tipo_movimiento=None, captura_base64=None,
                     captura_local=None, origen_camara=None):
    # Guarda un registro de acceso en Firestore y avisa por MQTT.
    registro = {
        "uid": uid,
        "nombreCompleto": nombre,
        "metodo": metodo,
        "estado": estado_acceso,
        "detalle": detalle,
        "origen": origen,
        "uidTarjeta": uid_tarjeta,
        "tipoMovimiento": tipo_movimiento,
        "capturaBase64": captura_base64,
        "capturaLocalPath": captura_local,
        "origenCamara": origen_camara,
        "fechaHora": marca_tiempo(),
    }
    if db is not None:
        try:
            db.collection("accesos").add(registro)
        except Exception as exc:
            print(f"[WARN] No se pudo guardar el acceso en Firestore: {exc}")
    # Para MQTT mandamos la hora como texto y SIN la foto (seria muy pesada).
    evento = dict(registro)
    evento["fechaHora"] = datetime.now().isoformat()
    evento.pop("capturaBase64", None)
    publicar_mqtt(TOPICO_EVENTO, evento)
    print(f"[ACCESO] {estado_acceso} - {nombre or uid} por {metodo} ({tipo_movimiento or '-'})")


def _key_fecha(data):
    # Clave para ordenar accesos por fecha (timestamp Firestore o texto ISO).
    f = data.get("fechaHora")
    try:
        return f.timestamp()
    except Exception:
        return 0


def calcular_movimiento(uid):
    # Con un solo lector no sabemos si es entrada o salida, asi que alternamos:
    # el primer acceso del alumno es ENTRADA, el siguiente SALIDA, y asi.
    if db is None or not uid:
        return "ENTRADA"
    try:
        movimientos = []
        for doc in db.collection("accesos").where("uid", "==", uid).stream():
            data = doc.to_dict() or {}
            if data.get("tipoMovimiento") in ("ENTRADA", "SALIDA"):
                movimientos.append(data)
        if movimientos:
            movimientos.sort(key=_key_fecha, reverse=True)
            return "SALIDA" if movimientos[0]["tipoMovimiento"] == "ENTRADA" else "ENTRADA"
    except Exception as exc:
        print(f"[MOV] Error calculando movimiento: {exc}")
    return "ENTRADA"


def capturar_evidencia(con_overlay=True):
    # Toma una foto desde DroidCam (el ultimo frame) como evidencia del acceso.
    # Devuelve (capturaBase64, capturaLocalPath) o (None, None) si no hay camara.
    if cv2 is None:
        return None, None
    frame = camara.obtener_frame()
    if frame is None:
        return None, None
    try:
        if con_overlay:
            frame = dibujar_recuadro(frame.copy())  # incluye el cuadro verde/rojo
        alto, ancho = frame.shape[:2]
        if ancho > 480:
            esc = 480.0 / ancho
            frame = cv2.resize(frame, (int(ancho * esc), int(alto * esc)))
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 55])
        if not ok:
            return None, None
        b64 = "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode()
        # Guardar tambien una copia local en evidencias/YYYYMMDD/.
        ruta_local = ""
        try:
            dia = datetime.now().strftime("%Y%m%d")
            carpeta = CARPETA_EVID / dia
            carpeta.mkdir(parents=True, exist_ok=True)
            nombre_archivo = f"{int(time.time() * 1000)}.jpg"
            cv2.imwrite(str(carpeta / nombre_archivo), frame)
            ruta_local = f"evidencias/{dia}/{nombre_archivo}"
        except Exception:
            pass
        return b64, ruta_local
    except Exception as exc:
        print(f"[EVIDENCIA] Error al capturar: {exc}")
        return None, None


def normalizar_uid_rfid(valor):
    # Convierte cualquier formato de UID a uno solo: "B6 C7 40 07".
    # Acepta lista de enteros, "[0xB6, 0xC7, 0x40, 0x07]", "0xB6,0xC7,...",
    # "B6 C7 40 07" o "B6C74007".
    if valor is None:
        return ""
    if isinstance(valor, (list, tuple)):
        return " ".join("{:02X}".format(int(b) & 0xFF) for b in valor)
    texto = str(valor).strip()
    for basura in ("[", "]", "(", ")", "0x", "0X"):
        texto = texto.replace(basura, "")
    texto = texto.replace(",", " ")
    partes = texto.split()
    # Caso sin separadores: "B6C74007" -> partir en pares de dos.
    if len(partes) == 1 and len(partes[0]) > 2 and len(partes[0]) % 2 == 0:
        cadena = partes[0]
        partes = [cadena[i:i + 2] for i in range(0, len(cadena), 2)]
    return " ".join(p.upper().zfill(2) for p in partes)


def publicar_abrir(origen, uid, uid_tarjeta=None):
    # Manda la orden ABRIR por MQTT al actuador.
    payload = {
        "accion": "ABRIR",
        "origen": origen,
        "uid": uid,
        "uidTarjeta": uid_tarjeta,
        "fechaHora": datetime.now().isoformat(),
    }
    return publicar_mqtt(TOPICO_COMANDO, payload)


def publicar_beep(accion, origen, uid_tarjeta=None):
    # Manda un beep al actuador sin abrir la cerradura.
    # accion puede ser "BEEP_OK" (confirmacion) o "BEEP_ERROR" (alerta).
    payload = {
        "accion": accion,
        "origen": origen,
        "uidTarjeta": uid_tarjeta,
        "fechaHora": datetime.now().isoformat(),
    }
    return publicar_mqtt(TOPICO_COMANDO, payload)


def abrir_cerradura(origen, uid, nombre, metodo, estado_acceso="PERMITIDO", detalle="", uid_tarjeta=None):
    # Abre la cerradura respetando un cooldown para no abrir muchas veces seguidas.
    # Primero intenta MQTT; si no hay MQTT y esta activado, usa UDP de respaldo.
    global ultima_apertura
    ahora = time.time()
    cooldown = float(CONFIG["cooldown_apertura_segundos"])
    if estado_acceso == "PERMITIDO" and ahora - ultima_apertura < cooldown:
        return {"ok": False, "msg": "Espera un momento antes de volver a abrir."}

    via = "ninguno"
    if estado_acceso == "PERMITIDO":
        enviado = publicar_abrir(origen, uid, uid_tarjeta)
        if enviado:
            via = "MQTT"
        elif CONFIG.get("usar_udp_respaldo"):
            try:
                sock_udp.sendto(
                    b"ABRIR_PUERTA",
                    (CONFIG["ip_cerradura_udp"], int(CONFIG["puerto_udp"])),
                )
                via = "UDP"
            except Exception as exc:
                print(f"[WARN] Fallo UDP de respaldo: {exc}")
        ultima_apertura = ahora

    # Movimiento (entrada/salida) y evidencia fotografica.
    # La apertura WEB es la unica excepcion: no toma foto.
    es_web = metodo in ("WEB_ADMIN", "WEB_ALUMNO")
    detalle_final = detalle or f"Apertura via {via}"
    if es_web:
        tipo_mov = "WEB"
        cap_b64 = None
        cap_local = None
        orig_cam = None
    else:
        tipo_mov = calcular_movimiento(uid) if estado_acceso == "PERMITIDO" else "INTENTO_DENEGADO"
        cap_b64, cap_local = capturar_evidencia(con_overlay=True)
        orig_cam = CONFIG.get("tipo_camara", "DroidCam") if cap_b64 else None
        if cap_b64 is None:
            detalle_final += " (Sin evidencia visual: camara no disponible)"

    registrar_acceso(uid, nombre, metodo, estado_acceso, detalle_final, origen, uid_tarjeta,
                     tipo_movimiento=tipo_mov, captura_base64=cap_b64,
                     captura_local=cap_local, origen_camara=orig_cam)
    return {"ok": estado_acceso == "PERMITIDO", "via": via}


def buscar_usuario_por_rfid(uid_tarjeta):
    # Busca un alumno activo que tenga enlazada esa tarjeta RFID.
    if db is None:
        return None
    try:
        consulta = (
            db.collection("usuarios")
            .where("rfidUid", "==", uid_tarjeta)
            .limit(1)
            .stream()
        )
        for doc in consulta:
            data = doc.to_dict() or {}
            data["uid"] = doc.id
            return data
    except Exception as exc:
        print(f"[WARN] Error buscando tarjeta RFID: {exc}")
    return None


def procesar_rfid(uid_tarjeta):
    # Logica cuando llega una tarjeta RFID. El ESP32 solo lee y publica; aqui el
    # servidor decide, porque solo el servidor consulta Firestore.
    # 1) Si estamos en modo "escuchar" la enlazamos al alumno elegido (no abre).
    # 2) Si no, buscamos a quien pertenece y abrimos o negamos el acceso.
    uid = normalizar_uid_rfid(uid_tarjeta)
    if not uid:
        return

    with rfid_lock:
        activo = rfid_estado["activo"]
        uid_destino = rfid_estado["uidDestino"]
        nombre_destino = rfid_estado["nombreDestino"]
        rfid_estado["ultimaTarjeta"] = uid  # siempre guardamos la ultima leida

    # CASO 1: modo enlazar tarjeta
    if activo and uid_destino:
        if db is not None:
            try:
                db.collection("usuarios").document(uid_destino).update(
                    {"rfidUid": uid, "actualizadoEn": marca_tiempo()}
                )
                db.collection("rfid_pendiente").document("actual").set(
                    {"escuchando": False, "ultimaTarjeta": uid, "fechaHora": marca_tiempo()}, merge=True
                )
            except Exception as exc:
                print(f"[WARN] No se pudo enlazar la tarjeta: {exc}")
        with rfid_lock:
            rfid_estado.update(
                activo=False,
                msg=f"Tarjeta {uid} enlazada a {nombre_destino or uid_destino}",
                ultimoEvento=f"Tarjeta {uid} enlazada a {nombre_destino or uid_destino}",
                ultimoEnlaceUid=uid_destino,
                ultimoEnlaceTarjeta=uid,
            )
        registrar_acceso(uid_destino, nombre_destino, "RFID_ENLACE", "PERMITIDO",
                         "Tarjeta RFID enlazada", "servidor", uid)
        publicar_beep("BEEP_OK", "RFID_ENLACE", uid)
        print(f"[RFID] Tarjeta {uid} enlazada a {uid_destino}")
        return

    # CASO 2: revisar a quien pertenece la tarjeta
    usuario = buscar_usuario_por_rfid(uid)
    if usuario and usuario.get("activo", True):
        # Tarjeta vinculada y usuario activo: suena confirmacion y abre.
        nombre = usuario.get("nombreCompleto", "")
        abrir_cerradura(
            origen="RFID",
            uid=usuario["uid"],
            nombre=nombre,
            metodo="RFID",
            estado_acceso="PERMITIDO",
            detalle="Acceso por tarjeta RFID",
            uid_tarjeta=uid,
        )
        with rfid_lock:
            rfid_estado.update(msg=f"Acceso permitido a {nombre}",
                               ultimoEvento=f"Acceso permitido: {nombre} ({uid})")
    elif usuario:
        # La tarjeta existe pero el usuario esta inactivo: alerta y NO abre.
        nombre = usuario.get("nombreCompleto", "")
        cap_b64, cap_local = capturar_evidencia(con_overlay=False)
        orig_cam = CONFIG.get("tipo_camara", "DroidCam") if cap_b64 else None
        registrar_acceso(usuario.get("uid"), nombre, "RFID", "DENEGADO",
                         "Usuario inactivo", "actuador", uid,
                         tipo_movimiento="INTENTO_DENEGADO", captura_base64=cap_b64,
                         captura_local=cap_local, origen_camara=orig_cam)
        publicar_beep("BEEP_ERROR", "RFID", uid)
        with rfid_lock:
            rfid_estado.update(msg=f"Usuario inactivo: {nombre}",
                               ultimoEvento=f"Tarjeta {uid}: usuario inactivo")
    else:
        # Tarjeta no vinculada: suena alerta y NO abre.
        cap_b64, cap_local = capturar_evidencia(con_overlay=False)
        orig_cam = CONFIG.get("tipo_camara", "DroidCam") if cap_b64 else None
        registrar_acceso(None, "Tarjeta desconocida", "RFID", "DENEGADO",
                         f"Tarjeta {uid} no registrada", "actuador", uid,
                         tipo_movimiento="INTENTO_DENEGADO", captura_base64=cap_b64,
                         captura_local=cap_local, origen_camara=orig_cam)
        publicar_beep("BEEP_ERROR", "RFID", uid)
        with rfid_lock:
            rfid_estado.update(msg=f"Tarjeta {uid} no registrada",
                               ultimoEvento=f"Tarjeta {uid} no registrada (denegada)")


def registrar_evento(tipo, descripcion):
    # Guarda un evento del sistema en Firestore (historial tecnico).
    if db is None:
        return
    try:
        db.collection("eventos_sistema").add(
            {"tipo": tipo, "descripcion": descripcion, "fechaHora": marca_tiempo()}
        )
    except Exception:
        pass


# PROCESAMIENTO DE IA Y VIDEO
class CamaraESP32:
    # Lee el video de la camara IP (DroidCam) en un hilo aparte. Si se cae,
    # reconecta sola y guarda el ultimo frame para no congelar la pagina.
    def __init__(self, url):
        self.url = url
        self.cap = None
        self.frame = None
        self.lock = threading.Lock()
        self.activa = False

    def conectar(self):
        self.cerrar()
        if cv2 is None:
            return
        self.cap = cv2.VideoCapture(self.url)
        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

    def cerrar(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None

    def iniciar(self):
        if self.activa or cv2 is None:
            return
        self.activa = True
        threading.Thread(target=self._bucle, daemon=True).start()

    def obtener_frame(self):
        with self.lock:
            if self.frame is None:
                return None
            return self.frame.copy()

    def _log_cada(self, ultimo_log, texto):
        # Imprime un aviso de camara solo cada 15 s para no llenar la consola.
        if time.time() - ultimo_log > 15:
            print(texto)
            return time.time()
        return ultimo_log

    def _bucle(self):
        # La camara corre en su propio hilo: si DroidCam no conecta, el resto del
        # sistema (web, RFID, MQTT, login) sigue funcionando normalmente.
        self.conectar()
        fallos = 0
        ultimo_log = 0.0
        msg_offline = "No se pudo conectar. Revisa la IP actual de DroidCam en config.json."
        while self.activa:
            try:
                if self.cap is None or not self.cap.isOpened():
                    actualizar_estado(camara=False, mensajeCamara=msg_offline)
                    ultimo_log = self._log_cada(ultimo_log, f"[CAM] Sin conexion a {self.url} (revisa la IP de DroidCam)")
                    time.sleep(5)        # reintento tranquilo, sin spamear
                    self.conectar()
                    continue
                ok, frame = self.cap.read()
                if not ok or frame is None:
                    fallos += 1
                    actualizar_estado(camara=False, mensajeCamara="No llega video de DroidCam.")
                    if fallos >= 10:
                        ultimo_log = self._log_cada(ultimo_log, f"[CAM] Sin video de {self.url}")
                        self.conectar()
                        fallos = 0
                    time.sleep(0.3)
                    continue
                fallos = 0
                with self.lock:
                    self.frame = frame
                actualizar_estado(camara=True, mensajeCamara="")
                time.sleep(0.03)
            except Exception as exc:
                actualizar_estado(camara=False, mensajeCamara=f"Error de camara: {exc}")
                time.sleep(5)
                self.conectar()


camara = CamaraESP32(CONFIG["url_camara"])

# Overlay que se dibuja sobre el video: recuadro verde (reconocido) o rojo (no
# reconocido) con su texto, durante unos segundos.
overlay = {"hasta": 0.0, "color": (0, 255, 0), "texto": "", "region": None}
# Para "frames_confirmacion": cuantas veces seguidas vimos el mismo alumno.
ultimo_uid_candidato = None
conteo_confirmacion = 0
# Cooldown de alertas de rostro no reconocido (para no spamear).
ultima_alerta_desconocido = 0.0
# Detector de rostros de OpenCV (se carga una sola vez).
_cascada_rostro = None


def _get_cascada():
    # Carga el detector Haar de OpenCV una sola vez (rapido y ligero).
    global _cascada_rostro
    if _cascada_rostro is None and cv2 is not None:
        try:
            ruta = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            _cascada_rostro = cv2.CascadeClassifier(ruta)
        except Exception as exc:
            print(f"[IA] No se pudo cargar el detector de rostros: {exc}")
            _cascada_rostro = False  # marcamos que ya intentamos y fallo
    return _cascada_rostro


def detectar_rostro(frame):
    # Detecta si hay un rostro y devuelve (hay_rostro, region) con la region como
    # fracciones (fx, fy, fw, fh) de 0 a 1 para poder dibujarla en cualquier tamano.
    cas = _get_cascada()
    if not cas or cas.empty():
        return False, None
    try:
        gris = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        caras = cas.detectMultiScale(gris, scaleFactor=1.2, minNeighbors=5, minSize=(60, 60))
    except Exception:
        return False, None
    if len(caras) == 0:
        return False, None
    x, y, w, h = max(caras, key=lambda c: c[2] * c[3])  # el rostro mas grande
    alto, ancho = frame.shape[:2]
    return True, (x / ancho, y / alto, w / ancho, h / alto)


def analizar_frame_ia(frame):
    # Analiza un frame. Devuelve (uid_reconocido, rostro_detectado, region, distancia).
    # - Primero detecta si hay rostro (rapido, OpenCV).
    # - Solo si hay rostro corre DeepFace para reconocer (mas eficiente).
    if cv2 is None or frame is None:
        return None, False, None, None

    # Reducimos el frame al ancho de config (mas chico = mas rapido).
    f = frame
    try:
        alto, ancho = f.shape[:2]
        objetivo = int(CONFIG.get("resize_ia_ancho", 480))
        if objetivo > 0 and ancho > objetivo:
            escala = float(objetivo) / ancho
            f = cv2.resize(f, (int(ancho * escala), int(alto * escala)))
    except Exception:
        pass

    hay_rostro, region = detectar_rostro(f)
    if not hay_rostro:
        return None, False, None, None  # no hay nadie: no se registra ni suena nada
    if DeepFace is None or contar_alumnos() == 0:
        return None, True, region, None  # hay rostro pero no podemos reconocer

    parametros = dict(
        img_path=f,
        db_path=str(CARPETA_BD),
        model_name=CONFIG.get("modelo_deepface", "Facenet"),
        detector_backend=CONFIG.get("detector_backend", "opencv"),
        enforce_detection=False,
        silent=True,
    )
    try:
        try:
            resultados = DeepFace.find(**parametros, refresh_database=True)
        except TypeError:
            resultados = DeepFace.find(**parametros)
    except Exception as exc:
        # Error interno de DeepFace: NO lo tratamos como denegado (evita spam).
        print(f"[IA] DeepFace fallo en este frame: {exc}")
        return None, False, None, None
    if not resultados or len(resultados[0]) == 0:
        return None, True, region, None  # habia rostro pero no coincide con nadie

    df = resultados[0]
    try:
        df = df[~df["identity"].astype(str).str.contains("ignorados")]
    except Exception:
        pass
    if len(df) == 0:
        return None, True, region, None

    fila = df.iloc[0]
    uid = obtener_uid_desde_identity(fila["identity"])
    distancia = None
    if "distance" in df.columns:
        try:
            distancia = float(fila["distance"])
        except Exception:
            distancia = None
    if distancia is None:
        try:
            distancia = float(fila[df.columns[-1]])
        except Exception:
            distancia = None

    # Umbral opcional: si esta puesto y la distancia es mayor, no se reconoce.
    umbral = CONFIG.get("umbral_reconocimiento")
    if umbral is not None and distancia is not None and distancia > float(umbral):
        return None, True, region, distancia
    return uid, True, region, distancia


def fijar_overlay(color, texto, region):
    # Programa el recuadro (verde o rojo) que se dibuja en el video unos segundos.
    overlay["color"] = color
    overlay["texto"] = texto
    overlay["region"] = region
    overlay["hasta"] = time.time() + 3


def manejar_rostro_desconocido(region):
    # Rostro detectado pero NO reconocido: cuadro rojo siempre; registro y beep
    # con cooldown para no llenar el historial ni spamear el actuador.
    global ultima_alerta_desconocido
    if CONFIG.get("mostrar_desconocidos", True):
        fijar_overlay((0, 0, 255), "No reconocido", region)
    ahora = time.time()
    cooldown = float(CONFIG.get("cooldown_denegado_ia_segundos", 10))
    if ahora - ultima_alerta_desconocido > cooldown:
        # Guardamos evidencia con el cuadro rojo para que se vea el intento.
        cap_b64, cap_local = capturar_evidencia(con_overlay=True)
        orig_cam = CONFIG.get("tipo_camara", "DroidCam") if cap_b64 else None
        registrar_acceso(None, "Rostro no reconocido", "IA_ROSTRO", "DENEGADO",
                         "Intento de acceso por rostro no registrado", "DroidCam",
                         tipo_movimiento="INTENTO_DENEGADO", captura_base64=cap_b64,
                         captura_local=cap_local, origen_camara=orig_cam)
        publicar_beep("BEEP_ERROR", "IA_ROSTRO", None)
        ultima_alerta_desconocido = ahora
        print("[IA] Rostro no reconocido (DENEGADO)")


def reconocimiento_loop():
    # Hilo que cada cierto tiempo revisa la camara: reconoce alumnos validos (abre
    # con cuadro verde) o marca rostros desconocidos (cuadro rojo, no abre).
    global ultimo_uid_candidato, conteo_confirmacion
    while True:
        frame = camara.obtener_frame()
        if frame is not None:
            uid, rostro_detectado, region, distancia = analizar_frame_ia(frame)
            usuario = obtener_usuario(uid) if uid else None
            # SOLO abre para alumnos activos con biometrico valido.
            es_alumno_valido = (
                usuario is not None
                and usuario.get("rol") == "alumno"
                and usuario.get("activo", False)
                and usuario.get("biometricoValido", False)
            )
            if uid:
                nombre = usuario.get("nombreCompleto", uid) if usuario else uid
                print(f"[IA] Candidato: {nombre} | UID: {uid} | distancia: {distancia}")
            if es_alumno_valido:
                # En modo demo basta 1 reconocimiento; si no, pide N seguidos.
                frames_req = 1 if CONFIG.get("modo_demo_ia") else max(1, int(CONFIG.get("frames_confirmacion", 1)))
                if uid == ultimo_uid_candidato:
                    conteo_confirmacion += 1
                else:
                    ultimo_uid_candidato = uid
                    conteo_confirmacion = 1
                if conteo_confirmacion >= frames_req:
                    nombre = usuario.get("nombreCompleto", uid)
                    print(f"[IA] Reconocido: {nombre}")
                    fijar_overlay((0, 255, 0), nombre, region)  # cuadro verde
                    actualizar_estado(ultimoReconocimiento=f"{nombre} ({time.strftime('%H:%M:%S')})")
                    publicar_mqtt(TOPICO_IA_RECONOCE, {"uid": uid, "nombre": nombre})
                    abrir_cerradura(
                        origen="IA_ROSTRO",
                        uid=uid,
                        nombre=nombre,
                        metodo="IA_ROSTRO",
                        estado_acceso="PERMITIDO",
                        detalle="Reconocimiento facial",
                    )
                    conteo_confirmacion = 0
            elif uid:
                # Coincidio con alguien pero NO es alumno valido (admin/inactivo).
                print("[IA] Candidato ignorado por rol/estado/biometrico.")
                ultimo_uid_candidato = None
                conteo_confirmacion = 0
                manejar_rostro_desconocido(region)
            elif rostro_detectado:
                # Hay rostro pero no coincide con ningun alumno: cuadro rojo, no abre.
                print("[IA] Rostro detectado, sin coincidencia valida.")
                ultimo_uid_candidato = None
                conteo_confirmacion = 0
                manejar_rostro_desconocido(region)
            else:
                # No hay rostro: no se registra ni suena nada.
                ultimo_uid_candidato = None
                conteo_confirmacion = 0
        actualizar_estado(alumnos=contar_alumnos())
        time.sleep(float(CONFIG["intervalo_ia_segundos"]))


def frame_de_espera(mensaje="Camara IP no disponible"):
    # Imagen sencilla que se muestra cuando no hay video, para no romper la pagina.
    if cv2 is None or np is None:
        return None
    img = np.zeros((360, 480, 3), dtype=np.uint8)
    img[:] = (40, 40, 40)
    cv2.putText(img, "SecurityBike AI", (90, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 180, 0), 2)
    cv2.putText(img, mensaje[:34], (40, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (230, 230, 230), 1)
    cv2.putText(img, "Revisa DroidCam, IP y Wi-Fi", (75, 250), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 180, 180), 1)
    return img


def dibujar_recuadro(frame):
    # Dibuja el recuadro verde (reconocido) o rojo (no reconocido) sobre el video.
    if time.time() >= overlay["hasta"]:
        return frame
    alto, ancho = frame.shape[:2]
    region = overlay["region"]
    if region:
        fx, fy, fw, fh = region
        x1, y1 = int(fx * ancho), int(fy * alto)
        x2, y2 = int((fx + fw) * ancho), int((fy + fh) * alto)
    else:
        # Sin coordenadas exactas: recuadro central aproximado.
        x1, y1 = int(ancho * 0.2), int(alto * 0.15)
        x2, y2 = int(ancho * 0.8), int(alto * 0.85)
    color = overlay["color"]
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    cv2.putText(frame, overlay["texto"], (x1, max(24, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    return frame


def generar_video():
    # Genera el flujo MJPEG que ve el navegador en <img src="/stream">.
    while True:
        if cv2 is None:
            time.sleep(1)
            continue
        frame = camara.obtener_frame()
        if frame is None:
            frame = frame_de_espera(estado.get("mensaje", "Sin camara"))
            if frame is None:
                time.sleep(0.5)
                continue
        else:
            frame = dibujar_recuadro(frame)
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            time.sleep(0.1)
            continue
        jpg = buffer.tobytes()
        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
        time.sleep(0.04)


# RUTAS WEB
@app.route("/")
def home():
    # Sirve la pagina principal desde la carpeta Interfaz.
    return send_from_directory(str(INTERFAZ_DIR), "index.html")


@app.route("/css/<path:archivo>")
def servir_css(archivo):
    return send_from_directory(str(INTERFAZ_DIR / "css"), archivo)


@app.route("/js/<path:archivo>")
def servir_js(archivo):
    return send_from_directory(str(INTERFAZ_DIR / "js"), archivo)


@app.route("/stream")
def stream():
    # Video en vivo en formato MJPEG.
    return Response(generar_video(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
def api_status():
    # Estado del sistema para los badges. Funciona aunque no haya camara.
    # Si hace rato no sabemos del actuador, marcamos la cerradura como desconectada.
    if time.time() - ultimo_actuador_visto > 30:
        actualizar_estado(cerradura=False)
    with estado_lock:
        data = dict(estado)
    data["alumnos"] = contar_alumnos()
    data["mqttBroker"] = f"{CONFIG['mqtt_broker']}:{CONFIG['mqtt_port']}"
    data["ipCerradura"] = CONFIG["ip_cerradura_udp"]
    data["tipoCamara"] = CONFIG.get("tipo_camara", "Camara IP")
    data["nombreCamara"] = CONFIG.get("nombre_camara", "Camara IP")
    data["modoDemoIa"] = bool(CONFIG.get("modo_demo_ia", False))
    data["ambiente"] = ultimo_ambiente  # ultimos datos de DHT11 + LDR (MQTT)
    return jsonify(data)


@app.route("/api/auth/register-profile", methods=["POST"])
@require_auth
def register_profile():
    # Guarda el perfil del alumno (o admin) en Firestore y su rostro local.
    usuario = g.usuario
    datos = request.get_json(silent=True) or {}
    nombres = (datos.get("nombres") or "").strip()
    apellidos = (datos.get("apellidos") or "").strip()
    carrera = (datos.get("carrera") or "").strip()
    semestre = str(datos.get("semestre") or "").strip()
    # Aceptamos varias fotos (nuevo) o una sola (compatibilidad).
    fotos = normalizar_lista_fotos(datos)

    if not nombres or not apellidos:
        return jsonify(ok=False, error="Faltan nombres o apellidos"), 400
    if not fotos:
        return jsonify(ok=False, error="Falta la captura del rostro"), 400

    rol = obtener_rol_por_email(usuario["email"])

    ruta = ""
    cantidad = 0
    biometrico = False
    principal = fotos[0]  # foto que se muestra en la interfaz

    if rol == "alumno":
        # El alumno necesita al menos 3 fotos validas para un buen reconocimiento.
        if len(fotos) < 3:
            return jsonify(ok=False, error="Captura al menos 3 fotos para mejorar el reconocimiento."), 400
        cantidad, principal_guardada, _ = guardar_fotos_biometricas(usuario["uid"], fotos, "replace")
        if cantidad < 3:
            quitar_rostro_local(usuario["uid"])
            return jsonify(ok=False, error="No se validaron 3 fotos. Captura de frente y con buena luz."), 400
        principal = principal_guardada or principal
        ruta = f"{CONFIG['carpeta_bd']}/{usuario['uid']}/rostro_1.jpg"
        biometrico = True
    else:
        # El admin puede registrarse aunque su rostro no sea valido; no entra a la IA.
        img, error = decodificar_rostro(fotos[0])
        if error:
            return jsonify(ok=False, error=error), 400

    perfil = {
        "uid": usuario["uid"],
        "nombres": nombres,
        "apellidos": apellidos,
        "nombreCompleto": f"{nombres} {apellidos}".strip(),
        "carrera": carrera,
        "semestre": semestre,
        "correo": usuario["email"],
        "rol": rol,
        "rfidUid": None,
        "rostroBase64": principal,
        "rostroLocalPath": ruta,
        "biometricoValido": biometrico,
        "cantidadRostros": cantidad,
        "activo": True,
        "creadoEn": marca_tiempo(),
        "actualizadoEn": marca_tiempo(),
    }
    if db is None:
        return jsonify(ok=False, error="Firebase no esta disponible"), 503
    try:
        db.collection("usuarios").document(usuario["uid"]).set(perfil, merge=True)
    except Exception as exc:
        return jsonify(ok=False, error=f"No se pudo guardar el perfil: {exc}"), 500
    registrar_evento("REGISTRO", f"Nuevo usuario {perfil['nombreCompleto']} ({rol})")
    return jsonify(ok=True, rol=rol, msg="Perfil guardado correctamente")


@app.route("/api/me")
@require_auth
def api_me():
    # Devuelve los datos del usuario que esta autenticado.
    usuario = g.usuario
    perfil = obtener_usuario(usuario["uid"]) or {}
    # Si el correo es admin pero en Firestore quedo como alumno, lo corregimos.
    if usuario["rol"] == "admin" and perfil.get("rol") != "admin" and db is not None:
        try:
            db.collection("usuarios").document(usuario["uid"]).set(
                {"rol": "admin", "actualizadoEn": marca_tiempo()}, merge=True
            )
        except Exception:
            pass
    perfil["uid"] = usuario["uid"]
    perfil["rol"] = usuario["rol"]
    return jsonify(ok=True, usuario=perfil)


@app.route("/api/alumnos")
@require_admin
def api_alumnos():
    # Lista todos los usuarios registrados (solo admin).
    if db is None:
        return jsonify(ok=False, error="Firebase no disponible"), 503
    lista = []
    try:
        for doc in db.collection("usuarios").stream():
            data = doc.to_dict() or {}
            data["uid"] = doc.id
            # Saltamos documentos incompletos/basura: sin correo no es un alumno real.
            if not data.get("correo"):
                continue
            lista.append(data)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    # Ordenamos por nombre para que la tabla salga limpia.
    lista.sort(key=lambda x: (x.get("nombreCompleto") or "zzz").lower())
    return jsonify(ok=True, alumnos=lista)


@app.route("/api/alumnos/<uid>")
@require_auth
def api_alumno_detalle(uid):
    # El admin ve a cualquiera; el alumno solo se ve a si mismo.
    usuario = g.usuario
    if usuario["rol"] != "admin" and usuario["uid"] != uid:
        return jsonify(ok=False, error="No tienes permiso para ver este alumno"), 403
    perfil = obtener_usuario(uid)
    if not perfil:
        return jsonify(ok=False, error="Alumno no encontrado"), 404
    perfil["uid"] = uid
    return jsonify(ok=True, alumno=perfil)


@app.route("/api/accesos")
@require_admin
def api_accesos():
    # Historial completo de accesos (solo admin).
    if db is None:
        return jsonify(ok=False, error="Firebase no disponible"), 503
    lista = []
    try:
        consulta = db.collection("accesos").order_by(
            "fechaHora", direction=firestore.Query.DESCENDING
        ).limit(200).stream()
        for doc in consulta:
            data = doc.to_dict() or {}
            data["id"] = doc.id
            lista.append(serializar_fecha(data))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    return jsonify(ok=True, accesos=lista)


@app.route("/api/accesos/mios")
@require_auth
def api_accesos_mios():
    # Historial del alumno autenticado.
    usuario = g.usuario
    if db is None:
        return jsonify(ok=False, error="Firebase no disponible"), 503
    lista = []
    try:
        consulta = db.collection("accesos").where("uid", "==", usuario["uid"]).limit(200).stream()
        for doc in consulta:
            data = doc.to_dict() or {}
            data["id"] = doc.id
            lista.append(serializar_fecha(data))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    # Ordenamos por fecha de forma manual para no exigir un indice de Firestore.
    lista.sort(key=lambda x: x.get("fechaHora", ""), reverse=True)
    return jsonify(ok=True, accesos=lista)


@app.route("/api/entradas-salidas")
@require_auth
def api_entradas_salidas():
    # Movimientos (entradas/salidas) con foto de evidencia.
    # Admin ve todos; el alumno solo los suyos (los denegados con uid null NO se
    # muestran al alumno porque su consulta filtra por su propio uid).
    usuario = g.usuario
    if db is None:
        return jsonify(ok=False, error="Firebase no disponible"), 503
    f_metodo = request.args.get("metodo")
    f_estado = request.args.get("estado")
    f_mov = request.args.get("tipoMovimiento")
    lista = []
    try:
        if usuario["rol"] == "admin":
            consulta = db.collection("accesos").stream()
        else:
            consulta = db.collection("accesos").where("uid", "==", usuario["uid"]).stream()
        for doc in consulta:
            data = doc.to_dict() or {}
            data["id"] = doc.id
            if f_metodo and data.get("metodo") != f_metodo:
                continue
            if f_estado and data.get("estado") != f_estado:
                continue
            if f_mov and data.get("tipoMovimiento") != f_mov:
                continue
            lista.append(serializar_fecha(data))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    lista.sort(key=lambda x: x.get("fechaHora", ""), reverse=True)
    return jsonify(ok=True, movimientos=lista[:100])  # limite para no enviar demasiadas fotos


@app.route("/api/abrir", methods=["POST"])
@require_auth
def api_abrir():
    # Abrir la cerradura desde la pagina (admin o alumno).
    usuario = g.usuario
    perfil = obtener_usuario(usuario["uid"]) or {}
    nombre = perfil.get("nombreCompleto", usuario["email"])
    metodo = "WEB_ADMIN" if usuario["rol"] == "admin" else "WEB_ALUMNO"
    resultado = abrir_cerradura(
        origen=metodo,
        uid=usuario["uid"],
        nombre=nombre,
        metodo=metodo,
        estado_acceso="PERMITIDO",
        detalle="Apertura desde la pagina web",
    )
    if not resultado["ok"]:
        return jsonify(ok=False, error=resultado.get("msg", "No se pudo abrir")), 429
    return jsonify(ok=True, msg="Orden de apertura enviada", via=resultado.get("via"))


@app.route("/api/rfid/escuchar", methods=["POST"])
@require_auth
def api_rfid_escuchar():
    # Activa el modo "esperar tarjeta" para enlazarla a un alumno.
    # El admin puede enlazar a cualquiera; el alumno solo a si mismo.
    datos = request.get_json(silent=True) or {}
    uid_destino = (datos.get("uidDestino") or datos.get("uid") or "").strip()
    if not uid_destino:
        return jsonify(ok=False, error="Falta el uidDestino del alumno"), 400
    if g.usuario["rol"] != "admin" and uid_destino != g.usuario["uid"]:
        return jsonify(ok=False, error="Solo puedes enlazar tu propia tarjeta"), 403
    perfil = obtener_usuario(uid_destino) or {}
    with rfid_lock:
        rfid_estado.update(
            activo=True,
            uidDestino=uid_destino,
            nombreDestino=perfil.get("nombreCompleto", uid_destino),
            iniciadoPor=g.usuario["email"],
            msg="Acerque la tarjeta al lector RFID...",
            timestamp=time.time(),
        )
    # Tambien lo dejamos en Firestore por si el actuador quiere consultarlo.
    if db is not None:
        try:
            db.collection("rfid_pendiente").document("actual").set(
                {
                    "escuchando": True,
                    "uidDestino": uid_destino,
                    "iniciadoPor": g.usuario["email"],
                    "fechaHora": marca_tiempo(),
                }
            )
        except Exception:
            pass
    return jsonify(ok=True, msg="Modo escucha RFID activado. Acerque la tarjeta al lector.")


@app.route("/api/rfid/estado")
@require_auth
def api_rfid_estado():
    # La pagina consulta aqui el estado del RFID.
    # PRIVACIDAD: el admin ve el estado global; el alumno SOLO ve lo que es suyo.
    usuario = g.usuario
    with rfid_lock:
        r = dict(rfid_estado)

    if usuario["rol"] == "admin":
        return jsonify(
            ok=True,
            activo=r["activo"],
            uidDestino=r["uidDestino"],
            nombreDestino=r["nombreDestino"],
            ultimaTarjeta=r["ultimaTarjeta"],
            msg=r["msg"],
            ultimoEvento=r["ultimoEvento"],
            rfid=r,
        )

    # ALUMNO: nunca devolvemos tarjetas ni nombres de otros usuarios.
    mi_uid = usuario["uid"]
    if r["activo"] and r["uidDestino"] == mi_uid:
        # Esta escuchando para enlazar SU tarjeta.
        return jsonify(ok=True, activo=True, uidDestino=mi_uid,
                       ultimaTarjeta=None, msg="Acerque la tarjeta al lector RFID...")
    if (not r["activo"]) and r["ultimoEnlaceUid"] == mi_uid:
        # El ultimo enlace fue el suyo.
        return jsonify(ok=True, activo=False, uidDestino=mi_uid,
                       ultimaTarjeta=r["ultimoEnlaceTarjeta"],
                       msg="Tarjeta enlazada correctamente.")
    # No hay nada relacionado con este alumno: respuesta vacia.
    return jsonify(ok=True, activo=False, uidDestino=None, ultimaTarjeta=None, msg="")


@app.route("/api/rfid/asignar", methods=["POST"])
@require_admin
def api_rfid_asignar():
    # Simula que llego una tarjeta RFID (sirve para probar sin hardware).
    # Hace lo mismo que cuando llega por MQTT: enlaza o valida la tarjeta.
    datos = request.get_json(silent=True) or {}
    uid_tarjeta = (datos.get("uidTarjeta") or "").strip()
    if not uid_tarjeta:
        return jsonify(ok=False, error="Falta el uidTarjeta"), 400
    procesar_rfid(uid_tarjeta)
    with rfid_lock:
        return jsonify(ok=True, rfid=dict(rfid_estado))


@app.route("/api/usuarios/<uid>/actualizar-rostro", methods=["POST"])
@require_auth
def api_actualizar_rostro(uid):
    # Actualiza el rostro de un usuario (alumno solo el suyo; admin a cualquiera).
    # Acepta varias fotos y un modo: "replace" (reemplaza) o "append" (agrega).
    datos = request.get_json(silent=True) or {}
    fotos = normalizar_lista_fotos(datos)
    modo = datos.get("modo", "replace")
    return actualizar_rostro_usuario(g.usuario, uid, fotos, modo)


@app.route("/api/registrar-rostro", methods=["POST"])
@require_auth
def api_registrar_rostro():
    # Ruta anterior; sigue funcionando y usa la misma logica con validacion.
    usuario = g.usuario
    datos = request.get_json(silent=True) or {}
    uid_objetivo = (datos.get("uid") or usuario["uid"]).strip()
    fotos = normalizar_lista_fotos(datos)
    modo = datos.get("modo", "replace")
    return actualizar_rostro_usuario(usuario, uid_objetivo, fotos, modo)


@app.route("/api/alumnos/<uid>", methods=["PUT"])
@require_admin
def api_editar_alumno(uid):
    # Solo el admin edita los datos del alumno (el alumno no puede).
    datos = request.get_json(silent=True) or {}
    nombres = (datos.get("nombres") or "").strip()
    apellidos = (datos.get("apellidos") or "").strip()
    carrera = (datos.get("carrera") or "").strip()
    semestre = str(datos.get("semestre") or "").strip()
    activo = bool(datos.get("activo", True))
    if not nombres or not apellidos:
        return jsonify(ok=False, error="Nombre y apellidos son obligatorios"), 400
    if not carrera:
        return jsonify(ok=False, error="La carrera es obligatoria"), 400
    if not semestre:
        return jsonify(ok=False, error="El semestre es obligatorio"), 400
    if db is None:
        return jsonify(ok=False, error="Firebase no disponible"), 503
    try:
        db.collection("usuarios").document(uid).set({
            "nombres": nombres,
            "apellidos": apellidos,
            "nombreCompleto": f"{nombres} {apellidos}".strip(),
            "carrera": carrera,
            "semestre": semestre,
            "activo": activo,
            "actualizadoEn": marca_tiempo(),
        }, merge=True)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    return jsonify(ok=True, msg="Datos del alumno actualizados")


@app.route("/api/alumnos/<uid>/desactivar", methods=["POST"])
@require_admin
def api_desactivar_alumno(uid):
    return cambiar_activo(uid, False)


@app.route("/api/alumnos/<uid>/reactivar", methods=["POST"])
@require_admin
def api_reactivar_alumno(uid):
    return cambiar_activo(uid, True)


def serializar_fecha(data):
    # Convierte la fecha de Firestore a texto para poder mandarla como JSON.
    fecha = data.get("fechaHora")
    if fecha is not None and hasattr(fecha, "isoformat"):
        data["fechaHora"] = fecha.isoformat()
    return data


# INICIO DEL SERVIDOR
def actualizar_estado_firestore_loop():
    # Cada cierto tiempo guarda el estado del sistema en Firestore para que el
    # admin lo vea tambien desde la consola de Firebase.
    while True:
        if db is not None:
            try:
                with estado_lock:
                    snapshot = dict(estado)
                db.collection("estado_sistema").document("status").set(
                    {
                        "servidorFlask": snapshot["servidorFlask"],
                        "firebase": snapshot["firebase"],
                        "mqtt": snapshot["mqtt"],
                        "camara": snapshot["camara"],
                        "cerradura": snapshot["cerradura"],
                        "ultimaActualizacion": marca_tiempo(),
                    }
                )
            except Exception:
                pass
        time.sleep(20)


if __name__ == "__main__":
    print("=" * 50)
    print(" SecurityBike AI - Servidor Central")
    print("=" * 50)
    inicializar_firebase()
    inicializar_mqtt()
    camara.iniciar()

    if DeepFace is not None:
        threading.Thread(target=reconocimiento_loop, daemon=True).start()
    else:
        print("[WARN] DeepFace no disponible. El video funciona, pero sin reconocimiento facial.")

    threading.Thread(target=actualizar_estado_firestore_loop, daemon=True).start()

    print(f"[OK] Pagina:  http://127.0.0.1:{CONFIG['puerto_servidor']}")
    print(f"[OK] Status:  http://127.0.0.1:{CONFIG['puerto_servidor']}/api/status")
    print(f"[INFO] Camara IP ({CONFIG.get('tipo_camara', 'DroidCam')}): {CONFIG['url_camara']}")
    print(f"[INFO] Cerradura UDP respaldo: {CONFIG['ip_cerradura_udp']}:{CONFIG['puerto_udp']}")
    app.run(
        host=CONFIG["host"],
        port=int(CONFIG["puerto_servidor"]),
        debug=bool(CONFIG["debug"]),
        threaded=True,
    )
