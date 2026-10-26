#!/usr/bin/env bash

set -euo pipefail

frontend_url="${FRONTEND_URL:-http://127.0.0.1:28080}"
cookie_file="$(mktemp /tmp/tcc-checkout-cookie.XXXXXX)"
trap 'rm -f "${cookie_file}"' EXIT

request() {
  local name="$1"
  shift
  curl --fail --silent --show-error --output /dev/null \
    --write-out "${name}=%{http_code}\n" \
    --cookie "${cookie_file}" --cookie-jar "${cookie_file}" "$@"
}

request home "${frontend_url}/"
request product "${frontend_url}/product/OLJCESPC7Z"
request add --request POST \
  --data 'product_id=OLJCESPC7Z&quantity=1' \
  "${frontend_url}/cart"
request cart "${frontend_url}/cart"

echo "checkout_start_us=$(date +%s%6N)"
request checkout --request POST \
  --data-urlencode 'email=tcc@example.com' \
  --data-urlencode 'street_address=1600 Amphitheatre Parkway' \
  --data-urlencode 'zip_code=94043' \
  --data-urlencode 'city=Mountain View' \
  --data-urlencode 'state=CA' \
  --data-urlencode 'country=United States' \
  --data-urlencode 'credit_card_number=4432801561520454' \
  --data-urlencode 'credit_card_expiration_month=1' \
  --data-urlencode "credit_card_expiration_year=$(( $(date +%Y) + 1 ))" \
  --data-urlencode 'credit_card_cvv=672' \
  "${frontend_url}/cart/checkout"
echo "checkout_end_us=$(date +%s%6N)"
