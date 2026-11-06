#!/bin/bash
# Mac/Linux: double-click (Mac) or run once from a file manager. First run installs everything.
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then echo "Python 3.10+ is required: https://www.python.org/downloads/"; read -p "Press Enter"; exit 1; fi
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt || { echo "Install failed"; read -p "Press Enter"; exit 1; }
mkdir -p ~/.streamlit
[ -f ~/.streamlit/credentials.toml ] || printf '[general]\nemail = ""\n' > ~/.streamlit/credentials.toml
.venv/bin/python -m streamlit run app.py
