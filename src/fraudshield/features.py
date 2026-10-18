"""Shared, stateless features for training and inference."""

import numpy as np
import pandas as pd

INPUT_COLUMNS = ("type", "amount", "oldbalanceOrg")
TRANSACTION_TYPES = ("CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER")
FEATURE_NAMES = (
    "log_amount",
    "log_sender_balance",
    "amount_balance_fraction",
    "amount_exceeds_balance",
) + tuple(f"type_{name}" for name in TRANSACTION_TYPES)


def build_features(frame):
    """Return float64 features; reject extra columns and invalid inputs."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("Input must be a pandas DataFrame.")

    if (
        not frame.columns.is_unique
        or set(frame.columns) != set(INPUT_COLUMNS)
    ):
        raise ValueError("Input must contain exactly the three allowed columns.")

    if not frame["type"].isin(TRANSACTION_TYPES).all():
        raise ValueError("Unknown or missing transaction type.")

    try:
        values = frame[["amount", "oldbalanceOrg"]].to_numpy(
            dtype=np.float64, copy=True
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Amount and balance must be numeric.") from exc

    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("Amount and balance must be finite and nonnegative.")

    amount = values[:, 0]
    balance = values[:, 1]

    # Scale before division to avoid overflow for very large finite inputs.
    scale = np.maximum(np.maximum(amount, balance), 1.0)
    fraction = (amount / scale) / (
        amount / scale + balance / scale + 1.0 / scale
    )

    features = np.column_stack(
        [
            np.log1p(amount),
            np.log1p(balance),
            fraction,
            (amount > balance).astype(np.float64),
        ]
        + [
            (frame["type"].to_numpy() == kind).astype(np.float64)
            for kind in TRANSACTION_TYPES
        ]
    )

    if not np.isfinite(features).all():
        raise ValueError("Feature calculation produced nonfinite values.")

    return features
