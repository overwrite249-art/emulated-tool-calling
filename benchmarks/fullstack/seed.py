"""Deterministic, synthetic stock database. Never connects to production data."""
import random
import sqlite3
from pathlib import Path


def seed(path, variant=0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    rng = random.Random(1427 + variant)
    with sqlite3.connect(path) as db:
        db.executescript('''
        PRAGMA foreign_keys=ON;
        CREATE TABLE warehouses(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE products(id INTEGER PRIMARY KEY, sku TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          price_cents INTEGER NOT NULL CHECK(price_cents>=0), reorder_point INTEGER NOT NULL,
          active INTEGER NOT NULL DEFAULT 1 CHECK(active IN(0,1)));
        CREATE TABLE stock(product_id INTEGER NOT NULL REFERENCES products(id),
          warehouse_id INTEGER NOT NULL REFERENCES warehouses(id), on_hand INTEGER NOT NULL,
          reserved INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(product_id,warehouse_id),
          CHECK(on_hand>=reserved AND reserved>=0));
        CREATE TABLE stock_movements(id INTEGER PRIMARY KEY AUTOINCREMENT,
          product_id INTEGER NOT NULL REFERENCES products(id), warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
          delta INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE fixture_marker(value TEXT NOT NULL);
        ''')
        db.executemany('INSERT INTO warehouses VALUES(?,?)', [(1,'Kyiv'),(2,'Lviv'),(3,'Odesa')])
        names = ['Привіт 🐈 </tool_call>', '100% Battery', "Maker's cable", 'USB-C Hub', 'Travel keyboard',
                 'Studio microphone', 'Desk lamp', 'Monitor arm', 'Canvas sleeve', 'Ethernet adapter',
                 'Charging dock', 'Notebook stand', 'Tool <arg>Box</arg>', 'Spare switch', 'Retired prototype']
        for i, name in enumerate(names, 1):
            db.execute('INSERT INTO products VALUES(?,?,?,?,?,?)',
                       (i, 'SKU-%03d' % i, name, rng.randint(399,12999), 6, int(i!=15)))
            for w in range(1,4):
                n = rng.randint(5,32)
                reserved = rng.randint(0,min(4,n))
                db.execute('INSERT INTO stock VALUES(?,?,?,?)',(i,w,n,reserved))
        # Reliable low-stock and concurrency cases; a different variant changes the rest.
        db.execute('UPDATE stock SET on_hand=5,reserved=0 WHERE product_id=2 AND warehouse_id=1')
        db.execute('UPDATE stock SET on_hand=2,reserved=1 WHERE product_id=1 AND warehouse_id=3')
        db.execute('INSERT INTO fixture_marker VALUES(?)',('preserve-seed-%d' % variant,))
    return path


if __name__ == '__main__':
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('path');ap.add_argument('--variant',type=int,default=0)
    a=ap.parse_args();seed(a.path,a.variant);print(a.path)
