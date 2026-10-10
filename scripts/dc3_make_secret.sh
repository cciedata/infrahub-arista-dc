#!/bin/bash
# write the dc3-automation password and its EOS sha512 hash (SHA-512 crypt, openssl passwd -6) to ~/.dc3/secrets.env
# (mode 600). Prompts twice; keeps every other line already in the file. Never prints the password.
set -eu
F=${DC3_SECRETS:-$HOME/.dc3/secrets.env}
mkdir -p "$(dirname "$F")"; chmod 700 "$(dirname "$F")"; touch "$F"; chmod 600 "$F"
read -rsp "dc3-automation password: " p1; echo; read -rsp "again: " p2; echo
[ -n "$p1" ] && [ "$p1" = "$p2" ] || { echo "passwords empty or different"; exit 1; }
case "$p1" in *"'"*|*'"'*) echo "the password must not contain quote characters (' or \")"; exit 1 ;; esac
h=$(printf '%s' "$p1" | openssl passwd -6 -stdin)
grep -vE '^(DC3_AUTOMATION_PASSWORD|DC3_AUTOMATION_HASH)=' "$F" > "$F.tmp" || true
printf "DC3_AUTOMATION_PASSWORD='%s'\nDC3_AUTOMATION_HASH='%s'\n" "$p1" "$h" >> "$F.tmp"; mv "$F.tmp" "$F"; chmod 600 "$F"
echo "written: $F (DC3_AUTOMATION_PASSWORD, DC3_AUTOMATION_HASH ${h:0:3}...)"
