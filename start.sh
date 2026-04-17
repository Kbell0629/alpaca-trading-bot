#!/bin/bash
# Startup wrapper. Sets LD_LIBRARY_PATH so numpy can find libstdc++ /
# libz that live at /usr/lib/x86_64-linux-gnu/ (Nix Python is hermetic
# and ignores /etc/ld.so.cache).
#
# We redirect stderr → stdout so any startup crash (e.g. another C
# extension breaking because it was linked against Nix glibc but now
# picks up system libs) shows up in Railway's deploy logs instead of
# disappearing into the void.
exec 2>&1
echo "[start.sh] launching with LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/usr/lib"
export LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:/usr/lib:${LD_LIBRARY_PATH:-}"
echo "[start.sh] exec python3 -u server.py"
exec python3 -u server.py
