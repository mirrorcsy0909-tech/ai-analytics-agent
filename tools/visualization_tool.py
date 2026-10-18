"""Validated chart inputs, contained unique filenames, serialized plotting."""

from pathlib import Path
from threading import RLock
from uuid import uuid4

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "outputs"
_PLOT_LOCK = RLock()


def _validate(df, x_col, y_col, filename):
    if not isinstance(df, pd.DataFrame) or df.empty or len(df) > 1000:
        raise ValueError("Chart data must have between 1 and 1000 rows.")
    if not df.columns.is_unique or x_col not in df or y_col not in df:
        raise ValueError("Chart columns must exist and have unique names.")
    if df[x_col].isna().any() or df[x_col].astype(str).duplicated().any():
        raise ValueError("Chart categories must be nonmissing and unique; aggregate duplicates first.")
    if not is_numeric_dtype(df[y_col]) or is_bool_dtype(df[y_col]) or not np.isfinite(df[y_col].to_numpy(dtype=float)).all():
        raise ValueError("Chart values must be finite numeric data.")
    if not isinstance(filename, str) or len(filename) > 160 or Path(filename).name != filename or "\\" in filename or Path(filename).suffix.lower() != ".png":
        raise ValueError("Use a plain PNG filename inside the chart directory.")
    root = Path(OUTPUT_DIR).resolve()
    if not (root / filename).resolve().is_relative_to(root):
        raise ValueError("Chart filename escapes the output directory.")
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{Path(filename).stem}_{uuid4().hex}.png"


def _create_chart(df, x_col, y_col, title, filename, kind):
    path = _validate(df, x_col, y_col, filename)
    with _PLOT_LOCK:
        figure = plt.figure(figsize=(10, 6))
        try:
            if kind == "bar":
                plt.bar(df[x_col].astype(str), df[y_col])
            else:
                plt.plot(df[x_col].astype(str), df[y_col], marker="o")
            plt.title(title)
            plt.xlabel(x_col)
            plt.ylabel(y_col)
            plt.xticks(rotation=45, ha="right")
            plt.tight_layout()
            with path.open("xb") as output:
                figure.savefig(output, format="png")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            plt.close(figure)
    return str(path)


def create_bar_chart(df: pd.DataFrame, x_col: str, y_col: str, title: str, filename: str) -> str:
    return _create_chart(df, x_col, y_col, title, filename, "bar")


def create_line_chart(df: pd.DataFrame, x_col: str, y_col: str, title: str, filename: str) -> str:
    return _create_chart(df, x_col, y_col, title, filename, "line")
