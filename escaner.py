#!/usr/bin/env python3
"""Escaneos: el teléfono manda un PDF y la PC lo guarda y lo pega.

- 🛡️ [2026-11-26] LO QUE SE GUARDA ES UN **JPEG**, no el PDF: lo pidió el dueño
  (*"no debe convertirlo a pdf, debe hacerlo por jpeg mejor"*, hablando de la
  captura que la app manda sola al tirar un screenshot). El teléfono arma un
  PDF; acá se rasteriza la primera hoja con `pdftoppm` (poppler) y se guarda
  `~/Escaneos/escaneo-AAAA-MM-DD_HHMMSS.jpg`. El JPEG se ve en cualquier lado
  (galería, chat, WhatsApp) y no necesita visor de PDF.
  Si no hay poppler o el PDF no se puede leer, queda el PDF de siempre: nunca se
  pierde el escaneo.
- El archivo queda en el portapapeles como `text/uri-list` (lo que entiende
  Nautilus, Thunderbird, Telegram Desktop y casi todo lo que acepte archivos).
- Con `pegar=True` se manda Ctrl+V a la ventana enfocada con ydotool, igual que
  el dictado: el archivo aparece adjunto en el campo que el dueño tenga abierto.

🛡️ ES EL MISMO MODELO QUE EL DICTADO: la app no muestra nada, la PC hace el
trabajo. Si el Ctrl+V no entra en la app enfocada, el archivo queda guardado y
copiado igual: no se pierde nada.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

ESCANEO_DIR = Path(
    os.environ.get("DICTADO_ESCANEOS", str(Path.home() / "Escaneos"))
)
HISTORIAL = ESCANEO_DIR / "historial.txt"
ULTIMO = ESCANEO_DIR / "ultimo.json"

# INPUT EVENT CODES de Linux para KEY_LEFTCTRL y KEY_V.
CTRL = "29:1"
V = "47:1"
V_SUELTA = "47:0"
CTRL_SUELTA = "29:0"


def _env_grafico() -> dict:
    """El servicio de usuario a veces no hereda WAYLAND_DISPLAY/XAUTHORITY."""
    env = os.environ.copy()
    if not env.get("WAYLAND_DISPLAY"):
        env["WAYLAND_DISPLAY"] = "wayland-0"
    if not env.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    env.setdefault("DISPLAY", ":0")
    return env


def guardar(flujo: BinaryIO, origen: str = "movil") -> Path:
    """Guarda el PDF que llega del teléfono y devuelve la ruta.

    🛡️ SE VALIDA LA CABECERA: un corte de red o un archivo que no es PDF se
    descarta en vez de dejar basura con extensión `.pdf` que después nadie
    puede abrir.
    """
    ESCANEO_DIR.mkdir(parents=True, exist_ok=True)
    destino = ESCANEO_DIR / f"escaneo-{datetime.now():%Y-%m-%d_%H%M%S}.pdf"
    # Dos escaneos en el mismo segundo no se pisan.
    contador = 1
    while destino.exists():
        destino = ESCANEO_DIR / f"escaneo-{datetime.now():%Y-%m-%d_%H%M%S}-{contador}.pdf"
        contador += 1

    with destino.open("wb") as fh:
        shutil.copyfileobj(flujo, fh)

    if destino.stat().st_size < 64:
        destino.unlink(missing_ok=True)
        raise ValueError("archivo vacio o cortado")
    with destino.open("rb") as fh:
        cabecera = fh.read(8)
    # 🛡️ [2026-11-26] La APK parcheada manda un JPEG; las viejas, un PDF. Los
    # dos valen: un corte de red o basura se sigue descartando.
    if cabecera.startswith(b"%PDF"):
        porque_pdf = True
    elif cabecera.startswith(b"\xff\xd8\xff"):
        porque_pdf = False
        destino = destino.rename(destino.with_suffix(".jpg"))
    elif cabecera.startswith(b"\x89PNG"):
        porque_pdf = False
        destino = destino.rename(destino.with_suffix(".png"))
    else:
        destino.unlink(missing_ok=True)
        raise ValueError("lo que llegó no es un PDF ni una imagen")

    # 🛡️ [2026-11-26] Se guarda en JPEG (ver el docstring del módulo): si llegó
    # un PDF se rasteriza; si ya es una imagen, se queda como está.
    if porque_pdf:
        jpeg = _pdf_a_jpeg(destino)
        if jpeg is not None:
            destino.unlink(missing_ok=True)
            destino = jpeg

    with HISTORIAL.open("a", encoding="utf-8") as fh:
        fh.write(
            f"--- {datetime.now():%Y-%m-%d %H:%M:%S} --- [{origen}] "
            f"{destino.name} ({destino.stat().st_size} B)\n"
        )
    ULTIMO.write_text(
        json.dumps(
            {
                "archivo": str(destino),
                "nombre": destino.name,
                "bytes": destino.stat().st_size,
                "origen": origen,
                "fecha": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return destino


def _pdf_a_jpeg(pdf: Path, dpi: int = 150) -> Path | None:
    """La primera hoja del PDF como JPEG (o `None` si no se puede).

    🛡️ `pdftoppm -singlefile` deja `hoja.jpg` al lado del PDF (sin el `-1` que
    agrega poppler). La factura o la captura que llega es de una hoja.
    """
    if not shutil.which("pdftoppm"):
        return None
    salida = pdf.with_suffix("")  # poppler le agrega `.jpg`
    try:
        subprocess.run(
            ["pdftoppm", "-jpeg", "-r", str(dpi), "-f", "1", "-l", "1", "-singlefile", str(pdf), str(salida)],
            timeout=20,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[escaner] pdftoppm fallo: {exc}", flush=True)
        return None
    jpg = salida.with_suffix(".jpg")
    if not jpg.exists() or jpg.stat().st_size == 0:
        return None
    return jpg


def copiar_al_portapapeles(ruta: Path) -> bool:
    """Deja el archivo listo para pegar (Ctrl+V) en la PC."""
    uri = ruta.as_uri()
    env = _env_grafico()
    # 🛡️ [2026-11-19] `stdout`/`stderr` a /dev/null: wl-copy se desprende en un
    # proceso hijo que se queda con los pipes abiertos y `subprocess.run` se
    # quedaba esperando 10 s (lo medí: `copiado` tardaba y el POST se demoraba).
    call = {"env": env, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if shutil.which("wl-copy"):
        try:
            subprocess.run(
                ["wl-copy", "--type", "text/uri-list"],
                input=uri + "\n",
                text=True,
                timeout=10,
                check=True,
                **call,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[escaner] wl-copy fallo: {exc}", flush=True)
    if shutil.which("xclip"):
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "text/uri-list", "-i"],
                input=uri + "\n",
                text=True,
                timeout=10,
                check=True,
                **call,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[escaner] xclip fallo: {exc}", flush=True)
    return False


def pegar_en_pc(espera: float = 0.25) -> bool:
    """Ctrl+V en la ventana enfocada. No manda Enter: el archivo queda adjunto."""
    if not shutil.which("ydotool"):
        return False
    try:
        # El portapapeles tarda un instante en publicar el tipo nuevo.
        time.sleep(espera)
        subprocess.run(
            ["ydotool", "key", CTRL, V, V_SUELTA, CTRL_SUELTA],
            timeout=10,
            check=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[escaner] ydotool key fallo: {exc}", flush=True)
        return False


def notificar(ruta: Path, pegado: bool) -> None:
    if not shutil.which("notify-send"):
        return
    detalle = "pegado en la ventana activa" if pegado else "en el portapapeles"
    subprocess.run(
        [
            "notify-send",
            "-a",
            "Dictado remoto",
            "-u",
            "low",
            "-i",
            "x-office-document",
            "Escaneo recibido",
            f"{ruta.name} · {detalle}",
        ],
        check=False,
    )


def ultimos(n: int = 20) -> list[dict]:
    """Los últimos escaneos, para `/api/escaneos` y para el agente."""
    if not ESCANEO_DIR.exists():
        return []
    # 🛡️ [2026-11-26] Los nuevos son `.jpg`; los `.pdf` viejos siguen apareciendo.
    archivos = sorted(
        (p for p in list(ESCANEO_DIR.glob("*.jpg")) + list(ESCANEO_DIR.glob("*.pdf"))),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    salida = []
    for ruta in archivos[: max(1, min(n, 100))]:
        stat = ruta.stat()
        salida.append(
            {
                "nombre": ruta.name,
                "archivo": str(ruta),
                "bytes": stat.st_size,
                "fecha": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
        )
    return salida
