#!/usr/bin/env bash

SCRIPT_DIR=$(dirname "$(realpath "$0")")
cd $SCRIPT_DIR

source .venv/bin/activate
python3 database.py

gunicorn -w 1 -b 0.0.0.0:5010 index:application
