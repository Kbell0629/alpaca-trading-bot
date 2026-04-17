#!/bin/bash
# Startup wrapper. Points LD_LIBRARY_PATH at a NARROW dir containing
# only the libs numpy actually needs (libstdc++.so.6, libz.so.1)
# symlinked into /opt/venv/native-libs at build time.
#
# Crucially we do NOT add /usr/lib/x86_64-linux-gnu in full — that
# pulled in the system glibc on top of Nix glibc and killed the
# Python interpreter at startup with:
#     python3: error while loading shared libraries: __vdso_time:
#         invalid mode for dlopen(): Invalid argument
# A scoped dir keeps numpy happy without polluting libc resolution.
exec 2>&1
echo "[start.sh] LD_LIBRARY_PATH=/opt/venv/native-libs"
export LD_LIBRARY_PATH="/opt/venv/native-libs:${LD_LIBRARY_PATH:-}"
echo "[start.sh] exec python3 -u server.py"
exec python3 -u server.py
