# Proyecto: SecurityBike AI - Camara ESP32-S3-CAM (Streamer de Video)
#
# Integrantes:
# - LUIS FERNANDO MIRELES RODRIGUEZ (23240002)
# - JOSE RODOLFO LOPEZ TORRES (22240496)
# - VICTOR MANUEL ORTEGA GOMEZ (23240031)
#
# Carrera: Ingenieria en Sistemas Computacionales
#
# Objetivo del programa:
# Este ESP32-CAM toma video y lo transmite por la red en formato MJPEG. El
# servidor de IA (centro_control.py) se conecta a este video para reconocer
# rostros. Tambien se puede ver directo en el navegador en http://IP/stream
# ======================

import network
import socket
import time
import camera


# CONFIGURACION DE RED
SSID = "DELL"          
PASSWORD = "123456789"  


# CONFIGURACION DE LA CAMARA
def inicializar_camara():
    # Inicializa la camara.
    try:
        camera.init()
    except Exception as e:
        print("[ERROR] No se pudo iniciar la camara:", e)
        print("Revisa el cable, la alimentacion y que sea el modelo correcto.")
        return False

    # Intentamos bajar la resolucion a QVGA para que el video sea mas fluido.
    # Distintos firmwares usan nombres distintos, por eso van con try/except.
    try:
        camera.framesize(camera.FRAME_QVGA)
    except Exception:
        try:
            camera.framesize(camera.FRAMESIZE_QVGA)
        except Exception:
            pass  # si no existe la constante, se usa la resolucion por defecto
    try:
        camera.quality(15)
    except Exception:
        pass

    print("[OK] Camara inicializada correctamente.")
    return True


# CONEXION WI-FI
def conectar_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(SSID, PASSWORD)
        print("Conectando al Wi-Fi...", end="")
        while not wlan.isconnected():
            print(".", end="")
            time.sleep(0.5)
    ip = wlan.ifconfig()[0]
    print("\n[OK] Conectado. IP:", ip)
    print("URL de Stream: http://" + ip + "/stream")
    return ip


# RUTAS WEB (servidor MJPEG)
def iniciar_servidor(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((ip, 80))
    s.listen(5)
    print("[OK] Servidor de video activo en el puerto 80.")

    while True:
        conn, addr = s.accept()
        try:
            req = conn.recv(1024)

            if b"GET /stream" in req:
                # Flujo de video continuo en formato MJPEG.
                conn.sendall(b"HTTP/1.1 200 OK\r\n")
                conn.sendall(b"Content-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n")
                while True:
                    buf = camera.capture()
                    if buf:
                        conn.sendall(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                        conn.sendall(buf)
                        conn.sendall(b"\r\n")
                        time.sleep(0.05)  # ~20 FPS
            else:
                # Cualquier otra ruta muestra un mensaje simple.
                cuerpo = "SecurityBike AI - Camara activa. Ir a /stream"
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                conn.sendall(cuerpo.encode())
        except Exception:
            pass  # ignoramos caidas de cliente para no tumbar el servidor
        finally:
            conn.close()


# INICIO DEL PROGRAMA
if inicializar_camara():
    ip = conectar_wifi()
    iniciar_servidor(ip)
else:
    print("[ERROR] La camara no inicio. Revisa el hardware y reinicia.")
