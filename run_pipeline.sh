#!/bin/bash
set -e
source .venv/bin/activate
python -m pipeline.run_pipeline
