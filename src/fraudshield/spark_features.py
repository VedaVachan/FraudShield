"""Spark equivalents of the shared fraud features."""

from pyspark.sql import functions as F

from fraudshield.features import (
    FEATURE_NAMES,
    INPUT_COLUMNS,
    TRANSACTION_TYPES,
)


def invalid_input_expression():
    """Boolean expression identifying invalid model inputs."""
    kind = F.col("type")
    invalid = kind.isNull() | ~kind.isin(*TRANSACTION_TYPES)

    for name in ("amount", "oldbalanceOrg"):
        value = F.col(name).cast("double")
        invalid = (
            invalid
            | value.isNull()
            | F.isnan(value)
            | (F.abs(value) == F.lit(float("inf")))
            | (value < 0)
        )

    return F.coalesce(invalid, F.lit(True))


def feature_expressions():
    """Feature expressions for an already validated Spark DataFrame."""
    amount = F.col("amount").cast("double")
    balance = F.col("oldbalanceOrg").cast("double")
    scale = F.greatest(amount, balance, F.lit(1.0))

    fraction = (amount / scale) / (
        amount / scale + balance / scale + F.lit(1.0) / scale
    )

    expressions = [
        F.log1p(amount),
        F.log1p(balance),
        fraction,
        (amount > balance).cast("double"),
    ]
    expressions.extend(
        (F.col("type") == kind).cast("double")
        for kind in TRANSACTION_TYPES
    )

    return [
        expression.alias(name)
        for expression, name in zip(expressions, FEATURE_NAMES)
    ]


def build_spark_features(frame):
    """Validate exactly three input columns and return nine features."""
    if (
        len(frame.columns) != len(INPUT_COLUMNS)
        or set(frame.columns) != set(INPUT_COLUMNS)
    ):
        raise ValueError("Input must contain exactly the three allowed columns.")

    if frame.filter(invalid_input_expression()).limit(1).count():
        raise ValueError("Invalid transaction type, amount, or sender balance.")

    return frame.select(*feature_expressions())
