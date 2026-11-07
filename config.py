import os
import re
from dotenv import load_dotenv

load_dotenv()

schema = os.getenv("DB_SCHEMA", "djmix_hub")

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
    raise ValueError("Invalid DB_SCHEMA name")

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    DB_SCHEMA = schema

    VAPID_PUBLIC_KEY = os.getenv(
        "VAPID_PUBLIC_KEY",
        "BIbUKbsJEYYEEXLfy-A0wNC9Ihp3AqrkAVlpunJp8T8f0hMmrxizHqISeYHFVci83jMYTVQT_woGpi9Lfn7s67c"
    )
    VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
    VAPID_PRIVATE_KEY_PATH = os.getenv(
        "VAPID_PRIVATE_KEY_PATH",
        os.path.join(
            os.path.expanduser("~"),
            ".config",
            "dj-mix-hub",
            "private_key.pem"
        )
    )
    VAPID_SUBJECT = os.getenv(
        "VAPID_SUBJECT",
        "mailto:5910henry@gmail.com"
    )
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_timeout": 30,
        "connect_args": {
            "options": f"-csearch_path={schema},public",
            "connect_timeout": 15,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3
        }
    }
