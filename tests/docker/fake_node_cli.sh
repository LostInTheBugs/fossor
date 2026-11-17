#!/bin/sh
# Faux client de nœud : getters seulement, et un « generatetoaddress » qui
# bloque (comme le vrai) pour rester visible dans ps.
case "$*" in
  *getmininginfo*)     echo '{"chain":"main","blocks":21458,"difficulty":128.421,"networkhashps":18400000000.0}' ;;
  *getblockchaininfo*) echo '{"chain":"main","blocks":21458,"difficulty":128.421}' ;;
  *getnetworkhashps*)  echo '18400000000.0' ;;
  *generatetoaddress*) while :; do sleep 5; done ;;
  *) exit 1 ;;
esac
