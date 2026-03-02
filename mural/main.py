import sys
import os

# Force path to find 'mural' package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    # The class name MUST be MuralApplication
    from mural.app import MuralApplication
except ImportError:
    from app import MuralApplication

if __name__ == "__main__":
    app = MuralApplication()
    sys.exit(app.run(sys.argv))