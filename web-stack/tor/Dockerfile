FROM alpine:3.24.1
RUN apk add --no-cache tor
COPY torrc /etc/tor/torrc
USER tor
CMD ["tor", "-f", "/etc/tor/torrc"]
