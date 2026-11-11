# PaySim dataset

## Source
Dataset: Synthetic Financial Datasets For Fraud Detection
Publisher: ealaxi on Kaggle
URL: https://www.kaggle.com/datasets/ealaxi/paysim1
Downloaded: 2026-10-18

PaySim contains synthetic payment transactions, not real production traffic.

## Local input
Original archive member: PS_20174392719_1491204439457_log.csv
Project path: data/raw/paysim.csv
Size: 493534783 bytes
SHA-256: 16910f90577b0d981bf8ff289714510bb89bc71bff7d3f220f024e287e4eea6b

The raw CSV is excluded from Git. It is tracked by DVC through `paysim.dvc` (md5
`e92a5f7447f43712f1dca473d0b0fa85`, 493534783 bytes) and is the input of the `spark_prepare`
stage in `dvc.yaml`. `.dvc/config` defines a remote `localstore`, the folder
`../fraudshield-dvc-remote` next to the repository, which is empty until its owner runs
`dvc push`, so a fresh clone must download the file from Kaggle (see README.md).

## Verified validation results
Rows: 6362620
Legitimate: 6354407
Fraudulent: 8213
Labeled fraud rate: approximately 0.1291%
Step range: 1 through 743
Step decreases in file order: 0
Validation errors: 0

Validator: scripts/validate_paysim.py
Report: reports/paysim_validation.json

Validation checks the header, row width, missing values, transaction types,
finite nonnegative financial values, positive integer steps, and binary labels.
It does not establish absence of duplicates, target leakage, or real-world validity.

## Re-running validation
Activate .venv and run:
    python scripts/validate_paysim.py

The validator refuses to overwrite an existing report.
Preserve or rename the old report before intentionally re-running.
