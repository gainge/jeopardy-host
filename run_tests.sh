#!/bin/sh
# Stdlib unittest only. No pip, no virtualenv, no test runner package.
set -e
cd "$(dirname "$0")"
exec python3 -m unittest discover -s tests -p 'test_*.py' -v "$@"
