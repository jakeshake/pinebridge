# dockur/windows plus this repo's MT5 provisioning files baked in, so a user
# only has to fill in template variables -- no cloning the repo onto the host.
FROM dockurr/windows:6.05

COPY oem /opt/tv-mt5/oem
COPY --chmod=755 entry.sh /run/tv-mt5-entry.sh

ENV VERSION="11" \
    RAM_SIZE="8G" \
    USER_PORTS="3389,5555"

EXPOSE 5555

ENTRYPOINT ["/usr/bin/tini", "-s", "/run/tv-mt5-entry.sh"]
