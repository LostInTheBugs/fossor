# Ubuntu sshd + sudo, throwaway target for the provisioning integration test.
# Ubuntu (not Alpine) on purpose: HiveOS and Ubuntu expose useradd and visudo,
# which is exactly what the provisioning flow uses.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server sudo passwd \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash tester \
    && echo 'tester:testerpass' | chpasswd \
    && printf 'tester ALL=(ALL) ALL\n' > /etc/sudoers.d/tester \
    && chmod 0440 /etc/sudoers.d/tester \
    && ssh-keygen -A \
    && mkdir -p /run/sshd

EXPOSE 2222

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl python3 ca-certificates procps coreutils \
    && rm -rf /var/lib/apt/lists/*

CMD ["/usr/sbin/sshd", "-D", "-e", "-p", "2222"]
