# Proyecto: SecurityBike AI - Capa HAL del Actuador (ESP32)
#
# Integrantes:
# - LUIS FERNANDO MIRELES RODRIGUEZ (23240002)
# - JOSE RODOLFO LOPEZ TORRES (22240496)
# - VICTOR MANUEL ORTEGA GOMEZ (23240031)
#
# Carrera: Ingenieria en Sistemas Computacionales
#
# Objetivo del programa:
# La HAL (Capa de Abstraccion de Hardware) junta en un solo lugar el control de
# los componentes fisicos del actuador: el buzzer, la cerradura (solenoide), la
# pantalla OLED y el lector RFID. Asi el programa principal (main.py) solo llama
# funciones claras como set_lock() o get_rfid_uid() sin tocar los pines.
# ======================

from machine import Pin, SoftI2C, ADC
import ssd1306
from mfrc522 import MFRC522
import dht
import time

# CONFIGURACION DE SENSORES DE AMBIENTE (pines finales)
TIPO_DHT = "DHT11"
PIN_DHT = 32          # DHT11 DATA -> GPIO32
PIN_LDR = 34          # LDR A0 -> GPIO34 (solo entrada, ideal para ADC)
UMBRAL_LUZ = 2000     # valorLuz >= UMBRAL_LUZ -> dia; si no -> noche
                      # (si el modulo invierte la logica, cambiar a <= aqui)


class HardwareInterface:
    def __init__(self):
        # CONFIGURACION DE PINES
        # Buzzer y solenoide como salidas.
        self.buzzer = Pin(27, Pin.OUT)
        self.solenoide = Pin(25, Pin.OUT)
        self.solenoide.value(1)  # Logica inversa: 1 = bloqueado (apagado)

        # CONFIGURACION DE PERIFERICOS
        # Envolvemos OLED y RFID en try/except para que el actuador siga vivo
        # aunque alguno no este conectado al momento de encender.
        self.oled = None
        self.lector = None
        try:
            i2c = SoftI2C(scl=Pin(22), sda=Pin(21))
            self.oled = ssd1306.SSD1306_I2C(128, 64, i2c)
        except Exception as e:
            print("[HAL] Aviso: no se encontro la pantalla OLED:", e)
        try:
            self.lector = MFRC522(sck=18, mosi=23, miso=19, rst=4, cs=5)
        except Exception as e:
            print("[HAL] Aviso: no se encontro el lector RFID:", e)

        # SENSORES DE AMBIENTE (DHT11 en GPIO32, LDR en GPIO34). Si fallan, el
        # sistema sigue vivo (no deben tumbar MQTT, RFID ni cerradura).
        self.dht_sensor = None
        self.ldr_adc = None
        self.temperatura = None
        self.humedad = None
        self.valor_luz = None
        self.es_dia = True
        try:
            self.dht_sensor = dht.DHT11(Pin(PIN_DHT))
        except Exception as e:
            print("[HAL] Aviso: no se inicializo el DHT11:", e)
        try:
            self.ldr_adc = ADC(Pin(PIN_LDR))  # GPIO34 es solo entrada
            try:
                self.ldr_adc.atten(ADC.ATTN_11DB)  # rango ~0-3.3V si el firmware lo soporta
            except Exception:
                pass
        except Exception as e:
            print("[HAL] Aviso: no se inicializo el LDR:", e)

        print("[HAL] Hardware inicializado.")

    # CONTROL DE LA CERRADURA
    def set_lock(self, estado):
        # True = abrir (0), False = cerrar (1)
        self.solenoide.value(0 if estado else 1)

    # CONTROL DEL BUZZER
    def play_sound(self, veces=1):
        # Se conserva para compatibilidad con el codigo anterior.
        for _ in range(veces):
            self.buzzer.value(1)
            time.sleep(0.3)
            self.buzzer.value(0)
            time.sleep(0.1)

    def beep_ok(self):
        # Dos sonidos cortos de confirmacion (acceso permitido o enlace correcto).
        for _ in range(2):
            self.buzzer.value(1)
            time.sleep(0.12)
            self.buzzer.value(0)
            time.sleep(0.08)

    def beep_error(self):
        # Tres sonidos rapidos de alerta (tarjeta no registrada / acceso denegado).
        for _ in range(3):
            self.buzzer.value(1)
            time.sleep(0.25)
            self.buzzer.value(0)
            time.sleep(0.12)

    # CONTROL DE LA PANTALLA OLED
    def display_message(self, l1, l2="", l3=""):
        if self.oled is None:
            print("[OLED]", l1, l2, l3)
            return
        self.oled.fill(0)
        self.oled.text(l1, 0, 10)
        self.oled.text(l2, 0, 30)
        self.oled.text(l3, 0, 50)
        self.oled.show()

    # LECTURA DE SENSORES DE AMBIENTE
    def leer_dht(self):
        # Devuelve (temperatura, humedad) o (None, None) si falla.
        if self.dht_sensor is None:
            return None, None
        try:
            self.dht_sensor.measure()
            self.temperatura = self.dht_sensor.temperature()
            self.humedad = self.dht_sensor.humidity()
            return self.temperatura, self.humedad
        except Exception as e:
            print("[HAL] Aviso: fallo lectura DHT11:", e)
            return None, None

    def leer_ldr(self):
        # Devuelve (valor_luz, es_dia). Si falla, asume de dia.
        if self.ldr_adc is None:
            return None, True
        try:
            self.valor_luz = self.ldr_adc.read()
            self.es_dia = self.valor_luz >= UMBRAL_LUZ
            return self.valor_luz, self.es_dia
        except Exception as e:
            print("[HAL] Aviso: fallo lectura LDR:", e)
            return None, True

    def obtener_saludo(self):
        # Saludo segun la luz. Si no hay LDR, saludo neutro.
        if self.ldr_adc is None:
            return "Bienvenido"
        return "Buenos dias" if self.es_dia else "Buenas noches"

    def leer_ambiente(self):
        # Lee ambos sensores y arma un diccionario con todo el ambiente.
        temp, hum = self.leer_dht()
        luz, dia = self.leer_ldr()
        return {
            "temperatura": temp,
            "humedad": hum,
            "valorLuz": luz,
            "esDia": dia,
            "saludo": self.obtener_saludo(),
        }

    def display_status_ambiente(self, titulo, subtitulo=""):
        # Muestra estado + saludo + temperatura/humedad en la OLED (4 lineas).
        amb = self.leer_ambiente()
        t = amb["temperatura"]
        h = amb["humedad"]
        linea_th = "T: {}C H: {}%".format(t if t is not None else "--", h if h is not None else "--")
        if self.oled is None:
            print("[OLED]", titulo, subtitulo, amb["saludo"], linea_th)
            return
        self.oled.fill(0)
        self.oled.text(titulo[:16], 0, 0)
        self.oled.text(subtitulo[:16], 0, 16)
        self.oled.text(amb["saludo"][:16], 0, 34)
        self.oled.text(linea_th[:16], 0, 50)
        self.oled.show()

    # LECTURA DEL RFID
    def get_rfid_uid(self):
        # Devuelve el UID de la tarjeta como texto, o None si no hay tarjeta.
        if self.lector is None:
            return None
        (stat, _) = self.lector.request(self.lector.REQIDL)
        if stat == self.lector.OK:
            (stat, uid) = self.lector.SelectTagSN()
            if stat == self.lector.OK:
                return self.lector.tohexstring(uid)
        return None

    # REPORTE EN CONSOLA
    def print_status(self, ip_address):
        print("\n" + "=" * 40)
        print("SISTEMA OPERATIVO: INICIADO")
        print("IP ASIGNADA:", ip_address)
        print("ESTADO: MONITOREANDO RFID, MQTT Y UDP")
        print("=" * 40 + "\n")
