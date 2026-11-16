FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest pytest-asyncio httpx

COPY . .

RUN chmod +x /app/docker-entrypoint.sh

ENV PYTHONPATH=/app

# Non-root runtime user (uid 1000): files created under /data (backups, DB)
# belong to fossor on the host instead of root.  The image stays readable and
# /data writable for it.
RUN useradd --uid 1000 --create-home --shell /bin/bash fossor \
 && mkdir -p /app/data /data \
 && chown -R fossor:fossor /app /data

USER fossor

EXPOSE 8008

# §4.18 : lors d'un retour arrière, l'updater recrée le service avec
# FOSSOR_RESTORE_BACKUP=<sauvegarde> ; l'entrée restaure la base *avant* que le
# code restauré ne l'ouvre (l'updater, lui, ne peut pas écrire dans /data).
ENTRYPOINT ["/app/docker-entrypoint.sh"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8008"]
