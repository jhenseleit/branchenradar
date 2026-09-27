# Branchenradar – Werkbank-Modul
FROM python:3.12-slim

WORKDIR /app

# Echte Schriftarten für die Text-Slides (sonst nur Pillow-Standardschrift)
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-liberation2 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py auth.py ./

# Persistentes Verzeichnis (Sliplane-Volume unter /data einhängen)
RUN mkdir -p /data

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
