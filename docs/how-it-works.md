# Headless H2D printing while keeping cloud mode

The goal was a small one-command sender: upload a sliced file and start the H2D without opening Studio or disabling Handy.

## The roadblocks

The printer accepted a local MQTT connection, but an unsigned `project_file` command failed with `84033543` (`mqtt message verify failed`). An access code was enough to connect, but not enough to authorize that command in the tested configuration.

Studio's public source exposes the networking plugin's C++ interface. A standalone Objective-C++ helper could load the installed library, register callbacks and read telemetry. Printing required more: the plugin's certificate initialization and a host-identity check.

The revealing experiment compared the same print payload's local encoding with and without an executable-path shim. Without it, encoding failed. With it, the plugin produced the security envelope and encrypted URL fields. This diagnostic did not publish a print command. The final helper calls exported APIs; it contains no private function offsets.

The [shim](../h2d_host_identity.c) interposes `_NSGetExecutablePath` **inside the helper process**, returning the path of the installed signed Studio executable. That redirects the plugin's executable-path check. It does not launch Studio, inject into Studio, patch its files, extract a signing key or change printer modes. It does deliberately work around the plugin's host check, so this is a version-sensitive compatibility technique rather than a supported vendor interface.

One false lead was instructive: a read-only calibration request worked even when print encoding still failed. Read-only access did not prove print authorization.

## The resulting sender

Python validates the slice and live printer state, then uses implicit FTPS for the file. The H2D rejected ordinary FTPS data connections with `522`; explicitly reusing the control connection's TLS session fixed that. The sender downloads the uploaded file and compares its size and MD5 before proceeding.

The native helper initializes the official plugin's certificates, asks it to sign and encrypt one `project_file` command, then waits for the requested job to enter `PREPARE` or `RUNNING`. The LAN-transport argument to certificate installation does not toggle LAN Only mode. The plugin retains responsibility for its existing account session and certificate machinery.

A local receipt written before dispatch prevents automatic retries after an ambiguous response. That matters when success means a machine starts moving.

## What was demonstrated

A single-material PLA job on an H2D's right 0.4 mm nozzle reached `PREPARE`; an independent logger then reported `RUNNING` and later `FINISH` at 100%. Cloud/Handy remained enabled, with Developer Mode off. A separate temporary upload passed full read-back verification and was removed without printing. The printed parts have not been physically inspected.

This is an open-source wrapper with a proprietary dependency. It has one tested macOS/Studio/plugin configuration, not a claim of universal Bambu compatibility. The method builds on existing public ABI and reverse-engineering work; it is not a claim to have discovered the entire protocol.

## References

- Bambu Studio: [public header at v02.08.02.61](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/Utils/bambu_networking.hpp) and [NetworkAgent wrapper](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/Utils/NetworkAgent.cpp).
- ClusterM/open-bamboo-networking: [LAN lifecycle](https://github.com/ClusterM/open-bamboo-networking/blob/master/research/08.04-lan.md), [print ABI](https://github.com/ClusterM/open-bamboo-networking/blob/master/research/08.08-print-abi.md), [MQTT signing](https://github.com/ClusterM/open-bamboo-networking/blob/master/research/10.04-mqtt-signing.md) and [field encryption](https://github.com/ClusterM/open-bamboo-networking/blob/master/research/10.03-mqtt-field-encryption.md).
