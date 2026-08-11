import duckdb

for db in ["btc_options.duckdb", "eth_options.duckdb"]:
    print(f"\n== {db} ==")
    con = duckdb.connect(db, read_only=True)
    df = con.execute("""
        select strftime(timestamp, '%Y-%m') as month,
               count(*) as records
        from options_trades
        group by 1
        order by 1
    """).fetchdf()
    con.close()
    print(df.to_string(index=False))
