#!/usr/bin/env python3
"""Motor de transcripcion compartido por el servidor web y el bot de Telegram.

faster-whisper large-v3 en la GPU, VAD silero, beam 5. Historial en ~/dictado/
con el mismo formato que usa pcvoice, para que el agente lo lea igual.
"""
from __future__ import annotations

import difflib
import gc
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

OUT_DIR = Path.home() / "dictado"
AUDIO_DIR = OUT_DIR / "audios"
# 🛡️ [2026-11-22] large-v3, NO turbo. El 19/11 se cambió al turbo por rapidez,
# pero el dueño notó que fallaba: en sus propios audios el turbo escribió
# "el Führer en Delivery" (footer), "para notar tan desperdado" (para no estar
# tan despegado) y "la fatura" (factura); con large-v3 los tres salen bien.
# Costo medido: +1,8 GB de VRAM (3,9 vs 2,1) y 0,5-0,8 s por nota de 10 s, que
# el dueño acepta a cambio de precisión. El turbo queda por env si hace falta.
MODELO = os.environ.get("DICTADO_MODELO", "large-v3")
IDIOMA = os.environ.get("DICTADO_IDIOMA", "es")

# 🛡️ [2026-11-19] DESCARGUE POR INACTIVIDAD: LA GPU ES DE 8 GB Y ES COMPARTIDA.
#
# Lo que pasó: `large-v3` en `cuda/float16` se queda con 3882 MiB RESIDENTES
# aunque pasen 12 h sin dictar. Se abrió un juego, la VRAM se llenó, y el proceso
# GPU de Brave murió 3 veces por Skia OOM (`exit_code=8704`, `NO_MEMORY` en el
# kernel, `nvidia-smi` ya no lista el proceso GPU de Brave): Chromium apagó la
# aceleración para toda la sesión y el scroll de cualquier lista se sintió
# pesado. El culpable del espacio no era el juego solo, era este modelo sentado
# sin hacer nada.
#
# Ahora, tras N minutos sin transcribir, el modelo se suelta (VRAM a cero) y la
# próxima nota lo recarga sola: 1,7 s medidos (`carga_s` en `/api/salud`).
# `DICTADO_DESCARGAR_MIN=0` apaga el vigilante (el modelo se queda cargado).
INACTIVO_MIN = float(os.environ.get("DICTADO_DESCARGAR_MIN", "10"))
# 🛡️ [2026-11-22] EL PROMPT ES UNA MUESTRA DE ESTILO, NO UNA LISTA DE PALABRAS.
# Whisper imita el formato de lo que se le pone: con una lista de nombres suelta
# devolvía frases sueltas y sin mayúsculas iniciales. Con una instrucción del
# mismo tipo que el dictado (español dominicano, temas de la app) acierta mejor
# las mayúsculas y la puntuación. Los nombres propios van ADEMÁS en `hotwords`,
# que sesgan el decodificador sin ocupar el prompt.
PROMPT = os.environ.get(
    "DICTADO_PROMPT",
    "Instrucciones técnicas dictadas en español dominicano sobre una app web: "
    "Pon el botón más arriba, quita el footer en delivery, el popover del "
    "despachador no debe abrir el teclado. Trabajo con Tailwind, GitHub, "
    "commit, deploy, Zentro POS, Surtidora Noemi, Glint, Neorides, Vetra.",
)

# 🛡️ [2026-11-22] HOTWORDS: nombres y jerga que whisper rompe seguido. Se pasan
# al decodificador en cada ventana (faster-whisper >= 1.1). Medido: "GitHub" y
# "quise decir" salieron bien con hotwords y mal sin ellos.
HOTWORDS = os.environ.get(
    "DICTADO_HOTWORDS",
    "Zentro POS, Surtidora Noemi, Glint, Neorides, HiperOfertas RD, Vetra, "
    "Nestly, Chispa, Rastro, Tailscale, OpenCode, Dokploy, Prisma, Postgres, "
    "Tailwind, GitHub, commit, deploy, push, popover, footer, delivery, "
    "repartidor, despachador, factura, RNC, ITBIS.",
)

AUDIO_DIR.mkdir(parents=True, exist_ok=True)


class AudioInvalido(Exception):
    pass


# 🛡️ [2026-11-19] LA ORDEN HABLADA "ENVIAR".
#
# Lo pidió el dueño: "que detecte cuando diga enviar sin decir nada más al
# final para que deje de grabar y lo envíe". La palabra se saca del texto ANTES
# de pegarla en la PC: el dictado es lo que va antes de la orden, no la orden.
ORDENES = ("enviar", "envia", "envía", "envialo", "envíalo", "envie", "envíe")


def _plano(texto: str) -> str:
    """Minúsculas y sin signos: solo sirve para comparar."""
    limpio = re.sub(r"[^0-9a-záéíóúñü]+", " ", (texto or "").lower())
    return " ".join(limpio.split())


def es_eco_del_prompt(texto: str) -> bool:
    """True si lo que "transcribió" es el prompt repetido.

    🛡️ [2026-11-22] MEDIDO: con un audio largo y flojo (-37 dB de media, casi todo
    silencio y ruido de cuarto) whisper devolvió literalmente
    "Instrucciones técnicas dictadas en español dominicano sobre una app web,
    POS, Surtidora, La pimienta." — o sea, el prompt con una palabra pegada.
    Eso se hubiera pegado en la PC como si fuera un dictado. Con el mismo audio
    y sin prompt devolvió lo que de verdad había ("1, 2, 3, 4 ... la pimienta"),
    que tampoco es dictado, pero al menos no es texto inventado por nosotros.

    El chequeo es textual: si el resultado se parece demasiado al prompt, no hay
    voz que transcribir.
    """
    t = _plano(texto)
    p = _plano(PROMPT)
    if len(t) < 20 or not p:
        return False
    # 🛡️ EL ECO EMPIEZA CON EL PROMPT, no lo cita al pasar: si el dictado no
    # arranca igual que el prompt, es habla de verdad y no se toca. Así una
    # frase del dueño que use las mismas palabras ("quita el footer en
    # delivery") nunca se descarta.
    if not t.startswith(p[:40]):
        return False
    # 🛡️ Y se mide CUÁNTO DEL TEXTO ESTÁ DENTRO DEL PROMPT, no el parecido de los
    # dos completos: el prompt es mucho más largo, y comparando enteros daba
    # 0,41 aunque la transcripción fuera el prompt recortado.
    sm = difflib.SequenceMatcher(None, t, p, autojunk=False)
    cubierto = sum(bloque.size for bloque in sm.get_matching_blocks())
    return cubierto / len(t) >= 0.80


def limpiar_orden(texto: str) -> str:
    """Saca la orden del FINAL del dictado ("... la factura, enviar")."""
    partes = texto.strip().split()
    cambiado = False
    while partes:
        ultima = partes[-1].strip(" .,;:!¡?¿\n\t").lower()
        if ultima in ORDENES:
            partes.pop()
            cambiado = True
            continue
        break
    return " ".join(partes) if cambiado else texto


def es_orden(texto: str) -> bool:
    """True si el dictado TERMINA con la orden (la app lo usa para cortar)."""
    partes = texto.strip().split()
    if not partes:
        return False
    return partes[-1].strip(" .,;:!¡?¿\n\t").lower() in ORDENES


class Motor:
    def __init__(self) -> None:
        self.modelo = None
        self.lock = threading.Lock()
        self.estado = {
            "cargando": True,
            "error": None,
            "dispositivo": None,
            "modelo": MODELO,
            "descargado": False,
        }
        self._ultimo_uso = time.time()
        # 🛡️ [2026-11-22] CARGAR large-v3 TARDA ~12 s (el turbo tardaba 1,7 s).
        # Antes, si una nota llegaba mientras el modelo estaba cargando, el
        # servidor contestaba 503 "cargando modelo, reintenta" y el dueño veía un
        # error en la app. Ahora `asegurar()` ESPERA a que termine la carga.
        self._listo = threading.Event()
        self._wav_vacio()

    # -- arranque ---------------------------------------------------------
    def _wav_vacio(self) -> None:
        ruta = AUDIO_DIR / "vacio.wav"
        if ruta.exists():
            return
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
             "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(ruta)],
            check=False,
        )

    def cargar(self) -> None:
        self._listo.clear()
        try:
            self._cargar()
        finally:
            self._listo.set()

    def _cargar(self) -> None:
        from faster_whisper import WhisperModel

        for dispositivo, compute in (("cuda", "float16"), ("cuda", "int8_float16"), ("cpu", "int8")):
            try:
                t0 = time.time()
                self.modelo = WhisperModel(MODELO, device=dispositivo, compute_type=compute)
                # Primer pase de prueba: la primera inferencia compila kernels.
                self._transcribir_crudo(str(AUDIO_DIR / "vacio.wav"), IDIOMA)
                self.estado.update(
                    cargando=False,
                    error=None,
                    dispositivo=f"{dispositivo}/{compute}",
                    carga_s=f"{time.time() - t0:.1f}",
                    descargado=False,
                )
                self._ultimo_uso = time.time()
                print(f"[motor] {MODELO} listo en {dispositivo}/{compute}", flush=True)
                return
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                self.estado.update(cargando=False, error=f"{dispositivo}/{compute}: {exc}")
                print(f"[motor] fallo {dispositivo}/{compute}: {exc}", flush=True)
        print(f"[motor] SIN MODELO: {self.estado['error']}", flush=True)

    def cargar_en_hilo(self) -> threading.Thread:
        hilo = threading.Thread(target=self.cargar, daemon=True)
        hilo.start()
        return hilo

    # -- memoria (VRAM) ---------------------------------------------------
    def tocar(self) -> None:
        """Marca uso reciente: el vigilante cuenta la inactividad desde acá."""
        self._ultimo_uso = time.time()

    def _soltar(self, motivo: str) -> bool:
        """Suelta el modelo. NO toma el lock: lo toma quien llama."""
        if self.modelo is None:
            return False
        self.modelo = None
        gc.collect()
        # faster-whisper libera su memoria en el destructor; torch solo hace
        # falta para devolverle el caché al driver. Si no está instalado,
        # no importa: el modelo ya se soltó.
        try:
            import torch  # noqa: PLC0415

            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        except Exception:  # noqa: BLE001
            pass
        self.estado.update(descargado=True, dispositivo=None, motivo_descarga=motivo)
        print(f"[motor] modelo descargado de la GPU ({motivo})", flush=True)
        return True

    def descargar(self, motivo: str = "manual") -> bool:
        with self.lock:
            return self._soltar(motivo)

    def asegurar(self) -> None:
        """Si el vigilante soltó el modelo, lo vuelve a cargar (1,7 s medidos)."""
        if self.modelo is not None:
            self.tocar()
            return
        if self.estado.get("cargando"):
            # Ya hay una carga en curso (la dispara el parcial del teléfono o el
            # arranque): se espera en vez de devolver error.
            print("[motor] esperando la carga que ya está en curso", flush=True)
            self._listo.wait(timeout=120)
            if self.modelo is None:
                raise RuntimeError(f"modelo no disponible: {self.estado['error']}")
            self.tocar()
            return
        self.estado.update(cargando=True, descargado=False)
        self.cargar()
        if self.modelo is None:
            raise RuntimeError(f"modelo no disponible: {self.estado['error']}")
        self.tocar()

    def vigilar_inactividad(self) -> None:
        """Hilo que suelta el modelo tras `INACTIVO_MIN` minutos sin usarlo."""
        if INACTIVO_MIN <= 0:
            print("[motor] vigilante de VRAM apagado (DICTADO_DESCARGAR_MIN=0)", flush=True)
            return
        limite = INACTIVO_MIN * 60

        def bucle() -> None:
            while True:
                time.sleep(30)
                if self.modelo is None or time.time() - self._ultimo_uso < limite:
                    continue
                # Con una transcripción en curso no se toca nada: se verá en la
                # próxima vuelta (30 s después).
                if not self.lock.acquire(blocking=False):
                    continue
                try:
                    if time.time() - self._ultimo_uso >= limite:
                        self._soltar(f"{INACTIVO_MIN:g} min sin usar")
                finally:
                    self.lock.release()

        threading.Thread(target=bucle, daemon=True, name="vigilante-vram").start()
        print(f"[motor] vigilante de VRAM activo: suelta el modelo a los {INACTIVO_MIN:g} min", flush=True)

    # -- audio ------------------------------------------------------------
    def a_wav(self, origen: Path, destino: Path) -> None:
        """Cualquier audio del telefono (opus, m4a, ogg, amr, 3gp) a 16 kHz mono."""
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(origen),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destino)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not destino.exists():
            raise AudioInvalido(proc.stderr.strip()[:300] or "audio no reconocido")

    def _muestras(self, wav: Path):
        """WAV → float32 mono 16 kHz, con ffmpeg (sin PyAV).

        🛡️ [2026-11-22] POR QUÉ NO SE LE PASA EL ARCHIVO A WHISPER: faster-whisper
        decodifica con PyAV, y el `av` 19.0.2 para Python 3.14 viene roto
        (`TypeError: open() got an unexpected keyword argument
        'metadata_errors'`). Como el audio YA se convierte con ffmpeg, acá se
        pide el PCM por la salida estándar y se pasa el arreglo: una dependencia
        menos en el camino crítico y funciona con cualquier Python.
        """
        import numpy as np

        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(wav),
             "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
            capture_output=True,
        )
        if proc.returncode != 0 or not proc.stdout:
            detalle = proc.stderr.decode("utf-8", "replace").strip()[:300]
            raise AudioInvalido(detalle or "audio no reconocido")
        return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0

    def _transcribir_crudo(self, ruta: str, idioma: str, beam: int = 5,
                           con_prompt: bool = True):
        muestras = self._muestras(Path(ruta))
        segmentos, info = self.modelo.transcribe(
            muestras if muestras is not None else ruta,
            language=None if idioma == "auto" else idioma,
            task="transcribe",
            beam_size=beam,
            vad_filter=True,
            # 🛡️ [2026-11-22] CORTES DE SILENCIO LARGOS (2000 ms, el default de
            # silero) Y 400 ms DE AIRE A LOS LADOS. Con 400 ms de silencio el
            # VAD partía la nota en pedazos y cada pedazo se decodificaba solo:
            # "por lo menos en móvil" salía "por la mano móvil". Con el default
            # (y el padding explícito) sale bien.
            vad_parameters={"min_silence_duration_ms": 2000, "speech_pad_ms": 400},
            condition_on_previous_text=False,
            initial_prompt=PROMPT if con_prompt else None,
            hotwords=(HOTWORDS or None) if con_prompt else None,
        )
        return "".join(s.text for s in segmentos).strip(), info

    def texto_rapido(self, origen: Path, idioma: str = IDIOMA) -> str:
        """Transcribe y nada más: sin historial, sin notificación y sin pegar.

        La usa la app del teléfono mientras sigue grabando, para ver si el
        dictado ya termina en "enviar". Va con beam 1 (la mitad del tiempo):
        acá solo importa la última palabra, no la transcripción perfecta.
        """
        self.asegurar()
        wav = origen.with_suffix(".wav") if origen.suffix.lower() != ".wav" else origen
        if wav != origen:
            self.a_wav(origen, wav)
        with self.lock:
            # Sin prompt ni hotwords: acá solo interesa la última palabra, y el
            # prompt podría devolverse tal cual (eco) y confundir la orden.
            texto, _ = self._transcribir_crudo(str(wav), idioma, beam=1, con_prompt=False)
        self.tocar()
        return texto

    def transcribir(self, origen: Path, idioma: str = IDIOMA, origen_etiqueta: str = "telefono",
                    notificar: bool = True, principal: bool = True) -> dict:
        # Si el vigilante lo soltó por inactividad, esto lo recarga solo.
        self.asegurar()

        wav = origen.with_suffix(".wav") if origen.suffix.lower() != ".wav" else origen
        if wav != origen:
            self.a_wav(origen, wav)

        t0 = time.time()
        with self.lock:
            texto, info = self._transcribir_crudo(str(wav), idioma)
        self.tocar()
        segundos = time.time() - t0
        duracion = float(getattr(info, "duration", 0) or 0)
        texto = limpiar_orden(texto or "")
        # 🛡️ El prompt devuelto como transcripción NO es voz: no se pega.
        if texto and es_eco_del_prompt(texto):
            print(f"[motor] descartado: eco del prompt ({texto[:60]!r})", flush=True)
            texto = ""
        # 🛡️ [2026-11-19] "(no se detecto voz)" NO SE PEGA EN LA PC: es un aviso
        # para la pantalla, no un dictado. Antes se escribía en la ventana
        # enfocada (medido en la prueba del teléfono).
        hay_voz = bool(texto)
        texto = texto or "(no se detecto voz)"

        self.guardar(texto, origen_etiqueta, segundos, duracion, principal=principal)
        if notificar:
            self.notificar(texto)
        return {
            "texto": texto,
            "duracion": round(duracion, 2),
            "segundos": round(segundos, 2),
            "modelo": MODELO,
            "idioma": getattr(info, "language", idioma),
            "hay_voz": hay_voz,
        }

    # -- salidas ----------------------------------------------------------
    def guardar(self, texto: str, origen: str, segundos: float, duracion: float,
                principal: bool = True) -> None:
        """`principal=False` NO toca `ultimo.txt`/`ultimo.json`.

        🛡️ [2026-11-22] Lo usan las notas de voz de Hermes (Telegram): van al
        historial, pero no pisan el "último dictado" que lee `pcvoice texto` y
        que es el del dictado por voz en la PC.
        """
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with (OUT_DIR / "historial.txt").open("a", encoding="utf-8") as fh:
            fh.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} --- [{origen} {duracion:.1f}s]\n{texto}\n")
        if not principal:
            return
        (OUT_DIR / "ultimo.txt").write_text(texto, encoding="utf-8")
        (OUT_DIR / "ultimo.json").write_text(
            json.dumps(
                {
                    "texto": texto,
                    "origen": origen,
                    "duracion_audio": round(duracion, 2),
                    "segundos": round(segundos, 2),
                    "modelo": MODELO,
                    "fecha": datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def notificar(self, texto: str) -> None:
        if not shutil.which("notify-send"):
            return
        subprocess.run(
            ["notify-send", "-a", "Dictado remoto", "-u", "low", "Dictado recibido", texto[:180]],
            check=False,
        )

    def escribir_en_pc(self, texto: str, enter: bool = True) -> bool:
        """Pega el texto en la ventana enfocada (portapapeles + Ctrl+V) y da Enter.

        Pegar es instantáneo y respeta acentos y mayúsculas; tipear con ydotool
        va tecla por tecla y se presta a errores. El tipeo queda de respaldo.
        """
        plano = " ".join(texto.split())
        if not plano:
            return False

        if self._copiar(plano):
            # KEY_LEFTCTRL=29, KEY_V=47, KEY_ENTER=28 (input event codes de Linux)
            if self._ydotool("29:1", "47:1", "47:0", "29:0"):
                if enter:
                    time.sleep(0.2)
                    self._ydotool("28:1", "28:0")
                return True

        try:
            subprocess.run(["ydotool", "type", "-d", "6", plano], timeout=30, check=True)
            if enter:
                time.sleep(0.2)
                self._ydotool("28:1", "28:0")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[motor] no pude escribir: {exc}", flush=True)
            return False

    def _copiar(self, texto: str) -> bool:
        if not shutil.which("wl-copy"):
            return False
        try:
            subprocess.run(
                ["wl-copy", "--type", "text/plain;charset=utf-8"],
                input=texto,
                text=True,
                timeout=10,
                check=True,
                env=self._env_grafico(),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[motor] wl-copy fallo: {exc}", flush=True)
            return False

    def _ydotool(self, *teclas: str) -> bool:
        try:
            subprocess.run(["ydotool", "key", *teclas], timeout=10, check=True)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[motor] ydotool key fallo: {exc}", flush=True)
            return False

    @staticmethod
    def _env_grafico() -> dict:
        """El servicio de usuario a veces no hereda WAYLAND_DISPLAY/XAUTHORITY."""
        env = os.environ.copy()
        if not env.get("WAYLAND_DISPLAY"):
            env["WAYLAND_DISPLAY"] = "wayland-0"
        if not env.get("XDG_RUNTIME_DIR"):
            env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        env.setdefault("DISPLAY", ":0")
        return env


# -- portapapeles de la PC (sincronización con el teléfono) -----------------

def _env_grafico_global() -> dict:
    return Motor._env_grafico()


def copiar_portapapeles(texto: str) -> bool:
    """Deja `texto` en el portapapeles de la PC (lo que copiaste en el celular)."""
    if not texto or not shutil.which("wl-copy"):
        return False
    try:
        subprocess.run(
            ["wl-copy", "--type", "text/plain;charset=utf-8"],
            input=texto, text=True, timeout=10, check=True,
            env=_env_grafico_global(),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[portapapeles] wl-copy fallo: {exc}", flush=True)
        return False


def leer_portapapeles() -> str:
    """Lo que hay ahora en el portapapeles de la PC."""
    if not shutil.which("wl-paste"):
        return ""
    try:
        proc = subprocess.run(
            ["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"],
            capture_output=True, timeout=5, env=_env_grafico_global(),
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


motor = Motor()
