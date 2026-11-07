# Provisioning and dashboards are built in rather than bind-mounted, so a change
# to either changes the image and recreates the container on the next deploy.
ARG GRAFANA_VERSION=12.4.0
FROM grafana/grafana:${GRAFANA_VERSION}

# metric or imperial. Any other value fails the build.
ARG VELOMATE_UNITS=metric
COPY provisioning /etc/grafana/provisioning
COPY dashboards/${VELOMATE_UNITS} /etc/grafana/dashboards
