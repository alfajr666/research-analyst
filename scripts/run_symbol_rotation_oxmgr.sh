#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/research-analyst
export PYTHONPATH=/home/ubuntu/research-analyst/src/research_analyst
export PYTHONUNBUFFERED=1
exec /home/ubuntu/research-analyst/venv/bin/python src/research_analyst/symbol_rotation.py
