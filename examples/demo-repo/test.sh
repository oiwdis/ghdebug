#!/usr/bin/env bash
set -e
got="$(./greet.sh)"
if [ "$got" != "$EXPECTED_GREETING" ]; then
  echo "FAIL: expected '$EXPECTED_GREETING', got '$got'"
  exit 1
fi
echo "PASS"
