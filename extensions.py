from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import MetaData
from config import Config

db = SQLAlchemy(
    metadata=MetaData(schema=Config.DB_SCHEMA),
    session_options={
        "expire_on_commit": False
    }
)
