"""Point d'entrée Gunicorn / Railway."""
from app import app

if __name__ == "__main__":
    app.run()
