# Linux Docker setup

Both platforms use one C++/nlohmann-json adapter for Bambu's unmodified networking
plugin through the pinned public ABI. The Linux executable-path shim substitutes
`readlink("/proc/self/exe")`; the macOS shim interposes `_NSGetExecutablePath`.
Other links pass through unchanged. Signing and certificate exchange remain in
the official plugin; no signing keys are extracted or vendor binaries patched.

## Requirements

- Linux x86-64 with Docker Engine and Docker Compose v2.
- Your own Bambu Studio Linux AppImage and matching Linux networking plugin from
  the `02.08.02` series. ARM Linux and other plugin versions are not validated.
- An existing signed-in Studio session and saved printer access code for your
  account. The validated account regions are North America and Europe.
- LAN access to printer ports 8883 (MQTT) and 990 (implicit FTPS), plus the passive
  FTPS data ports selected by the printer. The plugin also needs internet access
  for certificate refresh.

The server does not need a desktop, FUSE, or a running Studio GUI. Extracted Studio
files supply the executable path and certificates. Authentication is provisioned
from an existing Studio installation; this wrapper does not implement login.

## Prepare Studio and the plugin

From the repository directory, create the private configuration and persistent
working directories:

```sh
mkdir -p config/plugins cache files vendor-linux
chmod 700 config cache
```

The artifacts used for validation were:

| Artifact | Version | Official download |
| --- | --- | --- |
| Ubuntu 24.04 x86-64 Studio AppImage | `02.08.02.61` | [Studio.AppImage](https://github.com/bambulab/BambuStudio/releases/download/v02.08.02.61/BambuStudio_ubuntu24.04-v02.08.02.61-20260820225108.AppImage) |
| Linux networking plugin | `02.08.02.54` | [plugin.zip](https://public-cdn.bblmw.com/upgrade/studio/plugins/02.08.02.54/f1a94b8828/linux_02.08.02.54.zip) |

Save these as `vendor-linux/Studio.AppImage` and `vendor-linux/plugin.zip`. These
are recorded SHA-256 hashes of the tested downloads, not vendor-signed manifests:

```text
d501b103fac5424513ec0e8d6bc145fb30719de2c7d94d7320d723740c81a7fd  Studio.AppImage
a7ea8ccec85ab5a7b8959a5697f7be957d58da82e67aad1cb0ad9b3cdf5baed6  plugin.zip
```

Extract the AppImage without launching Studio:

```sh
cd vendor-linux
chmod +x Studio.AppImage
./Studio.AppImage --appimage-extract
cd ..
```

Extract the Linux plugin archive into `config/plugins/`, keeping its accompanying
shared libraries. The resulting paths must include:

```text
vendor-linux/squashfs-root/bin/bambu-studio
vendor-linux/squashfs-root/resources/cert/slicer_base64.cer
vendor-linux/squashfs-root/resources/cert/printer.cer
config/plugins/libbambu_networking.so
```

## Provision configuration

Copy your own `BambuStudio.conf` and `BambuNetworkEngine.conf` into `config/` using
a private transfer such as SSH. Preserve the saved account region and printer
access-code entries. Studio normally stores these files under
`~/.config/BambuStudio/` on Linux or
`~/Library/Application Support/BambuStudio/` on macOS. A copied macOS session was
used in the validated Linux setup; use the Linux `.so` plugin, not the Mac `.dylib`.

```sh
chmod 600 config/BambuStudio.conf config/BambuNetworkEngine.conf
```

Create `.env` with your printer's LAN address and the numeric UID/GID that own
these directories (obtain them with `id -u` and `id -g`):

```dotenv
BAMBU_HOST=printer.example
BAMBU_UID=1000
BAMBU_GID=1000
# Set BAMBU_SERIAL if this configuration contains multiple saved printers.
```

The configuration contains credentials. `config/`, `cache/`, `files/`, `.env`, and
`vendor-linux/` are excluded from source control and the Docker build context.
The image contains no proprietary binaries or account configuration. Compose
mounts the extracted Studio files read-only and mounts configuration/cache writable
for the plugin and CLI. If the account session expires, refresh it in Studio and
replace the private session file; session refresh across platforms is unverified.

## Build and run

```sh
docker compose build
./bambu printers
./bambu status --json
./bambu watch --count 3
./bambu files list
```

`./bambu` runs a disposable Compose container for each command. There is no
background service or published port; `docker compose up -d` is unnecessary.
Compose uses the host network, runs under the configured UID/GID, drops all
capabilities, and enables `no-new-privileges`.

`--host` and `--serial` override the environment defaults. File arguments inside
the container use `/files/...`, which maps to `./files/` on the host. For example:

```sh
./bambu files download /model/example.gcode.3mf /files/example.gcode.3mf
```

To pause an existing print, first read its exact job name, then explicitly request
the control operation:

```sh
./bambu status
./bambu pause --job 'Exact job name from status' --yes --json
./bambu status --json
```

The existing job/state checks, certificate-readiness guard, per-printer lock,
receipts, and acknowledgment plus telemetry confirmation remain in effect. An
unconfirmed command is not automatically retried.

P1S submission accepts single-filament slices (used filament ID 1) for a 0.4 mm
nozzle and AMS feeding. The archive checksum, selected plate, printer model,
single-extruder mapping, live nozzle diameter, and selected AMS material are
checked. The plugin verifies the configured printer identity and model before
upload and again before dispatch. P1S payloads omit the H2D nozzle mapping and
calibration fields. The original H2D right-nozzle restrictions remain in effect.

```sh
./bambu inspect /files/example.gcode.3mf
./bambu check /files/example.gcode.3mf --tray 1
# After checking that the plate is clear and the intended material is loaded:
./bambu print /files/example.gcode.3mf --tray 1
```

External spools, multiple used filaments, and other nozzle sizes are not supported.
The P1S model identifier (`C12`) and serial family (`01P`) come from
[Studio's pinned printer definition](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/resources/printers/C12.json).

## Validation

Validated on a Debian x86-64 Docker host using an Ubuntu 24.04 container, Studio
`02.08.02.61`, Linux plugin `02.08.02.54`, and P1S firmware `01.10.00.00`:

- Fresh printer status and AMS/temperature telemetry.
- Certificate-validated FTPS storage listing.
- Fresh file upload with complete read-back size/checksum verification.
- CLI download, rename with checksum verification, and deletion of task-owned
  probe files. Original printer files were not renamed or deleted.
- P1S slice validation, live model/nozzle/AMS preflight, and signed print
  submission: the printer acknowledged `project_file` and the matching job
  entered PREPARE, then RUNNING and advanced through printed layers.
- Bed-target commands at 35°C and back to 0°C, each confirmed by telemetry.
- Physical pause, resume, and cancel through the normal Docker CLI. Each received
  matching acknowledgment/state confirmation; layers advanced after resume.
  An independent post-cancel query confirmed a stopped job and both heater
  targets at 0°C.

The test print was deliberately canceled; natural completion and long-running
automation reliability remain unverified.
The wrapper's read-only `--sign-probe`
is not evidence that print controls are authorized.

The container sets `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`; the Linux
plugin otherwise encountered HTTPS peer-verification failures in this environment.
Studio's printer certificates and TLS verification remain intact.

The tested P1S accepted an upload but did not complete the data socket's TLS
shutdown handshake, causing Python's default upload to time out after sending all
bytes. For the P1S serial family, uploads close the encrypted data socket and then
require the FTP completion reply and a full read-back comparison. Certificate
verification and encrypted transfer remain enabled; other printer families keep
the standard TLS shutdown behavior.

Run offline tests without production mounts or network access:

```sh
docker run --rm --network none --entrypoint python3 bambu-headless:linux-local \
  -m unittest discover -p 'test_*.py' -v
```

The native fake-plugin tests run on both macOS and Linux. Linux-specific tests
exercise the executable-path shim, unrelated-link pass-through, truncation,
zero-length buffers, and fallback when the override is unset.

## Native Linux configuration

Outside Docker, Python 3.10+, GCC/G++, nlohmann-json development headers, and the
plugin's runtime libraries are required. The Dockerfile records the tested Ubuntu
packages. The launcher compiles the bridge and shim on first use.

| Setting | Linux default |
| --- | --- |
| `BAMBU_CONFIG_DIR` | `$XDG_CONFIG_HOME/BambuStudio`, or `~/.config/BambuStudio` |
| `BAMBU_CERT_DIR` | `/opt/bambu-studio/resources/cert` |
| `BAMBU_STUDIO_EXECUTABLE` | `/opt/bambu-studio/bin/bambu-studio` |
| `XDG_CACHE_HOME` | `~/.cache` (receipts stored in `h2d-headless/`) |

Set the Studio and certificate paths to your extracted installation. The plugin
is loaded from `$BAMBU_CONFIG_DIR/plugins/libbambu_networking.so`. Only the helper
receives `LD_PRELOAD` with the executable-path shim; no system-wide preload is used.
