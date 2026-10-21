from sqlmodel import Session, create_engine
from app.config import setting

engine = create_engine(setting.DATABASE_URL)

def get_session():
    with Session(engine) as session:
        yield session# Provides the database session to the endpoint and allows cleanup after the request.