FROM alpine:3.18

RUN apk add --no-cache stunnel openssl

COPY stunnel.conf /etc/stunnel/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

VOLUME /tls

ENTRYPOINT ["/docker-entrypoint.sh"]

