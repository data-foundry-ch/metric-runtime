#!/usr/bin/env bash
set -euo pipefail
python examples/pypizza/generate_data.py
pytest -q
echo "Launch demo: marimo run examples/pypizza/app.py"
