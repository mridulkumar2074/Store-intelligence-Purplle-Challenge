"""Convert Brigade Bangalore sales CSV to the required POS transaction format."""
import pandas as pd
import sys
import os

def convert_pos(input_csv: str, output_csv: str):
    df = pd.read_csv(input_csv)

    # Filter only sales (not returns)
    df = df[df['invoice_type'] == 'sales'].copy()

    # Parse timestamp from order_date + order_time
    df['timestamp'] = pd.to_datetime(
        df['order_date'].astype(str) + ' ' + df['order_time'].astype(str),
        format='%d-%m-%Y %H:%M:%S', dayfirst=True, errors='coerce'
    )
    df = df.dropna(subset=['timestamp'])
    df['timestamp'] = df['timestamp'].dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    # Map store_id
    df['store_id'] = 'STORE_BLR_002'

    # Group by order_id to get one row per transaction
    txn = df.groupby('order_id').agg(
        transaction_id=('invoice_number', 'first'),
        store_id=('store_id', 'first'),
        timestamp=('timestamp', 'first'),
        basket_value_inr=('total_amount', 'sum')
    ).reset_index(drop=True)

    # Deduplicate by transaction_id (invoice_number is unique per order)
    txn = txn.drop_duplicates(subset='transaction_id')

    # Filter valid basket values
    txn = txn[txn['basket_value_inr'] > 0]

    txn = txn[['store_id', 'transaction_id', 'timestamp', 'basket_value_inr']]
    txn.to_csv(output_csv, index=False)
    print(f"Converted {len(txn)} transactions -> {output_csv}")

if __name__ == '__main__':
    base = os.path.dirname(os.path.dirname(__file__))
    src = os.path.join(base, 'Brigade_Bangalore_10_April_26 (1)bc6219c.csv')
    dst = os.path.join(base, 'data', 'pos_transactions.csv')
    convert_pos(src, dst)
