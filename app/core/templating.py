from pathlib import Path

from fastapi.templating import Jinja2Templates

# Não mover para app/main.py: os routers importam daqui e isso criaria import circular
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
