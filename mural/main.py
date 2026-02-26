import sys
import os

# Force le chemin pour trouver le package 'mural'
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    # Le nom de la classe DOIT être MuralApplication
    from mural.app import MuralApplication
except ImportError:
    from app import MuralApplication

if __name__ == "__main__":
    app = MuralApplication()
    sys.exit(app.run(sys.argv))