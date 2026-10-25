from sqlmodel import Session, create_engine
from app.config import settings

engine = create_engine(settings.DATABASE_URL)

def get_session():
    with Session(engine) as session:
        yield session# Provides the database session to the endpoint and allows cleanup after the request.