#!/bin/bash
# Startup wrapper for the trading bot under Nixpacks.
#
# Why this exists: Nix-built Python (which is what Nixpacks uses by
# default) is hermetic — its glibc dlopen consults RPATH + LD_LIBRARY_PATH
# but NOT /etc/ld.so.cache. So the libstdc++ / libz / etc that numpy
# needs (apt-installed at /usr/lib/x86_64-linux-gnu/) is invisible
# unless we hand the loader the path explicitly.
#
# Multiple cleaner attempts failed:
#   - Setting LD_LIBRARY_PATH as a Railway env var: applied at BUILD too,
#     overriding Nix's own LD_LIBRARY_PATH and breaking pip install.
#   - Inline `LD_LIBRARY_PATH=... python ...` in startCommand: Railway
#     exec's the command without a shell, so the prefix doesn't parse.
#   - `/usr/bin/env LD_LIBRARY_PATH=...` wrap: server started but
#     Railway healthcheck consistently rolled the deploy back.
#   - ldconfig + symlinks during install phase: Nix Python ignores
#     ld.so.cache; symlinks in /usr/lib don't survive the runtime
#     image layer copy.
#
# This script runs as the entry point, sets the env var in its own
# shell, and exec's python — exec replaces the shell so PID 1 is
# python (Railway's healthcheck + signal handling work normally).
export LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:/usr/lib:${LD_LIBRARY_PATH:-}"
exec python3 -u server.py
