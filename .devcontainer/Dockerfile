FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# The base image ships a Yarn APT source whose signing key is no longer valid
# (NO_PUBKEY 62D54FD4003F6525). Every `apt-get update` – e.g. inside the devcontainer
# features – then fails. Yarn is not needed here, so the source is removed.
RUN rm -f /etc/apt/sources.list.d/yarn.list
