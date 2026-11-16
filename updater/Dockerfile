# fossor-updater — le seul conteneur qui monte le socket Docker (§4.18).
# Base « équivalent docker:27-cli » : Python (machine à états lisible et
# testable) + le client Docker et le plugin compose installés par apk.
FROM python:3.12-alpine

RUN apk add --no-cache docker-cli docker-cli-compose tzdata

WORKDIR /app
# Le contexte de build est ce dossier (`build: ./updater` dans le compose).
COPY updater.py /app/updater.py

# Même identité que l'application : le `.env` du compose appartient à l'uid
# 1000 sur l'hôte, et le groupe `docker` du socket est ajouté à l'exécution
# (`group_add: ["${DOCKER_GID:-999}"]` dans docker-compose.yml).
RUN adduser --uid 1000 --disabled-password --gecos "" fossor
USER 1000:1000

ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "/app/updater.py"]
