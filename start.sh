#!/bin/bash
# Startup wrapper. Points LD_LIBRARY_PATH at a NARROW dir containing
# only the libs numpy actually needs (libstdc++.so.6, libz.so.1,
# libffi.so.8) symlinked into /opt/venv/native-libs at build time.
#
# Crucially we do NOT add /usr/lib/x86_64-linux-gnu in full — that
# pulled in the system glibc on top of Nix glibc and killed the
# Python interpreter at startup with:
#     python3: error while loading shared libraries: __vdso_time:
#         invalid mode for dlopen(): Invalid argument
# A scoped dir keeps numpy happy without polluting libc resolution.
#
# Round-61 pt.14: switched from `python3` (Nix's system interpreter,
# which has no access to /opt/venv/lib/python3.X/site-packages) to
# `/opt/venv/bin/python` (the venv interpreter installed at build
# time with `python -m venv --copies /opt/venv`). User-reported
# symptom on Railway was `ModuleNotFoundError: No module named
# 'cryptography'` even though requirements.txt has it pinned —
# because pip installed cryptography into the venv at /opt/venv but
# the runtime was running Nix's system python which couldn't see it.
# Other deps (yfinance, sentry-sdk, websocket-client) only "worked"
# by accident — they're either pure-Python in stdlib paths the Nix
# python could pick up, or imported lazily and silently failed when
# loaded for the first time. Cryptography failed loudly because
# auth.py imports it at module load.
exec 2>&1
echo "[start.sh] LD_LIBRARY_PATH=/opt/venv/native-libs"
export LD_LIBRARY_PATH="/opt/venv/native-libs:${LD_LIBRARY_PATH:-}"

# Pick the venv's python first; fall back to system python3 only if
# the venv was somehow lost (e.g. a Nixpacks regression that wipes
# /opt/venv between build and runtime layers).
if [ -x "/opt/venv/bin/python" ]; then
    PY="/opt/venv/bin/python"
elif [ -x "/opt/venv/bin/python3" ]; then
    PY="/opt/venv/bin/python3"
else
    echo "[start.sh] WARNING: /opt/venv/bin/python not found — falling"
    echo "[start.sh] back to system python3. Pip-installed deps"
    echo "[start.sh] (cryptography, yfinance, sentry-sdk) will be"
    echo "[start.sh] missing. The dashboard will surface this via the"
    echo "[start.sh] 'Encryption broken on this deployment' banner."
    PY="python3"
fi

# Boot-time visibility check: verify cryptography is importable BEFORE
# starting the server so the build log shows the actual import error
# instead of leaving the user to discover it via "Save Paper Keys".
echo "[start.sh] Using python: $PY"
"$PY" -c "import sys; print('[start.sh] python', sys.version.split()[0], 'sys.prefix=', sys.prefix)" || true
"$PY" -c "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; print('[start.sh] cryptography.AESGCM OK')" \
    || echo "[start.sh] WARNING: cryptography import failed — saved Alpaca creds will be inaccessible until rebuild."

echo "[start.sh] exec $PY -u server.py"
exec "$PY" -u server.py
