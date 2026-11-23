#!/usr/bin/env python3
"""Servidor de dictado: SOLO voz. El telefono manda audio, la PC escribe.

Es la version recortada de `dictado-remoto` (que ademas tiene bot de Telegram y
la web): aca estan los endpoints que usa la APK (dictado, capturas/escaneo) y el
aviso por UDP que la hace aparecer sola en el selector de servidores del
telefono. Sin escaneo, la captura de pantalla fallaria al elegir esta PC.

    /api/salud        "¿quien dicta aca?" (nombre del equipo + modelo)
    /api/parcial      ultima palabra mientras se graba (detecta "enviar")
    /api/transcribir  transcribe y pega en la ventana enfocada de la PC
    /api/escanear     recibe el PDF del telefono (foto o captura) y lo pega
    /api/escaneos     los ultimos PDF recibidos

El motor (faster-whisper large-v3 en la GPU, prompt de estilo, hotwords y guarda
de eco) es el mismo archivo `motor.py` de `dictado-remoto`: no hay dos motores
distintos en la casa. Ver `sincronizar-motor.sh`.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from escaner import (
    copiar_al_portapapeles,
    guardar as guardar_escaneo,
    notificar as notificar_escaneo,
    pegar_en_pc,
    ultimos as escaneos_ultimos,
)
from motor import (
    AUDIO_DIR,
    IDIOMA,
    AudioInvalido,
    poner_portapapeles,
    estado_portapapeles,
    es_orden,
    leer_portapapeles,
    motor,
)

VERSION = "1.1"

# 🛡️ EL NOMBRE ES LO QUE MUESTRA EL SELECTOR DEL TELEFONO. Por defecto el
# hostname; se puede fijar con DICTADO_EQUIPO (por ejemplo "LinuxWork").
EQUIPO = os.environ.get("DICTADO_EQUIPO") or socket.gethostname()
PUERTO = int(os.environ.get("DICTADO_PUERTO", "8099"))

app = FastAPI(title="Dictado (PC)")


def _beacon() -> None:
    """Contesta "¿quién dicta por acá?" en UDP :8099.

    🛡️ ES LO QUE HACE QUE LA PC APAREZCA SOLA EN LA APK: el telefono manda
    `DICTADO?` al broadcast de la red y cada servidor contesta su nombre y su
    puerto. Sin esto habría que escribir la IP a mano en el telefono.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", 8099))
    except OSError as exc:
        print(f"[beacon] no pude escuchar UDP 8099: {exc}", flush=True)
        return
    respuesta = json.dumps({"equipo": EQUIPO, "puerto": PUERTO}, ensure_ascii=False).encode()
    print(f"[beacon] escuchando en UDP 8099 como {EQUIPO}", flush=True)
    while True:
        try:
            datos, origen = sock.recvfrom(64)
        except OSError:
            continue
        if datos.strip().upper() != b"DICTADO?":
            continue
        try:
            sock.sendto(respuesta, origen)
        except OSError:
            pass


@app.on_event("startup")
def _arranque() -> None:
    motor.cargar_en_hilo()
    # Suelta la VRAM tras N minutos sin dictar (ver motor.py).
    motor.vigilar_inactividad()
    threading.Thread(target=_beacon, daemon=True).start()


@app.get("/api/salud")
def salud() -> dict:
    """Lo que consulta el selector del teléfono: nombre del equipo y modelo."""
    return {
        "ok": motor.modelo is not None,
        "equipo": EQUIPO,
        "puerto": PUERTO,
        "version": VERSION,
        "idioma": IDIOMA,
        **motor.estado,
    }


@app.post("/api/parcial")
async def parcial(audio: UploadFile = File(...), idioma: str = Form(IDIOMA)) -> dict:
    """Transcripción rápida SIN efectos (la app la manda cada 1,4 s al grabar)."""
    marca = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    crudo = AUDIO_DIR / f"{marca}-parcial.aac"
    with crudo.open("wb") as fh:
        shutil.copyfileobj(audio.file, fh)
    if crudo.stat().st_size == 0:
        crudo.unlink(missing_ok=True)
        raise HTTPException(400, "audio vacio")
    try:
        texto = motor.texto_rapido(crudo, idioma=idioma)
    except AudioInvalido:
        texto = ""
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    finally:
        crudo.unlink(missing_ok=True)
        crudo.with_suffix(".wav").unlink(missing_ok=True)
    return {"texto": texto, "orden": es_orden(texto)}


@app.post("/api/escanear")
async def escanear(
    pdf: UploadFile = File(...),
    pegar: bool = Form(True),
    origen: str = Form("movil"),
) -> JSONResponse:
    """Guarda el PDF que manda el teléfono, lo copia y lo pega en la PC.

    🛡️ ES EL MISMO ENDPOINT QUE USA LA PESTAÑA ESCANEAR Y EL AVISO DE CAPTURAS
    DEL APK: si la PC elegida en el selector no lo tiene, "Enviar a la PC" de una
    captura falla con 404. Por eso este servidor, aunque sea "solo dictado",
    también recibe imágenes: se guardan en `~/Escaneos`, quedan en el
    portapapeles como archivo y se pegan con Ctrl+V en la ventana enfocada.
    """
    try:
        destino = guardar_escaneo(pdf.file, origen=origen)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    copiado = copiar_al_portapapeles(destino)
    pegado = bool(copiado and pegar and pegar_en_pc())
    notificar_escaneo(destino, pegado)
    return JSONResponse(
        {
            "ok": True,
            "archivo": str(destino),
            "nombre": destino.name,
            "carpeta": str(destino.parent),
            "bytes": destino.stat().st_size,
            "copiado": copiado,
            "pegado": pegado,
        }
    )


@app.get("/api/escaneos")
def escaneos(n: int = 20) -> dict:
    """Los últimos PDF recibidos (los lee el agente y el propio teléfono)."""
    return {"items": escaneos_ultimos(n)}


@app.post("/api/transcribir")
async def transcribir(
    audio: UploadFile = File(...),
    escribir: bool = Form(False),
    idioma: str = Form(IDIOMA),
    origen: str = Form("telefono"),
    notificar: bool = Form(True),
    principal: bool = Form(True),
) -> JSONResponse:
    """Transcribe y, si `escribir=true`, lo pega en la ventana enfocada."""
    marca = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    sufijo = Path(audio.filename or "audio").suffix or ".bin"
    crudo = AUDIO_DIR / f"{marca}-crudo{sufijo}"
    with crudo.open("wb") as fh:
        shutil.copyfileobj(audio.file, fh)
    if crudo.stat().st_size == 0:
        crudo.unlink(missing_ok=True)
        raise HTTPException(400, "audio vacio")

    try:
        resultado = motor.transcribir(crudo, idioma=idioma, origen_etiqueta=origen,
                                      notificar=notificar, principal=principal)
    except AudioInvalido as exc:
        raise HTTPException(400, f"audio no reconocido: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    resultado["escrito_en_pc"] = (
        motor.escribir_en_pc(resultado["texto"])
        if escribir and resultado.get("hay_voz")
        else False
    )
    return JSONResponse(resultado)


@app.post("/api/portapapeles")
async def portapapeles(datos: dict) -> dict:
    """Texto que llega al portapapeles de la PC (desde el teléfono o a mano)."""
    texto = str(datos.get("texto", ""))
    if not texto.strip():
        raise HTTPException(400, "texto vacio")
    origen = str(datos.get("origen", "telefono"))
    ok = poner_portapapeles(texto, origen=origen)
    return {"copiado": ok, "caracteres": len(texto)}


@app.get("/api/portapapeles")
def portapapeles_leer(version: int = -1) -> dict:
    """Lo que hay en el portapapeles de la PC.

    `version` es la última que vio el teléfono: si no coincide, la respuesta trae
    `cambiado=true` y el teléfono lo escribe en su portapapeles (camino PC →
    teléfono). Si `origen` es "telefono", es lo que él mismo mandó y no se
    devuelve.
    """
    estado = estado_portapapeles()
    return {
        "texto": estado["texto"] or "",
        "version": estado["version"],
        "origen": estado["origen"],
        "cambiado": estado["version"] != version,
    }
