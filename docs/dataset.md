# The PaySim dataset

## Where it comes from

I used "Synthetic Financial Datasets For Fraud Detection", published by ealaxi on Kaggle:
https://www.kaggle.com/datasets/ealaxi/paysim1. I downloaded it on 2026-10-18.

PaySim is simulated mobile-money data. None of it is real customer traffic, so any
result in this project says something about the simulation, not about real fraud.

## The file I used

The CSV inside the Kaggle archive is called `PS_20174392719_1491204439457_log.csv`. In the
project it lives at `data/raw/paysim.csv`.

- Size: 493,534,783 bytes
- SHA-256: `16910f90577b0d981bf8ff289714510bb89bc71bff7d3f220f024e287e4eea6b`
- DVC md5 (from `paysim.dvc`): `e92a5f7447f43712f1dca473d0b0fa85`

The CSV is too big for Git, so DVC tracks it through `paysim.dvc`. It is the input of the
`spark_prepare` stage in `dvc.yaml`. The DVC remote in `.dvc/config` (`localstore`) is
just a folder next to the repository, `../fraudshield-dvc-remote`, and it stays empty
until its owner runs `dvc push`. In practice that means anyone cloning the repository has
to download the file from Kaggle themselves (the README explains where to put it).

## What the validation found

`scripts/validate_paysim.py` checked the file and wrote `reports/paysim_validation.json`:

| | |
|---|---|
| Rows | 6,362,620 |
| Legitimate | 6,354,407 |
| Fraudulent | 8,213 |
| Fraud rate | about 0.1291% |
| Steps | 1 to 743, never decreasing in file order |
| Validation errors | 0 |

The script checks the header, the number of columns per row, missing values, the
transaction types, that money values are finite and not negative, that steps are positive
integers and that the labels are 0 or 1. It does not look for duplicate rows or target
leakage, and it can't tell you whether the data is realistic.

## Running the validation again

With `.venv` active:

```bash
python scripts/validate_paysim.py
```

The script will not overwrite an existing report, so rename or move the old one first if
you really want to run it again.
