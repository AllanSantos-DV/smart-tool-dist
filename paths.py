"""Where Smart Tool keeps its state: ~/.smart-tool (config.json and model adapters) and ~/.smart-tool/data (indexes,
daemon registry, caches, metrics). Pure stdlib so the installer can import it too."""
import os

HOME_DIR = os.path.join(os.path.expanduser("~"), ".smart-tool")
DATA_DIR = os.path.join(HOME_DIR, "data")
