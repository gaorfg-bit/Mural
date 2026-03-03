#!/bin/bash
# Wrapper pour lancer Mural installé dans /usr/share/mural
export PYTHONPATH="/usr/share/mural"
exec python3 -m mural.main "$@"