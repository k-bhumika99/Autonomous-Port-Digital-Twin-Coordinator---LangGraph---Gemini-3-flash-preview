"""Create the database (if needed) and add a demo account with the sample port.

Run from the project folder:

    python -m database.seed

Demo login:  demo@example.com  /  Demo@12345   (change or delete it for real use)
"""
from werkzeug.security import generate_password_hash

from config import Config
from database import db
from models.port_models import build_config
from services.config_service import sample_config_dict, save_config

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "Demo@12345"


def seed(path: str | None = None) -> None:
    path = path or Config.DATABASE
    db.init_db(path)
    conn = db.connect(path)
    try:
        row = db.query_one(conn, "SELECT id FROM users WHERE email = ?", (DEMO_EMAIL,))
        if row is None:
            cur = conn.execute("INSERT INTO users (full_name, email, password_hash) VALUES (?, ?, ?)",
                               ("Demo User", DEMO_EMAIL, generate_password_hash(DEMO_PASSWORD)))
            conn.commit()
            user_id = cur.lastrowid
            print(f"Created demo user {DEMO_EMAIL} / {DEMO_PASSWORD}")
        else:
            user_id = row["id"]
            print("Demo user already exists.")
        if db.query_one(conn, "SELECT id FROM port_configurations WHERE user_id = ?", (user_id,)) is None:
            config, errors, _ = build_config(sample_config_dict())
            assert config is not None, errors
            save_config(conn, user_id, config)
            print("Saved the sample port configuration for the demo user.")
    finally:
        conn.close()
    print(f"Database ready: {path}")


if __name__ == "__main__":
    seed()
