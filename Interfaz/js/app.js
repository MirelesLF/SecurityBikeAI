/*
  Proyecto: SecurityBike AI - Logica de la Interfaz (app.js)

  Integrantes:
  - LUIS FERNANDO MIRELES RODRIGUEZ (23240002)
  - JOSE RODOLFO LOPEZ TORRES (22240496)
  - VICTOR MANUEL ORTEGA GOMEZ (23240031)

  Carrera: Ingenieria en Sistemas Computacionales

  Objetivo del programa:
  Conectar la pagina con Firebase Auth y con el servidor Flask. Maneja login,
  registro con captura de rostro, cierre de sesion limpio, el menu de navegacion,
  los badges de estado, abrir la cerradura y el flujo completo de tarjetas RFID
  segun el rol (alumno o administrador).
*/

// ====================== //
// CONFIGURACION DE FIREBASE //
// ====================== //
// Nota: la apiKey web de Firebase es publica por diseno (no es secreta).
const firebaseConfig = {
    apiKey: "AIzaSyBvN0KR92YxaKbypK6UVAil2KmA2aX4vwg",
    authDomain: "securitybikeia.firebaseapp.com",
    projectId: "securitybikeia",
    storageBucket: "securitybikeia.firebasestorage.app",
    messagingSenderId: "314207449872",
    appId: "1:314207449872:web:a7deefaacc045943bb2b95",
    measurementId: "G-RTFQJSN9RE"
};
firebase.initializeApp(firebaseConfig);
const auth = firebase.auth();
// Mantener la sesion al recargar la pagina.
auth.setPersistence(firebase.auth.Auth.Persistence.LOCAL).catch(() => {});

// Variables globales
let usuarioActual = null;
let streamLocal = null;     // camara del registro (navegador)
let fotosRegistro = [];     // varias fotos capturadas en el registro
const MAX_FOTOS = 5;        // maximo de fotos biometricas
let registrando = false;    // controla el flujo de registro
let pollStatus = null;      // intervalo de los badges
let pollRfid = null;        // intervalo del modo escuchar tarjeta
let cacheAccesos = [];      // historial guardado para filtrar sin recargar
let cacheMovimientos = [];  // entradas/salidas con evidencia
let streamRostro = null;    // camara del modal "actualizar rostro"
let rostroDestinoUid = null; // a quien se le actualiza el rostro
let fotosRostro = [];       // fotos capturadas en el modal de rostro
let rostroModo = "replace"; // "replace" o "append"
let detalleUid = null;      // alumno abierto en el modal de detalle

const $ = (id) => document.getElementById(id);

// Menus segun el rol
const MENU_ALUMNO = [
    { id: "sec-alumno-inicio", icono: "home", texto: "Inicio" },
    { id: "sec-camara", icono: "videocam", texto: "Camara" },
    { id: "sec-alumno-rfid", icono: "nfc", texto: "Mi tarjeta RFID" },
    { id: "sec-alumno-historial", icono: "history", texto: "Mi historial" },
    { id: "sec-entradas", icono: "swap_vert", texto: "Mis entradas/salidas" },
    { id: "sec-alumno-perfil", icono: "person", texto: "Mi perfil" },
];
const MENU_ADMIN = [
    { id: "sec-admin-dashboard", icono: "dashboard", texto: "Dashboard" },
    { id: "sec-admin-alumnos", icono: "groups", texto: "Alumnos" },
    { id: "sec-camara", icono: "videocam", texto: "Camara" },
    { id: "sec-admin-historial", icono: "history", texto: "Historial" },
    { id: "sec-entradas", icono: "swap_vert", texto: "Entradas y salidas" },
    { id: "sec-admin-rfid", icono: "nfc", texto: "RFID" },
    { id: "sec-admin-sistema", icono: "settings", texto: "Sistema" },
];


// ====================== //
// AYUDAS PARA LLAMAR A LA API //
// ====================== //
async function obtenerToken() {
    if (!auth.currentUser) throw new Error("Sesion no lista");
    return await auth.currentUser.getIdToken();
}

function ocultarCargando() {
    const c = $("cargando");
    if (c) c.classList.add("oculto");
}

async function api(ruta, opciones = {}) {
    const token = await obtenerToken();
    const resp = await fetch(ruta, {
        ...opciones,
        headers: {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            ...(opciones.headers || {})
        }
    });
    return await resp.json();
}

function mensajeAuth(texto, tipo) {
    const el = $("auth-mensaje");
    el.textContent = texto;
    el.className = "auth-mensaje " + (tipo || "");
}

function mostrarToast(texto, tipo) {
    const t = $("toast");
    t.textContent = texto;
    t.className = "toast " + (tipo || "");
    setTimeout(() => t.classList.add("oculto"), 3200);
}

function traducirError(error) {
    const mapa = {
        "auth/email-already-in-use": "Ese correo ya esta registrado.",
        "auth/invalid-email": "El correo no es valido.",
        "auth/weak-password": "La contrasena debe tener al menos 6 caracteres.",
        "auth/wrong-password": "Contrasena incorrecta.",
        "auth/user-not-found": "No existe una cuenta con ese correo.",
        "auth/invalid-credential": "Correo o contrasena incorrectos.",
        "auth/network-request-failed": "Error de red. Revisa tu internet."
    };
    return mapa[error.code] || error.message;
}


// ====================== //
// LIMPIEZA DE FORMULARIOS Y CAMARA //
// ====================== //
function limpiarFormularioLogin() {
    $("login-correo").value = "";
    $("login-password").value = "";
}

function limpiarFormularioRegistro() {
    ["reg-nombres", "reg-apellidos", "reg-carrera", "reg-correo", "reg-password"].forEach(id => $(id).value = "");
    $("reg-semestre").value = "";
}

function limpiarCapturaRostro() {
    fotosRegistro = [];
    $("video-registro").classList.add("oculto");
    $("camara-vacia").classList.remove("oculto");
    $("btn-capturar").disabled = true;
    $("btn-quitar-foto").disabled = true;
    $("reg-contador").textContent = "0";
    $("reg-miniaturas").innerHTML = "";
}

function renderMiniaturas(idContenedor, idContador, fotos) {
    $(idContenedor).innerHTML = fotos.map((f, i) =>
        `<div class="mini-foto"><span>${i + 1}</span><img src="${f}"></div>`).join("");
    if (idContador) $(idContador).textContent = fotos.length;
}

function detenerCamaraLocal() {
    if (streamLocal) {
        streamLocal.getTracks().forEach(t => t.stop());
        streamLocal = null;
    }
}


// ====================== //
// CAMBIO ENTRE LOGIN Y REGISTRO //
// ====================== //
function mostrarLogin() {
    $("tab-login").classList.add("activo");
    $("tab-registro").classList.remove("activo");
    $("form-login").classList.remove("oculto");
    $("form-registro").classList.add("oculto");
    // Al pasar a login limpiamos el registro y apagamos la camara.
    limpiarFormularioRegistro();
    limpiarCapturaRostro();
    detenerCamaraLocal();
    mensajeAuth("", "");
}

function mostrarRegistro() {
    $("tab-login").classList.remove("activo");
    $("tab-registro").classList.add("activo");
    $("form-login").classList.add("oculto");
    $("form-registro").classList.remove("oculto");
    // Empezar limpio, sin datos anteriores.
    limpiarFormularioRegistro();
    limpiarCapturaRostro();
    mensajeAuth("", "");
}

$("tab-login").onclick = mostrarLogin;
$("tab-registro").onclick = mostrarRegistro;


// ====================== //
// LOGIN CON CORREO         //
// ====================== //
$("form-login").onsubmit = async (e) => {
    e.preventDefault();
    mensajeAuth("Entrando...", "");
    try {
        await auth.signInWithEmailAndPassword($("login-correo").value, $("login-password").value);
    } catch (error) {
        mensajeAuth(traducirError(error), "error");
    }
};


// ====================== //
// CAPTURA DE ROSTRO        //
// ====================== //
$("btn-iniciar-camara").onclick = async () => {
    try {
        streamLocal = await navigator.mediaDevices.getUserMedia({
            video: { width: 480, height: 360 }, audio: false
        });
        const video = $("video-registro");
        video.srcObject = streamLocal;
        video.classList.remove("oculto");
        $("camara-vacia").classList.add("oculto");
        $("btn-capturar").disabled = false;
        mensajeAuth("Camara lista. Captura de 3 a 5 fotos.", "ok");
    } catch (error) {
        mensajeAuth("No se pudo abrir la camara: " + error.message, "error");
    }
};

$("btn-capturar").onclick = () => {
    if (fotosRegistro.length >= MAX_FOTOS) {
        mensajeAuth("Maximo " + MAX_FOTOS + " fotos.", "error");
        return;
    }
    const video = $("video-registro");
    const canvas = $("canvas-captura");
    canvas.width = 480;
    canvas.height = 360;
    canvas.getContext("2d").drawImage(video, 0, 0, 480, 360);
    fotosRegistro.push(canvas.toDataURL("image/jpeg", 0.7));  // calidad 0.7 para no pesar
    renderMiniaturas("reg-miniaturas", "reg-contador", fotosRegistro);
    $("btn-quitar-foto").disabled = false;
    mensajeAuth("Fotos capturadas: " + fotosRegistro.length + "/" + MAX_FOTOS, "ok");
};

$("btn-quitar-foto").onclick = () => {
    fotosRegistro.pop();
    renderMiniaturas("reg-miniaturas", "reg-contador", fotosRegistro);
    $("btn-quitar-foto").disabled = fotosRegistro.length === 0;
};


// ====================== //
// REGISTRO DE ALUMNO       //
// ====================== //
$("form-registro").onsubmit = async (e) => {
    e.preventDefault();
    if (fotosRegistro.length < 3) {
        mensajeAuth("Captura al menos 3 fotos para mejorar el reconocimiento.", "error");
        return;
    }
    const correo = $("reg-correo").value;
    const password = $("reg-password").value;
    mensajeAuth("Creando cuenta...", "");
    registrando = true;
    try {
        const cred = await auth.createUserWithEmailAndPassword(correo, password);
        const token = await cred.user.getIdToken();
        const resp = await fetch("/api/auth/register-profile", {
            method: "POST",
            headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json" },
            body: JSON.stringify({
                nombres: $("reg-nombres").value.trim(),
                apellidos: $("reg-apellidos").value.trim(),
                carrera: $("reg-carrera").value.trim(),
                semestre: $("reg-semestre").value,
                correo: correo,
                fotos: fotosRegistro
            })
        });
        const data = await resp.json();
        if (data.ok) {
            mensajeAuth("Cuenta creada correctamente (" + data.rol + ").", "ok");
            detenerCamaraLocal();
            registrando = false;
            iniciarDashboard();
        } else {
            mensajeAuth(data.error || "No se pudo guardar el perfil.", "error");
            registrando = false;
        }
    } catch (error) {
        mensajeAuth(traducirError(error), "error");
        registrando = false;
    }
};


// ====================== //
// CONTROL DE SESION        //
// ====================== //
auth.onAuthStateChanged((user) => {
    if (user && !registrando) {
        iniciarDashboard();
    } else if (!user) {
        resetAuthUI();
    }
});

function resetAuthUI() {
    // Deja la pantalla de autenticacion completamente limpia y en "Iniciar sesion".
    usuarioActual = null;
    if (pollStatus) { clearInterval(pollStatus); pollStatus = null; }
    if (pollRfid) { clearInterval(pollRfid); pollRfid = null; }
    detenerCamaraLocal();
    detenerCamaraRostro();
    detenerStream();
    limpiarFormularioLogin();
    limpiarFormularioRegistro();
    limpiarCapturaRostro();
    mostrarLogin();
    $("vista-dashboard").classList.add("oculto");
    $("vista-auth").classList.remove("oculto");
    ocultarCargando();
}

function cerrarSesion() {
    detenerCamaraLocal();
    detenerCamaraRostro();
    if (pollStatus) { clearInterval(pollStatus); pollStatus = null; }
    if (pollRfid) { clearInterval(pollRfid); pollRfid = null; }
    auth.signOut();   // onAuthStateChanged llamara a resetAuthUI()
}
$("btn-logout").onclick = cerrarSesion;


// ====================== //
// CARGA DEL DASHBOARD      //
// ====================== //
async function iniciarDashboard() {
    // Reintentamos /api/me unas veces: justo despues de iniciar sesion el token
    // a veces falla por desfase de reloj o por arranque en frio del servidor.
    let data = null;
    for (let intento = 0; intento < 4; intento++) {
        try {
            data = await api("/api/me");
            if (data && data.ok) break;
        } catch (e) {
            data = null;
        }
        await new Promise(r => setTimeout(r, 700));
    }
    if (!data || !data.ok) {
        resetAuthUI();
        mensajeAuth("No se pudo cargar tu perfil. Intenta iniciar sesion de nuevo.", "error");
        return;
    }
    usuarioActual = data.usuario;
    $("vista-auth").classList.add("oculto");
    $("vista-dashboard").classList.remove("oculto");
    ocultarCargando();

    $("usuario-nombre").textContent = usuarioActual.nombreCompleto || usuarioActual.correo || "Usuario";
    $("usuario-rol").textContent = usuarioActual.rol;

    if (usuarioActual.rol === "admin") {
        $("titulo-panel").textContent = "Sistema de Control de Acceso";
        $("subtitulo-panel").textContent = "Panel de control general";
        construirMenu(MENU_ADMIN);
    } else {
        $("titulo-panel").textContent = "Mi panel de acceso";
        $("subtitulo-panel").textContent = "Bienvenido, " + (usuarioActual.nombres || "alumno");
        construirMenu(MENU_ALUMNO);
    }

    iniciarStatusPolling();
}


// ====================== //
// MENU Y NAVEGACION        //
// ====================== //
function construirMenu(items) {
    const menu = $("menu");
    menu.innerHTML = "";
    items.forEach((item, i) => {
        const btn = document.createElement("button");
        btn.className = "menu-item" + (i === 0 ? " activo" : "");
        btn.dataset.seccion = item.id;
        btn.innerHTML = `<span class="material-icons-outlined">${item.icono}</span> ${item.texto}`;
        btn.onclick = () => mostrarSeccion(item.id);
        menu.appendChild(btn);
    });
    mostrarSeccion(items[0].id);  // mostrar la primera seccion por defecto
}

function mostrarSeccion(id) {
    // Oculta todas las secciones y muestra solo la elegida.
    document.querySelectorAll(".seccion").forEach(s => s.classList.add("oculto"));
    const sec = $(id);
    if (sec) sec.classList.remove("oculto");

    // Marca el boton activo del menu.
    document.querySelectorAll(".menu-item").forEach(b =>
        b.classList.toggle("activo", b.dataset.seccion === id));

    // La camara solo se conecta cuando se ve, para no gastar de mas.
    if (id === "sec-camara") iniciarStream(); else detenerStream();

    // Cargar datos frescos de cada seccion al entrar.
    if (id === "sec-alumno-inicio") cargarInicioAlumno();
    if (id === "sec-alumno-rfid") cargarMiTarjeta();
    if (id === "sec-alumno-historial") cargarMiHistorial();
    if (id === "sec-alumno-perfil") cargarMiPerfil();
    if (id === "sec-admin-dashboard") { cargarAlumnos(); cargarHistorialAdmin(); }
    if (id === "sec-admin-alumnos") cargarAlumnos();
    if (id === "sec-admin-historial") cargarHistorialAdmin();
    if (id === "sec-admin-sistema") cargarSistema();
    if (id === "sec-entradas") cargarEntradasSalidas();
}


// ====================== //
// STREAM DE LA CAMARA      //
// ====================== //
function iniciarStream() {
    $("stream-error").classList.add("oculto");
    const img = $("stream-camara");
    img.classList.remove("oculto");
    img.src = "/stream?t=" + Date.now();
}

function detenerStream() {
    const img = $("stream-camara");
    if (img) img.src = "";  // cierra la conexion MJPEG al salir de la camara
}


// ====================== //
// BADGES DE ESTADO         //
// ====================== //
function iniciarStatusPolling() {
    actualizarStatus();
    pollStatus = setInterval(actualizarStatus, 3000);
}

async function actualizarStatus() {
    try {
        const resp = await fetch("/api/status");
        const s = await resp.json();
        marcarBadge("badge-flask", s.servidorFlask);
        marcarBadge("badge-firebase", s.firebase);
        marcarBadge("badge-mqtt", s.mqtt);
        marcarBadge("badge-camara", s.camara);
        marcarBadge("badge-cerradura", s.cerradura);
        if (s.tipoCamara) $("badge-camara-texto").textContent = s.tipoCamara;
        $("nota-modo-ia").classList.toggle("oculto", !s.modoDemoIa);
        // Si el admin tiene abierta la seccion RFID, actualizamos su panel.
        if (usuarioActual && usuarioActual.rol === "admin" && !$("sec-admin-rfid").classList.contains("oculto")) {
            actualizarPanelRfid();
        }
    } catch (e) {
        marcarBadge("badge-flask", false);
    }
}

function marcarBadge(id, activo) {
    $(id).classList.toggle("activo", !!activo);
}


// ====================== //
// ABRIR CERRADURA          //
// ====================== //
document.querySelectorAll(".js-abrir").forEach(btn => btn.onclick = abrirCerradura);

async function abrirCerradura() {
    mostrarToast("Enviando orden de apertura...", "");
    try {
        const data = await api("/api/abrir", { method: "POST", body: "{}" });
        if (data.ok) mostrarToast("Cerradura abierta (" + (data.via || "OK") + ").", "ok");
        else mostrarToast(data.error || "No se pudo abrir.", "error");
    } catch (e) {
        mostrarToast("Error al abrir la cerradura.", "error");
    }
}


// ====================== //
// SECCIONES DEL ALUMNO     //
// ====================== //
async function cargarInicioAlumno() {
    const data = await api("/api/me");
    if (data.ok) usuarioActual = data.usuario;
    $("inicio-datos").innerHTML = filasDatos({
        "Nombre": usuarioActual.nombreCompleto,
        "Carrera": usuarioActual.carrera,
        "Semestre": usuarioActual.semestre,
        "Correo": usuarioActual.correo
    });
    $("inicio-rfid").textContent = usuarioActual.rfidUid || "Sin tarjeta enlazada";
}

async function cargarMiTarjeta() {
    // La tarjeta del alumno se basa en SUS datos (usuarioActual.rfidUid),
    // nunca en el estado global de RFID.
    const data = await api("/api/me");
    if (data.ok) usuarioActual = data.usuario;
    // Si no hay un enlace en curso, ocultamos el aviso para no dejar texto viejo.
    if (!pollRfid) $("mi-rfid-estado").classList.add("oculto");
    const tiene = !!usuarioActual.rfidUid;
    $("mi-rfid").textContent = tiene ? ("Tarjeta enlazada: " + usuarioActual.rfidUid) : "Sin tarjeta enlazada";
    $("btn-enlazar-mia").innerHTML = tiene
        ? '<span class="material-icons-outlined">cached</span> Reemplazar tarjeta'
        : '<span class="material-icons-outlined">add_card</span> Enlazar mi tarjeta';
}

async function cargarMiHistorial() {
    const data = await api("/api/accesos/mios");
    const cuerpo = $("tabla-mi-historial");
    if (!data.ok || !data.accesos || data.accesos.length === 0) {
        cuerpo.innerHTML = filaVacia(4, "Aun no tienes checadas registradas.");
        return;
    }
    cuerpo.innerHTML = data.accesos.map(a => `
        <tr>
            <td>${formatoFecha(a.fechaHora)}</td>
            <td><span class="etiqueta metodo">${a.metodo || "-"}</span></td>
            <td>${etiquetaEstado(a.estado)}</td>
            <td>${a.detalle || "-"}</td>
        </tr>`).join("");
}

async function cargarMiPerfil() {
    const data = await api("/api/me");
    if (data.ok) usuarioActual = data.usuario;
    $("perfil-datos").innerHTML = filasDatos({
        "Nombre": usuarioActual.nombreCompleto,
        "Carrera": usuarioActual.carrera,
        "Semestre": usuarioActual.semestre,
        "Correo": usuarioActual.correo,
        "Rol": usuarioActual.rol,
        "Tarjeta RFID": usuarioActual.rfidUid || "Sin enlazar"
    });
    if (usuarioActual.rostroBase64) $("mi-rostro").src = usuarioActual.rostroBase64;
    const bio = $("mi-biometrico");
    const n = usuarioActual.cantidadRostros || 0;
    if (usuarioActual.biometricoValido) {
        bio.textContent = "Biometrico activo: " + n + " foto" + (n === 1 ? "" : "s") + " registradas";
        bio.className = "mini-mensaje ok";
    } else {
        bio.textContent = "Biometrico no valido o pendiente de captura";
        bio.className = "mini-mensaje error";
    }
}

// Enlazar mi propia tarjeta (alumno)
// PRIVACIDAD: el alumno solo consulta SUS datos (/api/me). Detectamos el enlace
// cuando cambia su propio rfidUid. Asi nunca vemos tarjetas ni nombres de otros.
$("btn-enlazar-mia").onclick = async () => {
    const tarjetaAntes = usuarioActual.rfidUid || null;
    await api("/api/rfid/escuchar", { method: "POST", body: JSON.stringify({ uidDestino: usuarioActual.uid }) });
    $("mi-rfid-estado").classList.remove("oculto");
    $("mi-rfid-texto").textContent = "Acerque la tarjeta al lector RFID...";
    if (pollRfid) clearInterval(pollRfid);
    pollRfid = setInterval(async () => {
        const data = await api("/api/me");
        if (data.ok && data.usuario.rfidUid && data.usuario.rfidUid !== tarjetaAntes) {
            usuarioActual = data.usuario;
            $("mi-rfid-texto").textContent = "Tarjeta enlazada correctamente.";
            clearInterval(pollRfid);
            mostrarToast("Tarjeta enlazada correctamente.", "ok");
            cargarMiTarjeta();
        }
    }, 1500);
};


// ====================== //
// SECCIONES DEL ADMIN      //
// ====================== //
async function cargarAlumnos() {
    const data = await api("/api/alumnos");
    const cuerpo = $("tabla-alumnos");
    if (!data.ok || !data.alumnos || data.alumnos.length === 0) {
        cuerpo.innerHTML = filaVacia(9, "No hay alumnos registrados todavia.");
        $("m-alumnos").textContent = "0";
        $("m-tarjetas").textContent = "0";
        return;
    }
    const alumnos = data.alumnos;
    $("m-alumnos").textContent = alumnos.length;
    $("m-tarjetas").textContent = alumnos.filter(a => a.rfidUid).length;
    cuerpo.innerHTML = alumnos.map(al => {
        const nombre = (al.nombreCompleto || "Sin nombre").replace(/'/g, "");
        const accionTarjeta = al.rfidUid ? "Reemplazar tarjeta" : "Enlazar tarjeta";
        const activo = al.activo !== false;
        const bio = al.biometricoValido;
        // Boton desactivar/reactivar segun el estado.
        const btnEstado = activo
            ? `<button class="btn-claro" title="Desactivar" onclick="cambiarEstadoAlumno('${al.uid}', false)"><span class="material-icons-outlined">block</span></button>`
            : `<button class="btn-claro" title="Reactivar" onclick="cambiarEstadoAlumno('${al.uid}', true)"><span class="material-icons-outlined">check_circle</span></button>`;
        return `
        <tr>
            <td>${bio && al.rostroBase64 ? `<img class="foto-mini" src="${al.rostroBase64}">` : "-"}</td>
            <td>${al.nombreCompleto || "Sin nombre"}</td>
            <td>${al.carrera || "-"}</td>
            <td>${al.semestre || "-"}</td>
            <td>${al.correo || "-"}</td>
            <td>${al.rfidUid ? `<span class="etiqueta metodo">${al.rfidUid}</span>` : `<span class="etiqueta no">Sin tarjeta</span>`}</td>
            <td>${bio ? `<span class="etiqueta si">Si (${al.cantidadRostros || 0})</span>` : `<span class="etiqueta no">No</span>`}</td>
            <td>${activo ? `<span class="etiqueta si">Activo</span>` : `<span class="etiqueta denegado">Inactivo</span>`}</td>
            <td class="acciones-celda">
                <button class="btn-claro" title="Ver detalle" onclick="verDetalle('${al.uid}')">
                    <span class="material-icons-outlined">visibility</span>
                </button>
                <button class="btn-claro" title="Editar datos" onclick="abrirModalEditar('${al.uid}')">
                    <span class="material-icons-outlined">edit</span>
                </button>
                <button class="btn-claro" title="Actualizar rostro" onclick="abrirModalRostro('${al.uid}')">
                    <span class="material-icons-outlined">photo_camera</span>
                </button>
                <button class="btn-claro" title="${accionTarjeta}" onclick="abrirModalRfid('${al.uid}','${nombre}')">
                    <span class="material-icons-outlined">nfc</span>
                </button>
                ${btnEstado}
            </td>
        </tr>`;
    }).join("");
}

async function cargarHistorialAdmin() {
    const data = await api("/api/accesos");
    if (data.ok) {
        cacheAccesos = data.accesos || [];
        $("m-permitidos").textContent = cacheAccesos.filter(a => a.estado === "PERMITIDO").length;
        $("m-denegados").textContent = cacheAccesos.filter(a => a.estado === "DENEGADO").length;
        pintarHistorialAdmin();
    }
}

function pintarHistorialAdmin() {
    const fAlumno = $("filtro-alumno").value.toLowerCase();
    const fMetodo = $("filtro-metodo").value;
    const fEstado = $("filtro-estado").value;
    const lista = cacheAccesos.filter(a =>
        (!fAlumno || (a.nombreCompleto || "").toLowerCase().includes(fAlumno)) &&
        (!fMetodo || a.metodo === fMetodo) &&
        (!fEstado || a.estado === fEstado)
    );
    const cuerpo = $("tabla-historial");
    if (lista.length === 0) {
        cuerpo.innerHTML = filaVacia(5, "No hay registros con esos filtros.");
        return;
    }
    cuerpo.innerHTML = lista.map(a => `
        <tr>
            <td>${formatoFecha(a.fechaHora)}</td>
            <td>${a.nombreCompleto || "-"}</td>
            <td><span class="etiqueta metodo">${a.metodo || "-"}</span></td>
            <td>${etiquetaEstado(a.estado)}</td>
            <td>${a.detalle || "-"}</td>
        </tr>`).join("");
}

$("filtro-alumno").oninput = pintarHistorialAdmin;
$("filtro-metodo").onchange = pintarHistorialAdmin;
$("filtro-estado").onchange = pintarHistorialAdmin;
$("btn-refrescar").onclick = () => { cargarAlumnos(); cargarHistorialAdmin(); };

async function cargarSistema() {
    const resp = await fetch("/api/status");
    const s = await resp.json();
    const amb = s.ambiente || {};
    const tieneAmb = amb.temperatura !== undefined && amb.temperatura !== null;
    $("sistema-datos").innerHTML = filasDatos({
        "Servidor Flask": s.servidorFlask ? "Conectado" : "Desconectado",
        "Firebase": s.firebase ? "Conectado" : "Desconectado",
        "MQTT": s.mqtt ? "Conectado" : "Desconectado",
        "Tipo de camara": s.tipoCamara || "Camara IP",
        "Camara IP": s.camara ? "En linea" : "Sin senal",
        "Aviso camara": s.mensajeCamara || (s.camara ? "OK" : "-"),
        "Cerradura": s.cerradura ? "En linea" : "Sin senal",
        "URL de la camara": s.urlCamara,
        "Modo IA rapido": s.modoDemoIa ? "Activado" : "Normal",
        "Temperatura": tieneAmb ? (amb.temperatura + " C") : "Sin dato",
        "Humedad": tieneAmb ? (amb.humedad + " %") : "Sin dato",
        "Dia/Noche": amb.saludo ? amb.saludo : "Sin dato",
        "Broker MQTT": s.mqttBroker || "-",
        "Alumnos con rostro": s.alumnos
    });
}

// Panel RFID del admin (monitor)
async function actualizarPanelRfid() {
    // Solo el admin usa este panel; el backend le devuelve el estado global.
    const data = await api("/api/rfid/estado");
    if (!data.ok) return;
    $("rfid-panel-estado").textContent = data.activo ? "Escuchando tarjeta..." : "Inactivo";
    $("rfid-panel-destino").textContent = data.nombreDestino || "-";
    $("rfid-panel-ultima").textContent = data.ultimaTarjeta || "-";
    $("rfid-panel-evento").textContent = data.ultimoEvento || "-";
}

$("btn-panel-simular").onclick = async () => {
    const uid = $("rfid-panel-simular").value.trim();
    if (!uid) return;
    await api("/api/rfid/asignar", { method: "POST", body: JSON.stringify({ uidTarjeta: uid }) });
    mostrarToast("Tarjeta simulada enviada.", "ok");
    setTimeout(() => { actualizarPanelRfid(); cargarAlumnos(); }, 600);
};


// ====================== //
// DETALLE DE ALUMNO (modal) //
// ====================== //
async function verDetalle(uid) {
    const data = await api("/api/alumnos/" + uid);
    if (!data.ok) return;
    const a = data.alumno;
    detalleUid = uid;
    const bioValido = !!a.biometricoValido;
    // No mostramos cuadro negro como si fuera rostro valido.
    $("detalle-rostro").src = (bioValido && a.rostroBase64) ? a.rostroBase64 : "";
    const bio = $("detalle-biometrico");
    const n = a.cantidadRostros || 0;
    bio.textContent = bioValido ? ("Biometrico: Si (" + n + " foto" + (n === 1 ? "" : "s") + ")")
                                : "Biometrico no valido o pendiente de captura";
    bio.className = "mini-mensaje " + (bioValido ? "ok" : "error");
    $("detalle-datos").innerHTML = filasDatos({
        "Nombre": a.nombreCompleto,
        "Carrera": a.carrera,
        "Semestre": a.semestre,
        "Correo": a.correo,
        "Rol": a.rol,
        "Tarjeta RFID": a.rfidUid || "Sin enlazar",
        "Activo": a.activo !== false ? "Si" : "No"
    });
    $("modal-detalle").classList.remove("oculto");
}
$("btn-cerrar-detalle").onclick = () => $("modal-detalle").classList.add("oculto");
$("btn-detalle-rostro").onclick = () => { if (detalleUid) abrirModalRostro(detalleUid, "replace"); };


// ====================== //
// ENLAZAR RFID (admin, modal) //
// ====================== //
async function abrirModalRfid(uid, nombre) {
    $("rfid-alumno-nombre").textContent = nombre || uid;
    $("rfid-estado-texto").textContent = "Acerque la tarjeta al lector RFID...";
    $("rfid-estado-caja").classList.remove("enlazada");
    $("modal-rfid").classList.remove("oculto");

    await api("/api/rfid/escuchar", { method: "POST", body: JSON.stringify({ uidDestino: uid }) });

    if (pollRfid) clearInterval(pollRfid);
    pollRfid = setInterval(async () => {
        const data = await api("/api/rfid/estado");
        if (data.ok && !data.activo && data.ultimaTarjeta) {
            $("rfid-estado-texto").textContent = data.msg || "Tarjeta enlazada correctamente.";
            $("rfid-estado-caja").classList.add("enlazada");
            clearInterval(pollRfid);
            mostrarToast("Tarjeta enlazada correctamente.", "ok");
            cargarAlumnos();
        }
    }, 1500);
}

$("btn-rfid-simular").onclick = async () => {
    const uidTarjeta = $("rfid-simular-uid").value.trim();
    if (!uidTarjeta) return;
    await api("/api/rfid/asignar", { method: "POST", body: JSON.stringify({ uidTarjeta: uidTarjeta }) });
};

$("btn-cerrar-rfid").onclick = () => {
    if (pollRfid) { clearInterval(pollRfid); pollRfid = null; }
    $("modal-rfid").classList.add("oculto");
};


// ====================== //
// ACTUALIZAR ROSTRO (modal) //
// ====================== //
function abrirModalRostro(uid, modo) {
    rostroDestinoUid = uid;
    rostroModo = modo || "replace";
    fotosRostro = [];
    $("rostro-titulo").textContent = rostroModo === "append" ? "Agregar fotos biometricas" : "Reemplazar biometrico";
    $("rostro-video").classList.remove("oculto");
    $("rostro-vacia").classList.remove("oculto");
    $("rostro-btn-capturar").disabled = true;
    $("rostro-btn-quitar").disabled = true;
    $("rostro-btn-guardar").disabled = true;
    $("rostro-contador").textContent = "0";
    $("rostro-miniaturas").innerHTML = "";
    $("rostro-msg").textContent = "";
    $("rostro-msg").className = "mini-mensaje";
    $("modal-rostro").classList.remove("oculto");
}

function detenerCamaraRostro() {
    if (streamRostro) {
        streamRostro.getTracks().forEach(t => t.stop());
        streamRostro = null;
    }
}

$("rostro-btn-iniciar").onclick = async () => {
    try {
        streamRostro = await navigator.mediaDevices.getUserMedia({ video: { width: 480, height: 360 }, audio: false });
        const v = $("rostro-video");
        v.srcObject = streamRostro;
        v.classList.remove("oculto");
        $("rostro-vacia").classList.add("oculto");
        $("rostro-btn-capturar").disabled = false;
    } catch (e) {
        $("rostro-msg").textContent = "No se pudo abrir la camara: " + e.message;
        $("rostro-msg").className = "mini-mensaje error";
    }
};

$("rostro-btn-capturar").onclick = () => {
    if (fotosRostro.length >= MAX_FOTOS) return;
    const v = $("rostro-video");
    const c = $("rostro-canvas");
    c.width = 480; c.height = 360;
    c.getContext("2d").drawImage(v, 0, 0, 480, 360);
    fotosRostro.push(c.toDataURL("image/jpeg", 0.7));
    renderMiniaturas("rostro-miniaturas", "rostro-contador", fotosRostro);
    $("rostro-btn-quitar").disabled = false;
    $("rostro-btn-guardar").disabled = false;
};

$("rostro-btn-quitar").onclick = () => {
    fotosRostro.pop();
    renderMiniaturas("rostro-miniaturas", "rostro-contador", fotosRostro);
    $("rostro-btn-quitar").disabled = fotosRostro.length === 0;
    $("rostro-btn-guardar").disabled = fotosRostro.length === 0;
};

$("rostro-btn-guardar").onclick = async () => {
    if (fotosRostro.length === 0 || !rostroDestinoUid) return;
    if (fotosRostro.length < 3) {
        mostrarToast("Recomendado capturar 3 o mas fotos para un mejor reconocimiento.", "");
    }
    $("rostro-msg").textContent = "Guardando...";
    $("rostro-msg").className = "mini-mensaje";
    try {
        const data = await api("/api/usuarios/" + rostroDestinoUid + "/actualizar-rostro",
            { method: "POST", body: JSON.stringify({ fotos: fotosRostro, modo: rostroModo }) });
        if (data.ok) {
            mostrarToast(data.msg || "Rostro actualizado correctamente.", "ok");
            detenerCamaraRostro();
            $("modal-rostro").classList.add("oculto");
            if (usuarioActual.rol === "admin") cargarAlumnos();
            if (rostroDestinoUid === usuarioActual.uid) cargarMiPerfil();
        } else {
            $("rostro-msg").textContent = data.msg || data.error || "No se pudo actualizar.";
            $("rostro-msg").className = "mini-mensaje error";
        }
    } catch (e) {
        $("rostro-msg").textContent = "Error al guardar el rostro.";
        $("rostro-msg").className = "mini-mensaje error";
    }
};

$("rostro-btn-cerrar").onclick = () => {
    detenerCamaraRostro();
    $("modal-rostro").classList.add("oculto");
};


// ====================== //
// EDITAR ALUMNO (modal, solo admin) //
// ====================== //
async function abrirModalEditar(uid) {
    const data = await api("/api/alumnos/" + uid);
    if (!data.ok) return;
    const a = data.alumno;
    $("modal-editar").dataset.uid = uid;
    $("edit-nombres").value = a.nombres || "";
    $("edit-apellidos").value = a.apellidos || "";
    $("edit-carrera").value = a.carrera || "";
    $("edit-semestre").value = a.semestre || "1";
    $("edit-activo").value = (a.activo !== false) ? "true" : "false";
    $("edit-msg").textContent = "";
    $("edit-msg").className = "mini-mensaje";
    $("modal-editar").classList.remove("oculto");
}

$("edit-btn-guardar").onclick = async () => {
    const uid = $("modal-editar").dataset.uid;
    const cuerpo = {
        nombres: $("edit-nombres").value.trim(),
        apellidos: $("edit-apellidos").value.trim(),
        carrera: $("edit-carrera").value.trim(),
        semestre: $("edit-semestre").value,
        activo: $("edit-activo").value === "true"
    };
    const data = await api("/api/alumnos/" + uid, { method: "PUT", body: JSON.stringify(cuerpo) });
    if (data.ok) {
        mostrarToast("Datos actualizados.", "ok");
        $("modal-editar").classList.add("oculto");
        cargarAlumnos();
    } else {
        $("edit-msg").textContent = data.error || "No se pudo guardar.";
        $("edit-msg").className = "mini-mensaje error";
    }
};

$("edit-btn-cerrar").onclick = () => $("modal-editar").classList.add("oculto");


// ====================== //
// DESACTIVAR / REACTIVAR ALUMNO (admin) //
// ====================== //
async function cambiarEstadoAlumno(uid, activar) {
    const ruta = activar ? ("/api/alumnos/" + uid + "/reactivar") : ("/api/alumnos/" + uid + "/desactivar");
    const data = await api(ruta, { method: "POST", body: "{}" });
    if (data.ok) {
        mostrarToast(data.msg || "Estado actualizado.", "ok");
        cargarAlumnos();
    } else {
        mostrarToast(data.error || "No se pudo cambiar el estado.", "error");
    }
}


// ====================== //
// ENTRADAS Y SALIDAS (con evidencia) //
// ====================== //
async function cargarEntradasSalidas() {
    const data = await api("/api/entradas-salidas");
    if (data.ok) {
        cacheMovimientos = data.movimientos || [];
        pintarEntradas();
    }
}

function pintarEntradas() {
    const fMetodo = $("es-filtro-metodo").value;
    const fMov = $("es-filtro-mov").value;
    const lista = cacheMovimientos.filter(m =>
        (!fMetodo || m.metodo === fMetodo) &&
        (!fMov || m.tipoMovimiento === fMov)
    );
    const cuerpo = $("tabla-entradas");
    if (lista.length === 0) {
        cuerpo.innerHTML = filaVacia(7, "Sin movimientos registrados.");
        return;
    }
    cuerpo.innerHTML = lista.map(m => `
        <tr>
            <td>${m.capturaBase64
                ? `<img class="evidencia-thumb" src="${m.capturaBase64}" onclick="verFoto('${m.id}')">`
                : `<span class="etiqueta no">Sin foto</span>`}</td>
            <td>${formatoFecha(m.fechaHora)}</td>
            <td>${m.nombreCompleto || "-"}</td>
            <td><span class="etiqueta metodo">${m.metodo || "-"}</span></td>
            <td>${etiquetaMovimiento(m.tipoMovimiento)}</td>
            <td>${etiquetaEstado(m.estado)}</td>
            <td>${m.detalle || "-"}</td>
        </tr>`).join("");
}

function etiquetaMovimiento(mov) {
    if (mov === "ENTRADA") return `<span class="etiqueta entrada">ENTRADA</span>`;
    if (mov === "SALIDA") return `<span class="etiqueta salida">SALIDA</span>`;
    if (mov === "INTENTO_DENEGADO") return `<span class="etiqueta denegado-mov">INTENTO</span>`;
    if (mov === "WEB") return `<span class="etiqueta web">WEB</span>`;
    return `<span class="etiqueta">${mov || "-"}</span>`;
}

function verFoto(id) {
    const m = cacheMovimientos.find(x => x.id === id);
    if (!m || !m.capturaBase64) return;
    $("foto-grande").src = m.capturaBase64;
    $("modal-foto").classList.remove("oculto");
}

$("es-filtro-metodo").onchange = pintarEntradas;
$("es-filtro-mov").onchange = pintarEntradas;
$("es-refrescar").onclick = cargarEntradasSalidas;
$("btn-cerrar-foto").onclick = () => $("modal-foto").classList.add("oculto");


// ====================== //
// AYUDAS DE FORMATO        //
// ====================== //
function filasDatos(obj) {
    return Object.entries(obj).map(([k, v]) =>
        `<div class="fila"><span>${k}</span><span>${(v === 0 ? "0" : v) || "-"}</span></div>`
    ).join("");
}

function filaVacia(columnas, texto) {
    return `<tr><td colspan="${columnas}" class="texto-vacio">${texto}</td></tr>`;
}

function etiquetaEstado(estado) {
    if (estado === "PERMITIDO") return `<span class="etiqueta permitido">PERMITIDO</span>`;
    if (estado === "DENEGADO") return `<span class="etiqueta denegado">DENEGADO</span>`;
    return `<span class="etiqueta">${estado || "-"}</span>`;
}

function formatoFecha(fecha) {
    if (!fecha) return "-";
    try {
        const d = new Date(fecha);
        if (isNaN(d.getTime())) return fecha;
        return d.toLocaleString("es-MX");
    } catch (e) {
        return fecha;
    }
}
