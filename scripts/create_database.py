import pandas as pd
import sqlite3
from pathlib import Path

# Project paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = BASE_DIR / "data" / "cleaned_online_retail.csv"
DB_PATH = BASE_DIR / "database" / "analytics.db"

# Read CSV
df = pd.read_csv(DATA_PATH)

# Basic check
print("CSV loaded successfully")
print("Shape:", df.shape)
print("Columns:", df.columns.tolist())

# Connect to SQLite database
conn = sqlite3.connect(DB_PATH)

# Save dataframe as SQL table
df.to_sql("transactions", conn, if_exists="replace", index=False)

# Test query
query = """
SELECT Country,
       SUM(Revenue) AS total_revenue
FROM transactions
GROUP BY Country
ORDER BY total_revenue DESC
LIMIT 10;
"""

result = pd.read_sql_query(query, conn)

print("\nTop 10 countries by revenue:")
print(result)

conn.close()

print("\nDatabase created successfully at:", DB_PATH)