#!/bin/bash
# App Service passes a Key Vault reference's own text when it cannot resolve it. That text must never become a
# password or signing key: an admin created with it keeps it, and sessions signed with it can be forged.
unresolved=()
for name in $(compgen -e); do
  [[ ${!name} == '@Microsoft.KeyVault('* ]] && unresolved+=("$name")
done
if (( ${#unresolved[@]} )); then
  echo >&2 "Not starting: these settings hold an unresolved Key Vault reference: ${unresolved[*]}." \
    "Restart the app once it can read the secrets."
  exit 1
fi
