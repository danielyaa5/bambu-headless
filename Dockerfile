FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 g++ gcc nlohmann-json3-dev ca-certificates libssl3t64 libcurl4t64 libgl1 libglib2.0-0t64 binutils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN g++ -std=c++17 -O2 h2d_network_bridge.cpp -o h2d_network_bridge -ldl -pthread && gcc -shared -fPIC h2d_host_identity_linux.c -o h2d_host_identity.so -ldl
ENV HOME=/home/bambu XDG_CACHE_HOME=/cache BAMBU_CONFIG_DIR=/config BAMBU_CERT_DIR=/opt/bambu-studio/resources/cert BAMBU_STUDIO_EXECUTABLE=/opt/bambu-studio/bin/bambu-studio
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENTRYPOINT ["python3", "/app/bambu.py"]
CMD ["--help"]
