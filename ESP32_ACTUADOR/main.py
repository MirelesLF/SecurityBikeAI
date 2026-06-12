# Proyecto: SecurityBike AI - Actuador de la Cerradura (ESP32)
#
# Integrantes:
# - LUIS FERNANDO MIRELES RODRIGUEZ (23240002)
# - JOSE RODOLFO LOPEZ TORRES (22240496)
# - VICTOR MANUEL ORTEGA GOMEZ (23240031)
#
# Carrera: Ingenieria en Sistemas Computacionales
#
# Objetivo del programa:
# Este ESP32 controla la cerradura. Se conecta al Wi-Fi, muestra su IP en la
# OLED y se conecta al broker MQTT. Cuando lee una tarjeta RFID la publica para
# que el servidor decida; y cuando el servidor manda la orden "ABRIR", abre la
# cerradura 5 segundos. Como respaldo tambien escucha por UDP en el puerto 5051.
#
# ======================

from hal import HardwareInterface
import network
import socket
import time
import json

# Intentamos cargar la libreria MQTT de MicroPython. Si no esta, el actuador
# seguira funcionando solo con UDP de respaldo.
try:
    from umqtt.simple import MQTTClient
    HAY_MQTT = True
except Exception as e:
    HAY_MQTT = False
    print("[MQTT] umqtt.simple no disponible, se usara solo UDP:", e)


# CONFIGURACION GENERAL
WIFI_SSID = "DELL"         
WIFI_PASS = "123456789"       
PUERTO_UDP = 5051

# CONFIGURACION MQTT
MQTT_BROKER = "test.mosquitto.org"
MQTT_PORT = 1883
MQTT_CLIENT_ID = "securitybike_actuador"
TOPICO_COMANDO = b"securitybike/acceso/comando"     # se suscribe
TOPICO_RFID = b"securitybike/actuador/rfid"         # publica tarjetas
TOPICO_STATUS = b"securitybike/actuador/status"     # publica que esta vivo
TOPICO_SENSORES = b"securitybike/sensores/ambiente" # publica DHT11 + LDR

# Inicializacion del hardware
hw = HardwareInterface()
cliente_mqtt = None


# CONEXION WI-FI
def conectar_wifi():
    hw.display_message("Conectando", "Wi-Fi...")
    red = network.WLAN(network.STA_IF)
    red.active(True)
    if not red.isconnected():
        red.connect(WIFI_SSID, WIFI_PASS)
        while not red.isconnected():
            time.sleep(0.5)
    ip = red.ifconfig()[0]
    hw.print_status(ip)
    hw.display_message("SISTEMA ONLINE", "IP:", ip)
    time.sleep(2)
    return ip


# NORMALIZACION DEL UID
def normalizar_uid(valor):
    # Convierte la lectura del RFID a un solo formato: "B6 C7 40 07".
    if isinstance(valor, (list, tuple)):
        return " ".join("{:02X}".format(b) for b in valor)
    texto = str(valor)
    for basura in ("[", "]", "(", ")", "0x", "0X"):
        texto = texto.replace(basura, "")
    texto = texto.replace(",", " ")
    return " ".join(p.upper() for p in texto.split())


# SONIDOS SEGUROS
# Si el hal.py de la placa es viejo y no tiene beep_ok/beep_error, usamos
# play_sound como respaldo. El sonido NUNCA debe impedir abrir la cerradura.
def sonido_ok_seguro():
    try:
        if hasattr(hw, "beep_ok"):
            hw.beep_ok()
        elif hasattr(hw, "play_sound"):
            hw.play_sound(2)
    except Exception as e:
        print("[AVISO] Fallo el buzzer (OK):", e)


def sonido_error_seguro():
    try:
        if hasattr(hw, "beep_error"):
            hw.beep_error()
        elif hasattr(hw, "play_sound"):
            hw.play_sound(3)
    except Exception as e:
        print("[AVISO] Fallo el buzzer (ERROR):", e)


# PANTALLA EN REPOSO (con ambiente)
def mostrar_reposo():
    # Pantalla normal: estado + saludo (dia/noche) + temperatura/humedad.
    try:
        hw.display_status_ambiente("SecurityBike AI", "Sistema bloqueado")
    except Exception:
        pass


# PROCESAMIENTO DE COMANDOS
def ejecutar_apertura(metodo):
    # Abre la cerradura, espera y la vuelve a cerrar.
    # El sonido va en try/except: si el buzzer falla, la cerradura abre igual.
    print("[LOG] Acceso concedido:", metodo)
    try:
        hw.display_status_ambiente("ACCESO OK", str(metodo)[:16])
    except Exception:
        pass
    sonido_ok_seguro()
    hw.set_lock(True)   # abrir (esto siempre se ejecuta)
    time.sleep(5)       # tiempo abierto
    hw.set_lock(False)  # cerrar
    mostrar_reposo()


def callback_mqtt(topico, mensaje):
    # Llega un mensaje MQTT del servidor. El servidor ya decidio que hacer:
    #   ABRIR      -> beep de confirmacion y abre la cerradura
    #   BEEP_OK    -> beep de confirmacion (tarjeta enlazada), NO abre
    #   BEEP_ERROR -> beep de alerta (tarjeta denegada), NO abre
    # Todo va dentro de try/except para que un error NO tumbe la conexion MQTT.
    print("[MQTT] Mensaje:", topico, mensaje)
    try:
        try:
            datos = json.loads(mensaje)
            accion = datos.get("accion", "")
            origen = datos.get("origen", "")
        except Exception:
            accion = mensaje.decode() if isinstance(mensaje, bytes) else str(mensaje)
            origen = ""
        accion = accion.upper()

        if "ABRIR" in accion:
            ejecutar_apertura(origen or "MQTT")
        elif "BEEP_OK" in accion:
            sonido_ok_seguro()
            try:
                hw.display_status_ambiente("TARJETA", "ENLAZADA")
            except Exception:
                pass
            time.sleep(1)
            mostrar_reposo()
        elif "BEEP_ERROR" in accion:
            sonido_error_seguro()
            try:
                hw.display_status_ambiente("ACCESO DENEGADO", str(origen)[:16] if origen else "")
            except Exception:
                pass
            time.sleep(1)
            mostrar_reposo()
    except Exception as e:
        print("[ERROR] Fallo procesando comando MQTT:", e)
        try:
            hw.display_message("ERROR", "MQTT CMD")
        except Exception:
            pass


# CONFIGURACION MQTT (conexion)
def conectar_mqtt():
    global cliente_mqtt
    if not HAY_MQTT:
        return None
    try:
        cliente = MQTTClient(MQTT_CLIENT_ID, MQTT_BROKER, MQTT_PORT)
        cliente.set_callback(callback_mqtt)
        cliente.connect()
        cliente.subscribe(TOPICO_COMANDO)
        cliente.publish(TOPICO_STATUS, json.dumps({"estado": "online"}))
        print("[MQTT] Conectado y suscrito a", TOPICO_COMANDO)
        cliente_mqtt = cliente
        return cliente
    except Exception as e:
        print("[MQTT] No se pudo conectar:", e)
        return None


def publicar_rfid(uid_tarjeta):
    # Publica la tarjeta leida para que el servidor decida si abre o no.
    if cliente_mqtt is None:
        return
    try:
        payload = json.dumps({"uidTarjeta": uid_tarjeta})
        cliente_mqtt.publish(TOPICO_RFID, payload)
        print("[MQTT] RFID publicado:", uid_tarjeta)
    except Exception as e:
        print("[MQTT] Error al publicar RFID:", e)


def publicar_ambiente():
    # Lee DHT11 + LDR y publica el ambiente por MQTT. Un fallo de sensor NO debe
    # tumbar nada (leer_ambiente ya maneja errores por dentro).
    try:
        amb = hw.leer_ambiente()
    except Exception as e:
        print("[SENSORES] Error leyendo ambiente:", e)
        return None
    if cliente_mqtt is not None:
        try:
            cliente_mqtt.publish(TOPICO_SENSORES, json.dumps(amb))
            print("[MQTT] Ambiente publicado:", amb)
        except Exception as e:
            print("[MQTT] Error al publicar ambiente:", e)
    return amb


# INICIO DEL ACTUADOR
ip = conectar_wifi()
conectar_mqtt()

# Socket UDP de respaldo (por si MQTT falla)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", PUERTO_UDP))
sock.setblocking(False)

mostrar_reposo()
ultimo_heartbeat = time.time()
ultimo_ambiente = 0

# PROCESAMIENTO Y SALIDA (bucle principal)
while True:
    # 1. Revisar mensajes MQTT (no bloqueante)
    if cliente_mqtt is not None:
        try:
            cliente_mqtt.check_msg()
        except Exception as e:
            print("[MQTT] Se perdio la conexion, reconectando:", e)
            conectar_mqtt()

    # 2. Revisar respaldo UDP (IA / servidor)
    try:
        datos, _ = sock.recvfrom(1024)
        if datos.decode("utf-8") == "ABRIR_PUERTA":
            ejecutar_apertura("UDP")
    except OSError:
        pass

    # 3. Revisar lector RFID fisico. Solo publica el UID: el servidor decide.
    uid = hw.get_rfid_uid()
    if uid:
        uid = normalizar_uid(uid)
        publicar_rfid(uid)
        try:
            hw.display_message("Tarjeta leida", uid)
        except Exception:
            pass
        time.sleep(1)
        mostrar_reposo()

    # 4. Mandar "sigo vivo" al servidor cada 10 segundos
    if cliente_mqtt is not None and time.time() - ultimo_heartbeat > 10:
        try:
            cliente_mqtt.publish(TOPICO_STATUS, json.dumps({"estado": "online"}))
        except Exception:
            pass
        ultimo_heartbeat = time.time()

    # 5. Leer y publicar sensores (DHT11 + LDR) cada 12 segundos y refrescar OLED.
    if time.time() - ultimo_ambiente > 12:
        publicar_ambiente()
        mostrar_reposo()
        ultimo_ambiente = time.time()

    time.sleep(0.1)
