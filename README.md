# FraudShield

A local, low-latency fraud detection service for digital payments.

## Environment
Ubuntu VM: 6 CPU cores, 8 GB RAM.
Python 3.11 in a dedicated .venv; CPU-based training.
Docker and Compose installed; Docker access verified.

## Activate
    cd /home/vedavachan/projects/fraudshield
    source .venv/bin/activate

The venv depends on the existing Conda daai Python installation.

## Current progress
Environment and Git exclusions verified.
Dataset processing, model training, and application development not started.
No model quality or latency results measured yet.

## Data policy
Keep datasets, generated models, and secrets out of ordinary Git history.
DVC data versioning will be added later.
