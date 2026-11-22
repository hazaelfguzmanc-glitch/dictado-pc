#!/usr/bin/env bash
# Instala el servidor de dictado en ESTA PC, para que aparezca solo en el
# selector de servidores de la APK del telefono.
#
#   ./instalar.sh                  # puerto 8099, large-v3 (GPU si hay)
#   ./instalar.sh --puerto 8199
#   ./instalar.sh --modelo small   # GPU chica o sin CUDA
#   ./instalar.sh --nombre "PC Sala"  # cómo aparece en el selector del telefono
#   ./instalar.sh --sin-ufw        # no tocar el firewall
#
# 🛡️ NO TOCA NADA QUE YA ESTE: si otra PC de la red ya dicta en 8099, esta corre
# en el puerto que le pidas. El servicio es `dictado-pc.service` (systemd --user).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/dictado-pc"
VENV="$DEST/.venv"
UNIDAD="$HOME/.config/systemd/user/dictado-pc.service"
PUERTO=8099
MODELO="${DICTADO_MODELO:-large-v3}"
NOMBRE=""
UFW=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --puerto) PUERTO="$2"; shift 2 ;;
        --modelo) MODELO="$2"; shift 2 ;;
        --nombre) NOMBRE="$2"; shift 2 ;;
        --sin-ufw) UFW=0; shift ;;
        -h|--ayuda) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "opción desconocida: $1" >&2; exit 1 ;;
    esac
done

decir() { printf '  %s\n' "$*"; }
titulo() { printf '\n== %s\n' "$*"; }

[[ -f "$DIR/servidor.py" && -f "$DIR/motor.py" ]] || {
    echo "error: faltan servidor.py / motor.py al lado de este script" >&2
    exit 1
}

titulo "Paquetes"
FALTAN=()
command -v python3 >/dev/null || FALTAN+=(python)
command -v ffmpeg >/dev/null || FALTAN+=(ffmpeg)
command -v ydotool >/dev/null || FALTAN+=(ydotool)
command -v wl-copy >/dev/null || FALTAN+=(wl-clipboard)
command -v notify-send >/dev/null || FALTAN+=(libnotify)
if [[ ${#FALTAN[@]} -gt 0 ]]; then
    decir "faltan: ${FALTAN[*]}"
    if command -v pacman >/dev/null; then
        sudo -n pacman -S --needed --noconfirm "${FALTAN[@]}" || {
            echo "no pude instalarlos; instalalos a mano y volvé a correr" >&2; exit 1; }
    else
        echo "instalá a mano: ${FALTAN[*]}" >&2; exit 1
    fi
else
    decir "python3, ffmpeg, ydotool, wl-clipboard y notify-send: ya están"
fi

titulo "Programa"
mkdir -p "$DEST"
install -m 644 "$DIR/servidor.py" "$DEST/servidor.py"
install -m 644 "$DIR/motor.py" "$DEST/motor.py"
decir "copiado a $DEST"

if [[ ! -x "$VENV/bin/python" ]]; then
    decir "creando entorno de Python…"
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
decir "instalando faster-whisper y fastapi (la primera vez tarda)…"
"$VENV/bin/pip" install -q fastapi "uvicorn[standard]" python-multipart faster-whisper

# 🛡️ LAS LIBRERÍAS CUDA VAN EN EL VENV, NO EN EL SISTEMA: faster-whisper las
# busca al cargar el modelo (`libcublas.so.12`) y sin esto la primera
# transcripción muere con "Library libcublas.so.12 is not found".
GPULIBS=""
if command -v nvidia-smi >/dev/null; then
    decir "descargando cuBLAS y cuDNN de CUDA 12 (para la GPU)…"
    "$VENV/bin/pip" install -q nvidia-cublas-cu12 nvidia-cudnn-cu12 || true
    SITIO="$("$VENV/bin/python" -c 'import site;print(site.getsitepackages()[0])')"
    if [[ -d "$SITIO/nvidia/cublas/lib" ]]; then
        GPULIBS="Environment=LD_LIBRARY_PATH=$SITIO/nvidia/cublas/lib:$SITIO/nvidia/cudnn/lib"
        decir "GPU: cuBLAS/cuDNN listos"
    fi
else
    decir "sin nvidia-smi: va a transcribir en CPU (más lento; usá --modelo small)"
fi

titulo "Servicio"
mkdir -p "$(dirname "$UNIDAD")"
cat > "$UNIDAD" <<EOF
[Unit]
Description=Dictado (servidor de voz para el telefono)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.local/share/dictado-pc
Environment=PYTHONUNBUFFERED=1
Environment=HF_HOME=%h/.cache/huggingface
Environment=DICTADO_PUERTO=$PUERTO
Environment=DICTADO_MODELO=$MODELO
Environment="DICTADO_EQUIPO=${NOMBRE:-$(hostname)}"
# Minutos sin dictar antes de soltar el modelo y devolver la VRAM (0 = nunca).
Environment=DICTADO_DESCARGAR_MIN=30
$GPULIBS
ExecStart=%h/.local/share/dictado-pc/.venv/bin/uvicorn servidor:app --host 0.0.0.0 --port $PUERTO --log-level warning
Restart=on-failure
RestartSec=4
TimeoutStopSec=20

[Install]
WantedBy=default.target
EOF
# El comando `dictado-pc` lee el puerto de acá.
mkdir -p "$HOME/.config/dictado-pc"
printf '%s\n' "$PUERTO" > "$HOME/.config/dictado-pc/puerto"
# Y queda en el PATH (si ~/.local/bin existe, cosa que en estas máquinas sí).
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/dictado-pc" "$HOME/.local/bin/dictado-pc"

systemctl --user daemon-reload
systemctl --user enable dictado-pc.service
# `restart` y no `start`: si ya estaba corriendo con otra config, la toma igual.
systemctl --user restart dictado-pc.service
decir "dictado-pc.service arriba (puerto $PUERTO, modelo $MODELO)"

titulo "Firewall"
if [[ "$UFW" == "1" ]] && command -v ufw >/dev/null && sudo -n ufw status 2>/dev/null | grep -q "Status: active"; then
    RED="$(ip -brief addr | awk '{print $3}' | cut -d/ -f1 | grep -E '^192\.168\.' | head -1 || true)"
    if [[ -n "$RED" ]]; then
        SUBNET="${RED%.*}.0/24"
        sudo -n ufw allow from "$SUBNET" to any port "$PUERTO" proto tcp comment "Dictado (APK)" >/dev/null 2>&1 \
            && decir "ufw: abierto $PUERTO/tcp para $SUBNET" \
            || decir "ufw: no pude agregar la regla (revisá a mano)"
    fi
    # La tailnet también dicta (el dueño usa otra PC fuera de la LAN).
    sudo -n ufw allow in on tailscale0 to any port "$PUERTO" proto tcp comment "Dictado (tailnet)" >/dev/null 2>&1 \
        && decir "ufw: abierto $PUERTO/tcp por tailscale0" || true
else
    decir "firewall: sin cambios"
fi

titulo "Comprobación"
for _ in $(seq 1 20); do
    if curl -s -m 2 "http://127.0.0.1:$PUERTO/api/salud" >/dev/null 2>&1; then break; fi
    sleep 1
done
if curl -s -m 3 "http://127.0.0.1:$PUERTO/api/salud" > /tmp/.dictado-salud.json 2>/dev/null; then
    decir "responde: $(cat /tmp/.dictado-salud.json)"
else
    decir "todavía no responde: mirá 'dictado-pc logs' (el modelo grande tarda en cargar)"
fi
rm -f /tmp/.dictado-salud.json

LAN="$(ip -brief addr | awk '{print $3}' | cut -d/ -f1 | grep -E '^192\.168\.' | head -1 || true)"
TS="$(tailscale ip -4 2>/dev/null | head -1 || true)"
titulo "Listo"
decir "PC:       $(hostname)"
decir "LAN:      ${LAN:-?}:$PUERTO"
decir "Tailscale: ${TS:-—}:$PUERTO"
decir ""
decir "En el telefono: abrí Dictado → mantené la bola → elegí ${NOMBRE:-$(hostname)}."
decir "Aparece sola cuando el teléfono y la PC están en la misma red."
decir ""
decir "Comandos: dictado-pc estado | url | reiniciar | logs | apagar"
