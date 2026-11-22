#!/usr/bin/env python3
"""Escaneos: el teléfono manda un PDF y la PC lo guarda y lo pega.

- Los PDF caen en `~/Escaneos/escaneo-AAAA-MM-DD_HHMMSS.pdf`.
- El archivo queda en el portapapeles como `text/uri-list` (lo que entiende
  Nautilus, Thunderbird, Telegram Desktop y casi todo lo que acepte archivos).
- Con `pegar=True` se manda Ctrl+V a la ventana enfocada con ydotool, igual que
  el dictado: el PDF aparece adjunto en el campo que el dueño tenga abierto.

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
        raise ValueError("pdf vacio o cortado")
    with destino.open("rb") as fh:
        if fh.read(4) != b"%PDF":
            destino.unlink(missing_ok=True)
            raise ValueError("lo que llegó no es un PDF")

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
    """Ctrl+V en la ventana enfocada. No manda Enter: el PDF queda adjunto."""
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
    archivos = sorted(
        (p for p in ESCANEO_DIR.glob("*.pdf")), key=lambda p: p.stat().st_mtime, reverse=True
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
