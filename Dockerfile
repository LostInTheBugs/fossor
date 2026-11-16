FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest pytest-asyncio httpx

COPY . .

ENV PYTHONPATH=/app

# Non-root runtime user (uid 1000): files created under /data (backups, DB)
# belong to fossor on the host instead of root.  The image stays readable and
# /data writable for it.
RUN useradd --uid 1000 --create-home --shell /bin/bash fossor \
 && mkdir -p /app/data /data \
 && chown -R fossor:fossor /app /data

USER fossor

EXPOSE 8008

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8008"]
