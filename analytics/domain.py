"""数据产品与云账户使用方的当前目录。"""
class ProductDirectory:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS products(product TEXT PRIMARY KEY, owner TEXT, account TEXT)")

    def register(self, product, owner, account):
        prior = self.connection.execute("SELECT owner,account FROM products WHERE product=?", (product,)).fetchone()
        if prior and tuple(prior) != (owner,account): raise ValueError("产品归属已存在")
        self.connection.execute("INSERT OR IGNORE INTO products VALUES(?,?,?)", (product,owner,account))

    def account(self, product):
        row = self.connection.execute("SELECT account FROM products WHERE product=?", (product,)).fetchone()
        return row[0] if row else None

    def get(self, product):
        row = self.connection.execute(
            "SELECT product,owner,account FROM products WHERE product=?", (product,)).fetchone()
        return dict(row) if row else None

    def all(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT product,owner,account FROM products ORDER BY product").fetchall()]

    def account_products(self, account):
        return [row[0] for row in self.connection.execute(
            "SELECT product FROM products WHERE account=? ORDER BY product", (account,)).fetchall()]
