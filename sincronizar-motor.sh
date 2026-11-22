#!/usr/bin/env bash
# Copia el motor de `dictado-remoto` (la PC que ya lo tiene) a esta app.
#
# 🛡️ POR QUÉ EXISTE: el motor (modelo, prompt, hotwords, VAD, guarda de eco) es
# UNO solo en la casa. Se arregla allá y se copia acá; nunca se edita acá.
#
#   ./sincronizar-motor.sh                   # busca ~/dictado-remoto
#   ./sincronizar-motor.sh /ruta/al/repo     # o la ruta que le digas
#   ./sincronizar-motor.sh --github          # baja el último de GitHub
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:-$HOME/dictado-remoto}"

if [[ "$REPO" == "--github" ]]; then
    curl -fsSL "https://raw.githubusercontent.com/hazaelfguzmanc-glitch/dictado-remoto/main/motor.py" \
        -o "$DIR/motor.py"
    echo "motor.py bajado de GitHub"
else
    [[ -f "$REPO/motor.py" ]] || { echo "no encontré $REPO/motor.py" >&2; exit 1; }
    cp "$REPO/motor.py" "$DIR/motor.py"
    echo "motor.py copiado de $REPO"
fi

# Si está instalado, se reinicia para que tome el motor nuevo.
if systemctl --user is-active --quiet dictado-pc.service 2>/dev/null; then
    install -m 644 "$DIR/motor.py" "$HOME/.local/share/dictado-pc/motor.py"
    systemctl --user restart dictado-pc.service
    echo "dictado-pc reiniciado"
fi
