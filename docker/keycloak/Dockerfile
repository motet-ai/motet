FROM quay.io/keycloak/keycloak:26.4.0

COPY realm-motet.json /opt/keycloak/data/import/realm-motet.json

ENV KEYCLOAK_IMPORT=/opt/keycloak/data/import/realm-motet.json

ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
CMD ["start-dev", "--import-realm", "--features=preview,organization,scripts", "--http-enabled=true", "--hostname-strict=false"]

