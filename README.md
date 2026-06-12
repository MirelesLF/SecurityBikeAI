# SecurityBike AI

Este es nuestro proyecto final. Hicimos un sistema inteligente para controlar el
acceso a las bicicletas: la persona puede entrar con su tarjeta RFID, con su rostro
(usando inteligencia artificial) o con un boton desde la pagina web. Cuando el
sistema reconoce a un usuario valido, abre una cerradura electrica y guarda el
registro de entrada o salida en Firebase.

## Integrantes

- LUIS FERNANDO MIRELES RODRIGUEZ (23240002)
- JOSE RODOLFO LOPEZ TORRES (22240496)
- VICTOR MANUEL ORTEGA GOMEZ (23240031)

Carrera: Ingenieria en Sistemas Computacionales

## Que queriamos lograr (objetivo)

Queriamos hacer un control de acceso seguro que abra la cerradura de tres formas
(tarjeta RFID, reconocimiento facial o desde la web), que guarde los usuarios y los
accesos en Firebase, y que tenga permisos distintos para los alumnos y para el
administrador.

## Como funciona nuestro sistema

- El **ESP32 actuador** lee las tarjetas RFID, controla la cerradura (solenoide), el
  buzzer y la pantalla OLED, y ademas mide el ambiente con un sensor DHT11 (temperatura
  y humedad) y un LDR (para saber si es de dia o de noche).
- El **servidor en Python (Flask)** muestra la pagina web, recibe el video de la camara,
  reconoce los rostros con DeepFace, revisa los usuarios en Firebase y decide cuando abrir.
- El servidor y el ESP32 se comunican por **MQTT** (y dejamos UDP como respaldo).
- Como **camara** usamos un telefono con la app DroidCam, que funciona como camara IP
  por Wi-Fi.
- Tambien dejamos el modulo **ESP32_CAM** como respaldo experimental, porque fue parte
  del desarrollo original; en el prototipo final preferimos DroidCam porque nos dio mejor
  estabilidad y rendimiento para el reconocimiento facial.

## Tecnologias que usamos

- ESP32 con MicroPython
- Python con Flask
- Firebase (Authentication y Firestore)
- MQTT (broker `test.mosquitto.org`)
- OpenCV y DeepFace para la IA
- HTML, CSS y JavaScript para la pagina

## Como conectamos el ESP32

```text
Buzzer       -> GPIO27
Solenoide    -> GPIO25
OLED SDA     -> GPIO21
OLED SCL     -> GPIO22
RFID SCK     -> GPIO18
RFID MOSI    -> GPIO23
RFID MISO    -> GPIO19
RFID RST     -> GPIO4
RFID CS      -> GPIO5
DHT11 DATA   -> GPIO32
LDR A0       -> GPIO34
VCC sensores/OLED -> 3V3
GND sensores/OLED -> GND
```

## Como instalar el proyecto

Primero creamos el entorno de Python e instalamos las librerias:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Como lo configuramos

- Tomamos como referencia `Servidor/firebase_config.example.json` y colocamos nuestra
  llave real de Firebase localmente como `Servidor/tu_llave_firebase.json` (esa llave
  NO se sube al repositorio).
- En `Servidor/config.json` ajustamos la URL de la camara (la de DroidCam), el broker
  MQTT y los `admin_emails` (los correos que entran como administrador).

## Como correr el servidor

```powershell
cd Servidor
python centro_control.py
```

Y abrimos la pagina en: http://127.0.0.1:5000

## Como cargamos el ESP32

Con Thonny subimos al ESP32 estos archivos y los guardamos como `hal.py` y `main.py`:

- `ESP32_ACTUADOR/hal.py`
- `ESP32_ACTUADOR/main.py`

## Que puede hacer el sistema

- Iniciar sesion y manejar roles (admin y alumno).
- Abrir la cerradura por la web, por tarjeta RFID y por reconocimiento facial.
- Avisar cuando un rostro no es reconocido (no abre y suena una alerta).
- Registrar las entradas y salidas con una foto de evidencia.
- Mostrar en la OLED la temperatura, la humedad y si es de dia o de noche.

## Nota de privacidad

Cuidamos los datos del sistema: la llave real de Firebase no la subimos al repositorio,
y los rostros y las fotos de evidencia se generan solo localmente cuando corremos el
sistema, asi que tampoco se suben a GitHub.
