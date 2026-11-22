FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends squid-deb-proxy python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY squid.conf /etc/squid-deb-proxy/ui-squid.conf.template
COPY mirrors.acl /etc/squid-deb-proxy/ui-mirrors.acl
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
