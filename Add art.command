#!/bin/zsh
# Double-click to publish everything in inbox/paintings and inbox/photos.
cd "${0:A:h}"
PY=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
[[ -x $PY ]] || PY=python3
$PY tools/art.py
[[ $? -eq 2 ]] && open inbox
echo
read -k1 "?Press any key to close this window."
