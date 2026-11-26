# Dictado PC

El servidor de dictado por voz para una PC Linux, y nada más: el teléfono manda
la nota de voz, la PC transcribe y la pega en la ventana que tenga el foco.

**Lo que lo hace distinto: aparece solo en el selector de servidores de la APK.**
No hay que escribir una IP. La PC contesta el aviso `DICTADO?` que manda el
teléfono al broadcast de la red (UDP 8099) y se presenta con el nombre del
equipo; en la app se cambia de PC manteniendo la bola → *PC / Cambiar*.

Es la parte de `dictado-remoto` que usa el teléfono, sin bot de Telegram ni la
web: dictado por voz **y** las imágenes que mandás (capturas de pantalla y la
pestaña Escanear). Sin el escaneo, "Enviar a la PC" de una captura fallaría al
tener esta PC elegida en el selector.

## Instalar

```bash
git clone https://github.com/hazaelfguzmanc-glitch/dictado-pc
cd dictado-pc
./instalar.sh
```

Qué hace:

1. Instala lo que falte (`ffmpeg`, `ydotool`, `wl-clipboard`, `libnotify`) con
   pacman.
2. Copia el programa a `~/.local/share/dictado-pc` y arma su entorno de Python
   con `faster-whisper`.
3. Deja `dictado-pc.service` (systemd --user) prendido y habilitado.
4. Abre el puerto en `ufw` para la LAN y para la tailnet.
5. Imprime la dirección LAN, la de Tailscale y qué elegir en el teléfono.

Opciones: `--puerto 8199`, `--modelo small`, `--sin-ufw`.

## Usarlo

```bash
dictado-pc           # estado (modelo, GPU, quién dicta)
dictado-pc url       # direcciones de esta PC
dictado-pc logs      # en vivo
dictado-pc reiniciar
dictado-pc probar nota.m4a
dictado-pc apagar
dictado-pc desinstalar
```

Para que `dictado-pc` esté en el PATH:

```bash
ln -sf ~/Documents/GitHub/dictado-pc/dictado-pc ~/.local/bin/dictado-pc
```

## En el teléfono

1. Dictado → **mantené la bola** → *PC / Cambiar*.
2. La PC aparece con su nombre de equipo (LinuxWork, Hazaelspc…) y la dirección.
3. Elegila: desde ahí todo (bola flotante y capturas) le habla a esa PC.

Si el teléfono está fuera de la casa, la PC aparece igual si está en la tailnet y
la elegís desde "Otra dirección a mano" (o queda en la lista de fijas del build).

## Cómo está armado

| Archivo | Qué es |
|---|---|
| `servidor.py` | Los endpoints de la APK (`/api/salud`, `/api/parcial`, `/api/transcribir`, `/api/escanear`, `/api/escaneos`) y el aviso UDP del selector. |
| `motor.py` | El motor de transcripción: **una copia** de `dictado-remoto/motor.py`. |
| `escaner.py` | Guarda el escaneo **en JPEG** en `~/Escaneos`, lo deja en el portapapeles y lo pega con Ctrl+V. |
| `instalar.sh` | Paquetes, entorno, servicio y firewall. |
| `dictado-pc` | El comando de control. |
| `sincronizar-motor.sh` | Trae el motor actualizado desde `dictado-remoto`. |

🛡️ **El motor NO se edita acá.** Se arregla en `dictado-remoto` (que es donde
están los audios de prueba y el bot) y se copia:

```bash
./sincronizar-motor.sh            # desde ~/dictado-remoto
./sincronizar-motor.sh --github   # o el último publicado
```

Hoy el motor es faster-whisper `large-v3` en `cuda/float16` con prompt de estilo,
`hotwords`, VAD de 2000 ms y guarda de eco (ver el README de `dictado-remoto`).

## Capturas y escaneos

Mismo camino que el dictado: el teléfono arma el PDF y esta PC lo deja en
`~/Escaneos`, lo copia al portapapeles como archivo y lo pega (Ctrl+V) en la
ventana enfocada. Es lo que usan el aviso "Enviar a la PC" de una captura de
pantalla y la pestaña Escanear. Si el Ctrl+V no entra, el archivo queda guardado
y copiado igual.

🛡️ **Se guarda en JPEG, no en PDF** (2026-11-26, lo pidió el dueño: *"no debe
convertirlo a pdf, debe hacerlo por jpeg mejor"*). El PDF que manda el teléfono
se rasteriza con `pdftoppm` (poppler, la primera hoja a 150 dpi) y queda
`escaneo-AAAA-MM-DD_HHMMSS.jpg`. El JPEG se ve en cualquier lado y se adjunta
igual con Ctrl+V. Si falta poppler o el PDF no se puede leer, queda el PDF de
siempre: nunca se pierde el escaneo.

## Puertos

- **TCP 8099**: el API. La APK lo usa para transcribir.
- **UDP 8099**: el aviso "¿quién dicta acá?". Es el que llena el selector.

Dos PCs pueden correr cada una en 8099 (son máquinas distintas): el teléfono ve
las dos y elige. Si querés dos en la MISMA PC, la segunda va con `--puerto`.
