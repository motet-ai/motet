FROM python:3.11-slim

ARG KEYCLOAK_VERSION=26.4.0
ENV KC_HOME=/opt/keycloak

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates openjdk-21-jre-headless \
    && curl -sSL https://github.com/keycloak/keycloak/releases/download/${KEYCLOAK_VERSION}/keycloak-${KEYCLOAK_VERSION}.zip -o /tmp/keycloak.zip \
    && unzip /tmp/keycloak.zip -d /opt \
    && mv /opt/keycloak-${KEYCLOAK_VERSION} ${KC_HOME} \
    && rm -rf /var/lib/apt/lists/* /tmp/keycloak.zip

WORKDIR /app
COPY docker/keycloak/bootstrap_orgs.py /app/bootstrap_orgs.py

ENTRYPOINT ["python", "/app/bootstrap_orgs.py"]

